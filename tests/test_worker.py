import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from tests.dbutil import doc, set_state
from tests.fakes.models import FakeOcr, HashEmbedder
from tests.fakes.webdav import FakeNextcloud
from uriel_tools.indexer.pipeline import Indexer
from uriel_tools.indexer.worker import Worker
from uriel_tools.rag import jobs, store
from uriel_tools.rag.sources import SourcesConfig
from uriel_tools.rag.webdav import AuthFailed, Unreachable, WebDav

pytestmark = pytest.mark.db


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


class Setup:
    def __init__(self, nc, clock, embedder, shares):
        self.nc, self.clock, self.embedder, self.shares = nc, clock, embedder, shares
        self.password = "pw"
        self.list_error: Exception | None = None
        self.worker: Worker


@pytest.fixture
async def setup(pool, tmp_path):
    sources = SourcesConfig()
    s = Setup(
        FakeNextcloud({"uriel": "pw"}),
        Clock(datetime(2026, 3, 2, 11, 0, tzinfo=UTC)),
        HashEmbedder(),
        [
            {"id": "1", "share_type": 0, "share_with": "uriel", "uid_owner": "alice", "path": "/Documents"},
            {"id": "2", "share_type": 1, "share_with": "uriel-family", "uid_owner": "bob", "path": "/Docs"},
        ],
    )

    def dav(folder):
        return WebDav("http://nc", "uriel", s.password, transport=s.nc.transport())

    async def list_shares():
        if s.list_error:
            raise s.list_error
        if s.password != "pw":
            raise AuthFailed("OCS 401")
        return list(s.shares)

    indexer = Indexer(pool, sources, dav, s.embedder, FakeOcr(), now=s.clock)
    s.worker = Worker(
        pool, sources, indexer, s.embedder, list_shares, now=s.clock, heartbeat=tmp_path / "beat"
    )
    return s


async def job_rows(pool):
    async with pool.connection() as conn:
        return await (await conn.execute("SELECT * FROM jobs ORDER BY id")).fetchall()


async def by_mount(pool):
    return {f.mount: f for f in await store.list_folders(pool)}


async def drop_all_jobs(pool):
    for f in (await by_mount(pool)).values():
        await jobs.drop_folder_jobs(pool, f.id)


async def test_startup_mirrors_the_shares_and_reconciles_them(pool, setup):
    await setup.worker.startup()
    fs = await by_mount(pool)
    assert (fs["/Documents"].owners, fs["/Documents"].share_with) == (("alice",), ())
    assert (fs["/Docs"].owners, fs["/Docs"].share_with) == (("bob",), ("family",))
    assert sorted(j["folder_id"] for j in await job_rows(pool)) == sorted(f.id for f in fs.values())


async def test_steps_run_reconcile_then_index(pool, setup):
    setup.nc.put("uriel", "/Documents/a.txt", b"ciao a tutti", "text/plain")
    setup.shares.pop()  # just alice's share
    await setup.worker.startup()
    assert await setup.worker.step() is True  # reconcile
    assert await setup.worker.step() is True  # index
    assert await job_rows(pool) == []
    f = (await by_mount(pool))["/Documents"]
    assert (await store.get_document(pool, f.id, "/Documents/a.txt"))["status"] == "indexed"


async def test_a_new_share_is_picked_up_on_the_next_poll(pool, setup):
    await setup.worker.startup()
    await drop_all_jobs(pool)
    setup.shares.append(
        {"id": "3", "share_type": 0, "share_with": "uriel", "uid_owner": "carol", "path": "/Ricette"}
    )
    setup.nc.put("uriel", "/Ricette/torta.txt", b"farina, uova", "text/plain")
    await setup.worker.step()
    assert "/Ricette" not in await by_mount(pool)  # the next poll isn't due yet
    setup.clock.at += timedelta(minutes=5)
    await setup.worker.step()  # polls, then runs the new folder's reconcile
    new = (await by_mount(pool))["/Ricette"]
    assert new.owners == ("carol",)
    assert [(j["kind"], j["folder_id"], j["path"]) for j in await job_rows(pool)] == [
        ("index", new.id, "/Ricette/torta.txt")
    ]


async def test_unsharing_drops_the_folder_and_its_documents(pool, setup):
    await setup.worker.startup()
    await doc(pool, (await by_mount(pool))["/Docs"], "/Docs/old.txt", ["testo vecchio"])
    setup.shares.pop()
    setup.clock.at += timedelta(minutes=5)
    await setup.worker.step()
    assert "/Docs" not in await by_mount(pool)
    async with pool.connection() as conn:
        assert (await (await conn.execute("SELECT count(*) AS n FROM documents")).fetchone())["n"] == 0


async def test_a_failed_listing_changes_nothing(pool, setup):
    await setup.worker.startup()
    before = await by_mount(pool)
    setup.list_error = Unreachable("ConnectError")
    setup.clock.at += timedelta(minutes=5)
    await setup.worker.step()
    assert await by_mount(pool) == before


