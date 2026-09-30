import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from psycopg_pool import AsyncConnectionPool

from uriel_tools.edit.names import is_draft
from uriel_tools.indexer.reconcile import reconcile
from uriel_tools.rag import jobs, store
from uriel_tools.rag.chunk import chunk_pages
from uriel_tools.rag.embed import ModelUnavailable, document_input
from uriel_tools.rag.extract import ALLOWED, Page, Skipped, extract, resolve_mime
from uriel_tools.rag.jobs import Job
from uriel_tools.rag.ocr import render_pages
from uriel_tools.rag.sources import SourcesConfig
from uriel_tools.rag.store import DocumentMeta, Folder, NewChunk
from uriel_tools.rag.webdav import AuthFailed, NotFound, RemoteEntry, TooLarge, Unreachable, WebDav

MAX_BYTES = 50 * 1024 * 1024
EXTRACT_TIMEOUT_S = 120
EMBED_BATCH = 16


class Retry(Exception):
    """Worth retrying with backoff. etag is the file version that failed, recorded if attempts run out."""

    def __init__(self, error: str, etag: str | None = None):
        super().__init__(error)
        self.etag = etag


class Wait(Exception):
    """A model server is down: try again later without spending an attempt."""


class FolderAuthFailed(Exception):
    """The uriel app password was rejected: pause indexing until a share listing succeeds again."""


class Indexer:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        sources: SourcesConfig,
        webdav_for: Callable[[Folder], WebDav],
        embedder,
        ocr,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.pool, self.sources, self.webdav_for = pool, sources, webdav_for
        self.embedder, self.ocr, self.now = embedder, ocr, now

    async def run(self, job: Job, folder: Folder) -> None:
        dav = self.webdav_for(folder)
        try:
            if job.kind == "delete":
                await store.delete_documents(self.pool, folder.id, job.path)
            elif job.kind == "reconcile":
                await reconcile(self.pool, dav, folder)
            else:
                await self._index(dav, folder, job.path, with_ocr=job.kind == "ocr")
        except AuthFailed as e:
            raise FolderAuthFailed(str(e)) from e
        except ModelUnavailable as e:
            raise Wait(str(e)) from e
        except Unreachable as e:
            raise Retry(str(e)) from e
        finally:
            await dav.aclose()

    async def _index(self, dav: WebDav, folder: Folder, path: str, with_ocr: bool) -> None:
        now = self.now()
        if is_draft(path) and not with_ocr and not self.sources.ocr_window.contains(now):
            # Drafts are read overnight, so one being reviewed during the day isn't re-read at every save.
            await jobs.enqueue(
                self.pool, "index", folder.id, path, run_after=self.sources.ocr_window.next_start(now)
            )
            return
        try:
            entry = await dav.stat(path)
        except NotFound:
            await store.delete_documents(self.pool, folder.id, path)
            return
        if entry.is_dir:  # a folder created, moved or renamed arrives as one event
            async for f in dav.walk(path):
                await jobs.enqueue(self.pool, "index", folder.id, f.path)
            return
        existing = await store.get_document(self.pool, folder.id, path)
        if not with_ocr and existing and existing["etag"] == entry.etag and existing["status"] != "failed":
            if existing["status"] == "awaiting_ocr":
                # A crash between saving as awaiting_ocr and enqueuing the ocr job would strand it
                # otherwise; enqueue is idempotent so this is a no-op when the job already exists.
                run_after = self.sources.ocr_window.next_start(self.now())
                await jobs.enqueue(self.pool, "ocr", folder.id, path, run_after=run_after)
            return
        meta = DocumentMeta(entry.path, entry.etag, entry.fileid, entry.mime, entry.size, entry.modified)
        try:
            pages = await self._pages(dav, entry, with_ocr)
        except Skipped as e:
            await store.save_document(
                self.pool, folder.id, meta, "skipped", [], self.embedder.model, error=str(e)
            )
            return
        except (AuthFailed, Unreachable, ModelUnavailable):
            raise
        except Exception as e:
            raise Retry(f"{type(e).__name__}: {e}", entry.etag) from e
        pending_ocr = any(p.needs_ocr for p in pages)
        chunks = chunk_pages(pages)
        try:
            vectors = await self._embed([document_input(path, folder.mount, c.text) for c in chunks])
            new = [NewChunk(c.page, c.source, c.text, v) for c, v in zip(chunks, vectors, strict=True)]
            status = "awaiting_ocr" if pending_ocr else "indexed"
            await store.save_document(self.pool, folder.id, meta, status, new, self.embedder.model)
        except (AuthFailed, Unreachable, ModelUnavailable):
            raise
        except Exception as e:
            raise Retry(f"{type(e).__name__}: {e}", entry.etag) from e
        if pending_ocr:
            run_after = self.sources.ocr_window.next_start(self.now())
            await jobs.enqueue(self.pool, "ocr", folder.id, path, run_after=run_after)

    async def _pages(self, dav: WebDav, entry: RemoteEntry, with_ocr: bool) -> list[Page]:
        mime = resolve_mime(entry.mime, entry.path)
        if mime not in ALLOWED:
            raise Skipped(f"unsupported type {mime or 'unknown'}")
        if entry.size > MAX_BYTES:
            raise Skipped(f"larger than {MAX_BYTES // 2**20} MB")
        try:
            data = await dav.get(entry.path, MAX_BYTES)
        except TooLarge as e:
            raise Skipped(str(e)) from e
        try:
            # The thread can't be killed on timeout; it finishes in the background and its result is dropped.
            pages = await asyncio.wait_for(asyncio.to_thread(extract, data, mime), EXTRACT_TIMEOUT_S)
        except TimeoutError as e:
            raise Skipped(f"extraction took longer than {EXTRACT_TIMEOUT_S} s") from e
        return await self._ocr(data, mime, pages) if with_ocr else pages

    async def _ocr(self, data: bytes, mime: str, pages: list[Page]) -> list[Page]:
        wanted = [p.number for p in pages if p.needs_ocr]
        if not wanted:
            return pages
        rendered = await asyncio.to_thread(render_pages, data, mime, wanted)
        texts: dict[int, str] = {}
        try:
            for number, png in rendered:
                texts[number] = await self.ocr.transcribe(png)
        finally:
            await self.ocr.release()
        return [Page(p.number, texts.get(p.number, ""), source="ocr") if p.needs_ocr else p for p in pages]

    async def _embed(self, inputs: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(inputs), EMBED_BATCH):
            vectors += await self.embedder.embed(inputs[i : i + EMBED_BATCH])
        return vectors
