import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from psycopg_pool import AsyncConnectionPool

from uriel_tools.indexer.pipeline import EMBED_BATCH, FolderAuthFailed, Indexer, Retry, Wait
from uriel_tools.rag import jobs, store
from uriel_tools.rag.embed import DIMENSIONS, ModelUnavailable, document_input
from uriel_tools.rag.shares import mounts_from
from uriel_tools.rag.sources import SourcesConfig, next_daily
from uriel_tools.rag.webdav import AuthFailed

log = logging.getLogger("uriel_tools.indexer")
HEARTBEAT = Path("/tmp/uriel-indexer-heartbeat")
WAIT_DELAY = timedelta(minutes=5)
IDLE_SLEEP_S = 5.0
PROBE_TEXT = "startup self-check probe"


class Worker:
    """One job at a time. Logs carry ids and exception type names only, never paths or text."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        sources: SourcesConfig,
        indexer: Indexer,
        embedder,
        list_shares: Callable[[], Awaitable[list[dict]]],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        heartbeat: Path | None = HEARTBEAT,
    ):
        self.pool, self.sources, self.indexer, self.embedder = pool, sources, indexer, embedder
        self.list_shares, self.now, self.heartbeat = list_shares, now, heartbeat
        self.next_reconcile = datetime.max.replace(tzinfo=UTC)
        self.next_share_poll = datetime.min.replace(tzinfo=UTC)

    async def startup(self) -> None:
        await self.poll_shares(reconcile_all=True)  # catch up on whatever changed while the indexer was down
        self.next_reconcile = self._next_reconcile()

    async def poll_shares(self, *, reconcile_all: bool = False) -> None:
        """Mirror what is shared with uriel. A failed listing changes nothing: a Nextcloud hiccup must never
        look like everyone unsharing everything."""
        self.next_share_poll = self.now() + timedelta(minutes=self.sources.share_poll_minutes)
        try:
            shares = await self.list_shares()
        except AuthFailed:
            log.error("the uriel app password was rejected; indexing is paused until it is fixed")
            await store.set_all_folders_state(self.pool, "auth_failed")
            return
        except Exception as e:
            log.warning("listing shares failed: %s", type(e).__name__)
            return
        paused = {f.id for f in await store.list_folders(self.pool) if f.state != "ok"}
        folders, added = await store.sync_mounts(
            self.pool, mounts_from(shares, self.sources.share_group_prefix)
        )
        wanted = folders if reconcile_all else [f for f in folders if f in added or f.id in paused]
        for folder in wanted:
            await jobs.enqueue(self.pool, "reconcile", folder.id)
        if added:
            log.info("%d new shared folder(s)", len(added))

    def _next_reconcile(self) -> datetime:
        return next_daily(self.now(), self.sources.reconcile_at, self.sources.ocr_window.tz)

    async def step(self) -> bool:
        now = self.now()
        if now >= self.next_share_poll:
            await self.poll_shares()
        if now >= self.next_reconcile:
            for folder in await store.list_folders(self.pool):
                if folder.state == "ok":
                    await jobs.enqueue(self.pool, "reconcile", folder.id)
            self.next_reconcile = self._next_reconcile()
        job = await jobs.claim(self.pool, self.sources.ocr_window.contains(now))
        if job is None:
            return await self._reembed_some()
        folder = await store.get_folder(self.pool, job.folder_id)
        if folder is None:
            await jobs.complete(self.pool, job.id)
            return True
        try:
            await self.indexer.run(job, folder)
        except Wait as e:
            log.warning("job %s (%s) waiting: model unavailable", job.id, job.kind)
            await jobs.postpone(self.pool, job, WAIT_DELAY, str(e))
        except FolderAuthFailed:
            # One credential reads every folder, so a rejection pauses them all; the next share listing that
            # succeeds resumes them.
            log.error("the uriel app password was rejected; indexing is paused until it is fixed")
            await store.set_all_folders_state(self.pool, "auth_failed")
            for f in await store.list_folders(self.pool):
                await jobs.drop_folder_jobs(self.pool, f.id)
        except Exception as e:
            log.warning("job %s (%s, folder %s) failed: %s", job.id, job.kind, folder.id, type(e).__name__)
            retried = await jobs.retry(self.pool, job, f"{type(e).__name__}: {e}")
            if not retried and job.kind in ("index", "ocr"):
                etag = e.etag if isinstance(e, Retry) else None
                await store.mark_failed(self.pool, folder.id, job.path, etag, f"{type(e).__name__}: {e}")
        else:
            await jobs.complete(self.pool, job.id)
        return True

    async def _reembed_some(self) -> bool:
        rows = await store.stale_chunks(self.pool, self.embedder.model, EMBED_BATCH)
        if not rows:
            return False
        try:
            vectors = await self.embedder.embed(
                [document_input(r["path"], r["folder"], r["text"]) for r in rows]
            )
        except ModelUnavailable:
            return False
        await store.update_embeddings(
            self.pool, [(r["id"], v) for r, v in zip(rows, vectors, strict=True)], self.embedder.model
        )
        return True

    async def _embedder_ready(self) -> bool:
        """A wrong or unpulled model name (Ollama 404, wrong dims, ...) must not make the worker claim
        and then fail every job; check once before claiming any."""
        try:
            [vector] = await self.embedder.embed([PROBE_TEXT])
        except Exception as e:
            log.error("embedder self-check failed: %s", type(e).__name__)
            return False
        if len(vector) != DIMENSIONS:
            log.error("embedder self-check failed: got %d dimensions, need %d", len(vector), DIMENSIONS)
            return False
        return True

    async def run_forever(self, stop: asyncio.Event) -> None:
        await self.startup()
        while not stop.is_set() and not await self._embedder_ready():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), IDLE_SLEEP_S)
        while not stop.is_set():
            if self.heartbeat:
                self.heartbeat.touch()
            try:
                busy = await self.step()
            except Exception as e:  # e.g. Postgres restarting: keep the process alive and try again
                log.error("indexer step failed: %s", type(e).__name__)
                busy = False
            if not busy:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), IDLE_SLEEP_S)
