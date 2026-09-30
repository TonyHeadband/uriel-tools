from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg.errors import UniqueViolation
from psycopg_pool import AsyncConnectionPool

MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    folder_id: int
    path: str
    forced: bool
    attempts: int


async def enqueue(
    pool: AsyncConnectionPool,
    kind: str,
    folder_id: int,
    path: str = "",
    *,
    run_after: datetime | None = None,
    forced: bool = False,
) -> int:
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "INSERT INTO jobs (kind, folder_id, path, run_after, forced) "
                "VALUES (%s, %s, %s, coalesce(%s, now()), %s) "
                "ON CONFLICT (kind, folder_id, path) WHERE locked_until IS NULL "
                "DO UPDATE SET run_after = least(jobs.run_after, EXCLUDED.run_after), "
                "forced = jobs.forced OR EXCLUDED.forced RETURNING id",
                (kind, folder_id, path, run_after, forced),
            )
        ).fetchone()
    return row["id"]


async def claim(pool: AsyncConnectionPool, ocr_window_open: bool) -> Job | None:
    """Lease the next due job.

    OCR waits for the window unless forced; folders that failed auth are skipped, except for delete
    jobs, which need no WebDAV credentials and must still remove documents a user can no longer see.
    """
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "UPDATE jobs SET locked_until = now() + interval '10 minutes' WHERE id = ("
                " SELECT j.id FROM jobs j JOIN folders f ON f.id = j.folder_id"
                " WHERE j.run_after <= now() AND (j.locked_until IS NULL OR j.locked_until < now())"
                "  AND (f.state = 'ok' OR j.kind = 'delete') AND (j.kind <> 'ocr' OR j.forced OR %s)"
                " ORDER BY j.forced DESC, j.run_after, j.id LIMIT 1 FOR UPDATE OF j SKIP LOCKED"
                ") RETURNING id, kind, folder_id, path, forced, attempts",
                (ocr_window_open,),
            )
        ).fetchone()
    return Job(**row) if row else None


async def complete(pool: AsyncConnectionPool, job_id: int) -> None:
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM jobs WHERE id = %s", (job_id,))


async def _release(pool: AsyncConnectionPool, job_id: int, assignments: str, params: dict) -> None:
    try:
        async with pool.connection() as conn:
            await conn.execute(
                f"UPDATE jobs SET {assignments}, locked_until = NULL WHERE id = %(id)s",
                params | {"id": job_id},
            )
    except UniqueViolation:
        # A webhook queued the same work while this job ran; that pending job supersedes this one.
        await complete(pool, job_id)


async def retry(pool: AsyncConnectionPool, job: Job, error: str) -> bool:
    attempts = job.attempts + 1
    if attempts >= MAX_ATTEMPTS:
        await complete(pool, job.id)
        return False
    await _release(
        pool,
        job.id,
        "attempts = %(attempts)s, last_error = %(error)s, "
        "run_after = now() + make_interval(mins => %(mins)s)",
        {"attempts": attempts, "error": error, "mins": 2**attempts},
    )
    return True


async def postpone(pool: AsyncConnectionPool, job: Job, delay: timedelta, error: str) -> None:
    await _release(
        pool,
        job.id,
        "last_error = %(error)s, run_after = now() + make_interval(secs => %(secs)s)",
        {"error": error, "secs": delay.total_seconds()},
    )


async def drop_folder_jobs(pool: AsyncConnectionPool, folder_id: int) -> None:
    """Drop pending work for a folder that just failed auth, but keep pending deletes: they need no
    WebDAV access and must still run so a file removed in Nextcloud stops being served."""
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM jobs WHERE folder_id = %s AND kind <> 'delete'", (folder_id,))


async def request_ocr(pool: AsyncConnectionPool, folder_id: int, path: str) -> int:
    job_id = await enqueue(pool, "ocr", folder_id, path, forced=True)
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT count(*) AS n FROM jobs WHERE kind = 'ocr' AND forced AND id < %s "
                "AND (locked_until IS NULL OR locked_until > now())",
                (job_id,),
            )
        ).fetchone()
    return row["n"]
