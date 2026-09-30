from datetime import UTC, datetime, timedelta

import pytest

from tests import filegen
from tests.dbutil import folders, meta
from tests.fakes.models import FakeOcr, HashEmbedder
from tests.fakes.webdav import FakeNextcloud
from uriel_tools.indexer import pipeline
from uriel_tools.indexer.pipeline import FolderAuthFailed, Indexer, Retry, Wait
from uriel_tools.indexer.reconcile import reconcile
from uriel_tools.rag import store
from uriel_tools.rag.jobs import Job
from uriel_tools.rag.sources import SourcesConfig
from uriel_tools.rag.webdav import NotFound, RemoteEntry, WebDav

pytestmark = pytest.mark.db

NOON = datetime(2026, 3, 2, 11, 0, tzinfo=UTC)  # 12:00 in Rome: the OCR window is closed


class Env:
    def __init__(self, pool, folder, nc, password="pw"):
        self.pool, self.folder, self.nc = pool, folder, nc
        self.embedder, self.ocr = HashEmbedder(), FakeOcr()
        self.sources = SourcesConfig()
        self.password = password
        self.indexer = Indexer(pool, self.sources, self.dav, self.embedder, self.ocr, now=lambda: NOON)

    def dav(self, folder):
        return WebDav("http://nc", "alice", self.password, transport=self.nc.transport())

    async def run(self, kind, path=""):
        await self.indexer.run(Job(0, kind, self.folder.id, path, False, 0), self.folder)

    async def doc(self, path):
        return await store.get_document(self.pool, self.folder.id, path)

    async def chunks(self, path):
        async with self.pool.connection() as conn:
            return await (
                await conn.execute(
                    "SELECT c.* FROM chunks c JOIN documents d ON d.id = c.document_id "
                    "WHERE d.folder_id = %s AND d.path = %s ORDER BY ord",
                    (self.folder.id, path),
                )
            ).fetchall()

    async def jobs(self):
        async with self.pool.connection() as conn:
            return await (await conn.execute("SELECT kind, path, run_after FROM jobs ORDER BY id")).fetchall()


@pytest.fixture
async def env(pool):
    [folder] = await folders(pool, {"owner": "alice", "path": "/Documents"})
    return Env(pool, folder, FakeNextcloud({"alice": "pw"}))


async def test_index_text_pdf(env):
    f = env.nc.put(
        "alice",
        "/Documents/caldaia.pdf",
        filegen.text_pdf(["Premere il tasto reset per cinque secondi"]),
        "application/pdf",
    )
    await env.run("index", "/Documents/caldaia.pdf")
    doc = await env.doc("/Documents/caldaia.pdf")
    assert (doc["status"], doc["etag"]) == ("indexed", f.etag)
    [chunk] = await env.chunks("/Documents/caldaia.pdf")
    assert chunk["embed_model"] == "hash-embed" and "reset" in chunk["text"]
    assert env.embedder.calls[0][0].startswith("caldaia.pdf — /Documents\n")


async def test_unchanged_etag_is_not_downloaded_again(env):
    env.nc.put("alice", "/Documents/a.txt", b"ciao a tutti", "text/plain")
    await env.run("index", "/Documents/a.txt")
    await env.run("index", "/Documents/a.txt")
    assert len(env.nc.gets) == 1


async def test_scan_waits_for_the_ocr_window_then_is_read(env):
    env.nc.put("alice", "/Documents/ricevuta.pdf", filegen.scanned_pdf(["Ricevuta 4471"]), "application/pdf")
    await env.run("index", "/Documents/ricevuta.pdf")
    assert (await env.doc("/Documents/ricevuta.pdf"))["status"] == "awaiting_ocr"
    [job] = await env.jobs()
    assert job["kind"] == "ocr" and job["run_after"] == datetime(2026, 3, 3, 0, 0, tzinfo=UTC)  # 01:00 Rome
    await env.run("ocr", "/Documents/ricevuta.pdf")
    assert (await env.doc("/Documents/ricevuta.pdf"))["status"] == "indexed"
    [chunk] = await env.chunks("/Documents/ricevuta.pdf")
    assert (chunk["source"], chunk["text"]) == ("ocr", "testo scansionato")
    assert (env.ocr.calls, env.ocr.released) == (1, 1)


