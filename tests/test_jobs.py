from datetime import UTC, datetime, timedelta

import pytest

from tests.dbutil import folders, set_state
from uriel_tools.rag import jobs

pytestmark = pytest.mark.db


async def rows(pool):
    async with pool.connection() as conn:
        return await (await conn.execute("SELECT * FROM jobs ORDER BY id")).fetchall()


async def test_enqueue_collapses_duplicates(pool):
    [f] = await folders(pool, {"owner": "a", "path": "/D"})
    later = datetime.now(UTC) + timedelta(hours=1)
    first = await jobs.enqueue(pool, "ocr", f.id, "/D/x.png", run_after=later)
    again = await jobs.enqueue(pool, "ocr", f.id, "/D/x.png", forced=True)
    assert first == again
    [row] = await rows(pool)
    assert row["forced"] and row["run_after"] < later


async def test_claim_respects_run_after_and_lease(pool):
    [f] = await folders(pool, {"owner": "a", "path": "/D"})
    await jobs.enqueue(pool, "index", f.id, "/D/later", run_after=datetime.now(UTC) + timedelta(hours=1))
    await jobs.enqueue(pool, "index", f.id, "/D/now")
    job = await jobs.claim(pool, ocr_window_open=False)
    assert job.path == "/D/now"
    assert await jobs.claim(pool, ocr_window_open=False) is None  # leased, and the other is not due
    async with pool.connection() as conn:  # simulate a crashed worker whose lease ran out
        await conn.execute(
            "UPDATE jobs SET locked_until = now() - interval '1 second' WHERE id = %s", (job.id,)
        )
    assert (await jobs.claim(pool, ocr_window_open=False)).id == job.id


async def test_ocr_jobs_wait_for_the_window_unless_forced(pool):
    [f] = await folders(pool, {"owner": "a", "path": "/D"})
    await jobs.enqueue(pool, "ocr", f.id, "/D/a.png")
    assert await jobs.claim(pool, ocr_window_open=False) is None
    assert (await jobs.claim(pool, ocr_window_open=True)).path == "/D/a.png"
    await jobs.enqueue(pool, "ocr", f.id, "/D/b.png", forced=True)
    assert (await jobs.claim(pool, ocr_window_open=False)).path == "/D/b.png"


async def test_forced_jobs_go_first_and_auth_failed_folders_are_skipped(pool):
    ok, broken = await folders(pool, {"owner": "a", "path": "/D"}, {"owner": "b", "path": "/E"})
    await set_state(pool, broken.id, "auth_failed")
    await jobs.enqueue(pool, "index", broken.id, "/E/x")
    await jobs.enqueue(pool, "index", ok.id, "/D/plain")
    await jobs.enqueue(pool, "ocr", ok.id, "/D/urgent.png", forced=True)
    assert (await jobs.claim(pool, ocr_window_open=False)).path == "/D/urgent.png"
    assert (await jobs.claim(pool, ocr_window_open=False)).path == "/D/plain"
    assert await jobs.claim(pool, ocr_window_open=False) is None


async def test_delete_jobs_run_for_auth_failed_folders_but_not_index_jobs(pool):
    [broken] = await folders(pool, {"owner": "b", "path": "/E"})
    await set_state(pool, broken.id, "auth_failed")
    await jobs.enqueue(pool, "index", broken.id, "/E/x")
    await jobs.enqueue(pool, "delete", broken.id, "/E/y")
    job = await jobs.claim(pool, ocr_window_open=False)
    assert (job.kind, job.path) == ("delete", "/E/y")
    assert await jobs.claim(pool, ocr_window_open=False) is None  # the index job stays unclaimed


async def test_drop_folder_jobs_keeps_pending_deletes(pool):
    [f] = await folders(pool, {"owner": "a", "path": "/D"})
    await jobs.enqueue(pool, "index", f.id, "/D/x")
    await jobs.enqueue(pool, "delete", f.id, "/D/y")
    await jobs.drop_folder_jobs(pool, f.id)
    [row] = await rows(pool)
    assert (row["kind"], row["path"]) == ("delete", "/D/y")


async def test_retry_backs_off_then_gives_up(pool):
    [f] = await folders(pool, {"owner": "a", "path": "/D"})
    await jobs.enqueue(pool, "index", f.id, "/D/x")
    job = await jobs.claim(pool, ocr_window_open=False)
    assert await jobs.retry(pool, job, "boom") is True
    [row] = await rows(pool)
    assert row["attempts"] == 1 and row["locked_until"] is None and row["last_error"] == "boom"
    assert row["run_after"] > datetime.now(UTC) + timedelta(seconds=100)  # 2^1 minutes
    exhausted = jobs.Job(job.id, job.kind, job.folder_id, job.path, job.forced, jobs.MAX_ATTEMPTS - 1)
    assert await jobs.retry(pool, exhausted, "boom") is False
    assert await rows(pool) == []


async def test_retry_when_a_newer_duplicate_is_pending(pool):
    [f] = await folders(pool, {"owner": "a", "path": "/D"})
    await jobs.enqueue(pool, "index", f.id, "/D/x")
    running = await jobs.claim(pool, ocr_window_open=False)
    newer = await jobs.enqueue(pool, "index", f.id, "/D/x")  # a webhook arrives mid-job
    assert await jobs.retry(pool, running, "boom") is True
    await jobs.postpone(pool, running, timedelta(minutes=5), "down")  # also safe after the job is gone
    assert [r["id"] for r in await rows(pool)] == [newer]


async def test_postpone_spends_no_attempt(pool):
    [f] = await folders(pool, {"owner": "a", "path": "/D"})
    await jobs.enqueue(pool, "index", f.id, "/D/x")
    job = await jobs.claim(pool, ocr_window_open=False)
    await jobs.postpone(pool, job, timedelta(minutes=5), "ollama down")
    [row] = await rows(pool)
    assert row["attempts"] == 0 and row["locked_until"] is None


async def test_request_ocr_reports_jobs_ahead(pool):
    [f] = await folders(pool, {"owner": "a", "path": "/D"})
    await jobs.request_ocr(pool, f.id, "/D/first.png")
    assert await jobs.request_ocr(pool, f.id, "/D/second.png") == 1
    await jobs.drop_folder_jobs(pool, f.id)
    assert await rows(pool) == []