async def test_model_outage_postpones_without_spending_attempts(pool, setup):
    setup.nc.put("uriel", "/Documents/a.txt", b"ciao", "text/plain")
    setup.shares.pop()
    await setup.worker.startup()
    await setup.worker.step()  # reconcile
    setup.embedder.fail = True
    await setup.worker.step()
    [row] = await job_rows(pool)
    assert row["attempts"] == 0 and row["run_after"] > datetime.now(UTC) + timedelta(minutes=4)


async def test_exhausted_retries_mark_the_document_failed(pool, setup):
    f = setup.nc.put("uriel", "/Documents/bad.pdf", b"%PDF-1.4 garbage", "application/pdf")
    setup.shares.pop()
    await setup.worker.startup()
    await setup.worker.step()  # reconcile queues the index job
    async with pool.connection() as conn:
        await conn.execute("UPDATE jobs SET attempts = %s", (jobs.MAX_ATTEMPTS - 1,))
    await setup.worker.step()
    folder = (await by_mount(pool))["/Documents"]
    got = await store.get_document(pool, folder.id, "/Documents/bad.pdf")
    assert (got["status"], got["etag"]) == ("failed", f.etag)
    assert await job_rows(pool) == []


async def test_a_revoked_password_pauses_every_folder_until_a_listing_succeeds(pool, setup):
    setup.nc.put("uriel", "/Documents/a.txt", b"ciao", "text/plain")
    await setup.worker.startup()
    setup.password = "revoked"
    await setup.worker.step()  # a reconcile hits the 401
    assert {f.state for f in (await by_mount(pool)).values()} == {"auth_failed"}
    assert await job_rows(pool) == []
    setup.password = "pw"  # fixed; the next listing proves it
    await setup.worker.poll_shares()
    fs = await by_mount(pool)
    assert {f.state for f in fs.values()} == {"ok"}
    assert sorted((j["kind"], j["folder_id"]) for j in await job_rows(pool)) == sorted(
        ("reconcile", f.id) for f in fs.values()
    )


async def test_idle_worker_reembeds_stale_chunks(pool, setup):
    await setup.worker.startup()
    await drop_all_jobs(pool)
    await doc(
        pool, (await by_mount(pool))["/Documents"], "/Documents/old.txt", ["vecchio testo"], model="old"
    )
    assert await setup.worker.step() is True
    assert await store.stale_chunks(pool, "hash-embed", 10) == []
    assert await setup.worker.step() is False


async def test_daily_reconcile_is_queued_after_00_30(pool, setup):
    setup.shares.pop()
    await setup.worker.startup()
    await setup.worker.step()  # the startup reconcile: nothing to index yet
    assert await job_rows(pool) == []
    setup.nc.put("uriel", "/Documents/late.txt", b"ciao", "text/plain")  # no webhook arrives for this one
    setup.clock.at = datetime(2026, 3, 3, 0, 0, tzinfo=UTC)  # 01:00 Rome: past the 00:30 reconcile time
    await setup.worker.step()  # queues the daily reconcile and runs it
    assert [(j["kind"], j["path"]) for j in await job_rows(pool)] == [("index", "/Documents/late.txt")]


async def test_delete_runs_for_an_auth_failed_folder_without_webdav(pool, setup):
    await setup.worker.startup()
    await drop_all_jobs(pool)
    docs = (await by_mount(pool))["/Docs"]
    await set_state(pool, docs.id, "auth_failed")
    await doc(pool, docs, "/Docs/old.txt", ["testo vecchio"])
    await jobs.enqueue(pool, "delete", docs.id, "/Docs/old.txt")
    assert await setup.worker.step() is True  # the delete: no WebDAV credentials needed or used
    assert await store.get_document(pool, docs.id, "/Docs/old.txt") is None
    assert setup.nc.gets == []


async def test_embedder_self_check_blocks_claiming_until_it_recovers(pool, setup, monkeypatch):
    import uriel_tools.indexer.worker as worker_module

    monkeypatch.setattr(worker_module, "IDLE_SLEEP_S", 0.05)
    setup.nc.put("uriel", "/Documents/a.txt", b"ciao", "text/plain")
    setup.embedder.fail = True
    stop = asyncio.Event()
    task = asyncio.create_task(setup.worker.run_forever(stop))
    try:
        await asyncio.sleep(0.3)
        alice = (await by_mount(pool))["/Documents"]
        assert await store.get_document(pool, alice.id, "/Documents/a.txt") is None
        rows = await job_rows(pool)
        assert rows and all(r["locked_until"] is None for r in rows)  # nothing claimed while unhealthy
        setup.embedder.fail = False
        for _ in range(50):
            await asyncio.sleep(0.05)
            if await store.get_document(pool, alice.id, "/Documents/a.txt"):
                break
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
    assert (await store.get_document(pool, alice.id, "/Documents/a.txt"))["status"] == "indexed"


async def test_run_forever_stops_and_beats(pool, setup, tmp_path):
    stop = asyncio.Event()
    task = asyncio.create_task(setup.worker.run_forever(stop))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert (tmp_path / "beat").exists()