async def test_edit_while_awaiting_ocr_keeps_one_ocr_job(env):
    env.nc.put("alice", "/Documents/r.pdf", filegen.scanned_pdf(["v1"]), "application/pdf")
    await env.run("index", "/Documents/r.pdf")
    env.nc.put("alice", "/Documents/r.pdf", filegen.scanned_pdf(["v2"]), "application/pdf")
    await env.run("index", "/Documents/r.pdf")
    assert [j["kind"] for j in await env.jobs()] == ["ocr"]
    assert (await env.doc("/Documents/r.pdf"))["etag"] == env.nc.files[("alice", "/Documents/r.pdf")].etag


async def test_unsupported_and_oversized_files_are_skipped(env, monkeypatch):
    env.nc.put("alice", "/Documents/a.zip", b"PK", "application/zip")
    await env.run("index", "/Documents/a.zip")
    doc = await env.doc("/Documents/a.zip")
    assert doc["status"] == "skipped"
    monkeypatch.setattr(pipeline, "MAX_BYTES", 5)
    env.nc.put("alice", "/Documents/big.txt", b"0123456789", "text/plain")
    await env.run("index", "/Documents/big.txt")
    assert (await env.doc("/Documents/big.txt"))["status"] == "skipped"
    assert await env.chunks("/Documents/big.txt") == []


async def test_vanished_file_is_deleted(env):
    env.nc.put("alice", "/Documents/a.txt", b"ciao a tutti", "text/plain")
    await env.run("index", "/Documents/a.txt")
    env.nc.delete("alice", "/Documents/a.txt")
    await env.run("index", "/Documents/a.txt")
    assert await env.doc("/Documents/a.txt") is None


async def test_index_job_on_a_directory_queues_its_files(env):
    env.nc.put("alice", "/Documents/Casa/a.txt", b"uno", "text/plain")
    env.nc.put("alice", "/Documents/Casa/Sub/b.txt", b"due", "text/plain")
    await env.run("index", "/Documents/Casa")
    assert sorted(j["path"] for j in await env.jobs()) == [
        "/Documents/Casa/Sub/b.txt",
        "/Documents/Casa/a.txt",
    ]


async def test_delete_job_removes_the_subtree(env):
    for p in ("/Documents/Casa/a.txt", "/Documents/Casa/b.txt", "/Documents/c.txt"):
        env.nc.put("alice", p, b"testo", "text/plain")
        await env.run("index", p)
    await env.run("delete", "/Documents/Casa")
    assert sorted(await store.folder_documents(env.pool, env.folder.id)) == ["/Documents/c.txt"]


async def test_failures_are_classified(env):
    env.nc.put("alice", "/Documents/a.txt", b"ciao a tutti", "text/plain")
    env.embedder.fail = True
    with pytest.raises(Wait):
        await env.run("index", "/Documents/a.txt")
    env.embedder.fail = False
    env.password = "revoked"
    with pytest.raises(FolderAuthFailed):
        await env.run("index", "/Documents/a.txt")
    env.password = "pw"
    f = env.nc.put("alice", "/Documents/bad.pdf", b"%PDF-1.4 garbage", "application/pdf")
    with pytest.raises(Retry) as err:
        await env.run("index", "/Documents/bad.pdf")
    assert err.value.etag == f.etag


async def test_reconcile_queues_changes_and_removals(env):
    for p in (
        "/Documents/same.txt",
        "/Documents/changed.txt",
        "/Documents/gone.txt",
        "/Documents/failed.txt",
    ):
        env.nc.put("alice", p, b"testo", "text/plain")
        await env.run("index", p)
    await store.mark_failed(
        env.pool,
        env.folder.id,
        "/Documents/failed.txt",
        env.nc.files[("alice", "/Documents/failed.txt")].etag,
        "x",
    )
    env.nc.put("alice", "/Documents/changed.txt", b"nuovo", "text/plain")
    env.nc.put("alice", "/Documents/new.txt", b"nuovo", "text/plain")
    env.nc.delete("alice", "/Documents/gone.txt")
    queued, removed = await reconcile(env.pool, env.dav(env.folder), env.folder)
    assert (queued, removed) == (2, 1)
    got = sorted((j["kind"], j["path"]) for j in await env.jobs())
    assert got == [
        ("delete", "/Documents/gone.txt"),
        ("index", "/Documents/changed.txt"),
        ("index", "/Documents/new.txt"),
    ]


async def test_reconcile_of_a_deleted_folder_removes_everything(env):
    env.nc.put("alice", "/Documents/a.txt", b"testo", "text/plain")
    await env.run("index", "/Documents/a.txt")
    env.nc.delete("alice", "/Documents")
    assert await reconcile(env.pool, env.dav(env.folder), env.folder) == (0, 1)


async def test_run_dispatches_reconcile(env):
    env.nc.put("alice", "/Documents/a.txt", b"testo", "text/plain")
    await env.run("reconcile")
    assert [(j["kind"], j["path"]) for j in await env.jobs()] == [("index", "/Documents/a.txt")]
    assert all(j["run_after"] <= datetime.now(UTC) + timedelta(seconds=5) for j in await env.jobs())


async def test_reconcile_of_a_vanished_subfolder_does_not_delete_everything(env, monkeypatch):
    """A 404 mid-walk (a subfolder renamed while walking) must not be mistaken for the root vanishing."""
    env.nc.put("alice", "/Documents/a.txt", b"testo", "text/plain")
    await env.run("index", "/Documents/a.txt")
    env.nc.put("alice", "/Documents/Casa/b.txt", b"testo", "text/plain")
    await env.run("index", "/Documents/Casa/b.txt")
    dav = env.dav(env.folder)
    seen = env.nc.files[("alice", "/Documents/a.txt")]

    async def flaky_walk(path):
        yield RemoteEntry("/Documents/a.txt", False, seen.etag, seen.fileid, "text/plain", 5, None)
        raise NotFound("Casa vanished mid-walk")  # the root itself, /Documents, still exists

    monkeypatch.setattr(dav, "walk", flaky_walk)
    with pytest.raises(NotFound):
        await reconcile(env.pool, dav, env.folder)
    assert await env.jobs() == []


async def test_awaiting_ocr_gets_an_ocr_job_if_the_enqueue_was_missed(env):
    """A crash right after saving as awaiting_ocr but before enqueuing the ocr job must not strand it."""
    f = env.nc.put("alice", "/Documents/ricevuta.pdf", filegen.scanned_pdf(["v1"]), "application/pdf")
    await store.save_document(
        env.pool, env.folder.id, meta("/Documents/ricevuta.pdf", f.etag), "awaiting_ocr", [], "hash-embed"
    )
    await env.run("index", "/Documents/ricevuta.pdf")
    assert [j["kind"] for j in await env.jobs()] == ["ocr"]


async def test_non_model_failure_in_embed_stage_is_retried_with_etag(env, monkeypatch):
    f = env.nc.put("alice", "/Documents/a.txt", b"ciao a tutti", "text/plain")

    async def boom(texts, *, query=False):
        raise ValueError("dimension mismatch")

    monkeypatch.setattr(env.embedder, "embed", boom)
    with pytest.raises(Retry) as err:
        await env.run("index", "/Documents/a.txt")
    assert err.value.etag == f.etag
