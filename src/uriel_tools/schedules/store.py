"""The schedules tables. A function that takes `owner` touches that person's rows only."""

from datetime import datetime, timedelta
from itertools import takewhile

from psycopg_pool import AsyncConnectionPool

from uriel_tools.schedules import rules

SCHEDULE_COLUMNS = (
    "id, owner, owner_sub, title, prompt, rrule, dtstart, tz, tools, status, next_run_at, caldav_uid"
)


async def get_tz(pool: AsyncConnectionPool, owner: str, default: str) -> str:
    async with pool.connection() as conn:
        row = await (
            await conn.execute("SELECT tz FROM person_settings WHERE owner = %s", (owner,))
        ).fetchone()
    return row["tz"] if row else default


async def set_tz(pool: AsyncConnectionPool, owner: str, tz: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO person_settings (owner, tz) VALUES (%s, %s) "
            "ON CONFLICT (owner) DO UPDATE SET tz = EXCLUDED.tz, updated_at = now()",
            (owner, tz),
        )


async def insert(
    pool: AsyncConnectionPool,
    *,
    owner: str,
    owner_sub: str | None,
    title: str,
    prompt: str,
    rule: rules.Rule,
    tools: list[str],
    caldav_uid: str,
    next_run_at: datetime,
) -> dict:
    """Create a schedule, or return the one already created from the same draft (same caldav_uid)."""
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO schedules (owner, owner_sub, title, prompt, rrule, dtstart, tz, tools, caldav_uid, "
            "next_run_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (caldav_uid) DO NOTHING",
            (
                owner,
                owner_sub,
                title,
                prompt,
                rule.rrule,
                rule.dtstart,
                rule.tz,
                tools,
                caldav_uid,
                next_run_at,
            ),
        )
        return await (
            await conn.execute(
                f"SELECT {SCHEDULE_COLUMNS} FROM schedules WHERE caldav_uid = %s", (caldav_uid,)
            )
        ).fetchone()


async def active(pool: AsyncConnectionPool, owner: str) -> list[dict]:
    """The owner's active schedules, soonest first, each with its latest run's status and due time."""
    async with pool.connection() as conn:
        return await (
            await conn.execute(
                "SELECT s.id, s.title, s.prompt, s.rrule, s.dtstart, s.tz, s.tools, s.status, s.next_run_at, "
                "last.status AS last_status, last.due_at AS last_due_at FROM schedules s "
                "LEFT JOIN LATERAL (SELECT status, due_at FROM schedule_runs WHERE schedule_id = s.id "
                "ORDER BY due_at DESC LIMIT 1) last ON true "
                "WHERE s.owner = %s AND s.status = 'active' ORDER BY s.next_run_at, s.id",
                (owner,),
            )
        ).fetchall()


async def get(pool: AsyncConnectionPool, owner: str, schedule_id: int) -> dict | None:
    async with pool.connection() as conn:
        return await (
            await conn.execute(
                f"SELECT {SCHEDULE_COLUMNS} FROM schedules WHERE id = %s AND owner = %s", (schedule_id, owner)
            )
        ).fetchone()


async def runs(
    pool: AsyncConnectionPool, owner: str, schedule_id: int | None, since: datetime | None, limit: int
) -> list[dict]:
    """The owner's runs, newest first."""
    async with pool.connection() as conn:
        return await (
            await conn.execute(
                "SELECT r.id, r.schedule_id, s.title, s.tz, r.status, r.due_at, r.finished_at, r.summary, "
                "r.error FROM schedule_runs r JOIN schedules s ON s.id = r.schedule_id "
                "WHERE s.owner = %(owner)s AND (%(sid)s::bigint IS NULL OR r.schedule_id = %(sid)s) "
                "AND (%(since)s::timestamptz IS NULL OR r.due_at >= %(since)s) "
                "ORDER BY r.due_at DESC, r.id DESC LIMIT %(limit)s",
                {"owner": owner, "sid": schedule_id, "since": since, "limit": limit},
            )
        ).fetchall()


async def cancel(pool: AsyncConnectionPool, owner: str, schedule_id: int, by: str) -> bool:
    """Cancel an active schedule; False when it wasn't active."""
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "UPDATE schedules SET status = 'cancelled', cancelled_by = %s, next_run_at = NULL, "
                "mirror_dirty = true WHERE id = %s AND owner = %s AND status = 'active' RETURNING id",
                (by, schedule_id, owner),
            )
        ).fetchone()
    return row is not None


async def in_other_tz(pool: AsyncConnectionPool, owner: str, tz: str) -> list[dict]:
    async with pool.connection() as conn:
        return await (
            await conn.execute(
                "SELECT id, title, tz FROM schedules WHERE owner = %s AND status = 'active' AND tz <> %s "
                "ORDER BY id",
                (owner, tz),
            )
        ).fetchall()


async def claim_due(pool: AsyncConnectionPool, now: datetime, limit: int, lease: timedelta) -> list[dict]:
    """Claim the due occurrence of up to `limit` schedules, and fail runs whose lease expired.

    One transaction with FOR UPDATE SKIP LOCKED, so two claimers never take the same schedule, and
    UNIQUE (schedule_id, due_at) keeps an occurrence from running twice even so. Occurrences missed by more
    than their grace are recorded as skipped (and mirrored, so they don't vanish); only the latest due one can
    run. Runs are at most once: an expired lease is never handed out again, since only the finish_run call may
    have been lost and running it again could post the same answer twice.
    """
    claimed: list[dict] = []
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE schedule_runs SET status = 'failed', error = 'lease expired (gateway lost the run)', "
            "finished_at = %(now)s, lease_until = NULL, mirror_dirty = true "
            "WHERE status = 'claimed' AND lease_until < %(now)s",
            {"now": now},
        )
        due = await (
            await conn.execute(
                f"SELECT {SCHEDULE_COLUMNS} FROM schedules WHERE status = 'active' AND next_run_at <= %s "
                "ORDER BY next_run_at, id LIMIT %s FOR UPDATE SKIP LOCKED",
                (now, limit),
            )
        ).fetchall()
        for s in due:
            rule = (s["rrule"], s["dtstart"], s["tz"])
            missed = [s["next_run_at"]]
            missed += takewhile(lambda at: at <= now, rules.occurrences(*rule, s["next_run_at"]))
            latest = missed.pop()
            if now - latest > rules.grace(*rule, latest):
                missed.append(latest)
                latest = None
            for at in missed:
                await conn.execute(
                    "INSERT INTO schedule_runs (schedule_id, due_at, status, finished_at, mirror_dirty) "
                    "VALUES (%s, %s, 'skipped', %s, true) ON CONFLICT (schedule_id, due_at) DO NOTHING",
                    (s["id"], at, now),
                )
            if latest is not None:
                run = await (
                    await conn.execute(
                        "INSERT INTO schedule_runs (schedule_id, due_at, lease_until) VALUES (%s, %s, %s) "
                        "ON CONFLICT (schedule_id, due_at) DO NOTHING RETURNING id",
                        (s["id"], latest, now + lease),
                    )
                ).fetchone()
                if run is not None:
                    claimed.append(
                        {"run_id": run["id"], "due_at": latest}
                        | {k: s[k] for k in ("owner", "owner_sub", "title", "prompt", "tools", "tz")}
                    )
            following = next(rules.occurrences(*rule, now), None)
            if following is None:
                await conn.execute(
                    "UPDATE schedules SET status = 'finished', next_run_at = NULL, mirror_dirty = true "
                    "WHERE id = %s",
                    (s["id"],),
                )
            else:
                await conn.execute(
                    "UPDATE schedules SET next_run_at = %s WHERE id = %s", (following, s["id"])
                )
    return claimed


async def finish(
    pool: AsyncConnectionPool,
    run_id: int,
    status: str,
    summary: str | None,
    error: str | None,
    talk_message_id: int | None,
    now: datetime,
) -> dict | None:
    """Record a claimed run's outcome. A finished run keeps its outcome; None when there's no such run."""
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "UPDATE schedule_runs SET status = %s, summary = %s, error = %s, talk_message_id = %s, "
                "finished_at = %s, lease_until = NULL, mirror_dirty = true "
                "WHERE id = %s AND status = 'claimed' RETURNING id, status",
                (status, summary, error, talk_message_id, now, run_id),
            )
        ).fetchone()
        if row is not None:
            return {"run_id": row["id"], "status": row["status"], "finished": True}
        row = await (
            await conn.execute("SELECT id, status FROM schedule_runs WHERE id = %s", (run_id,))
        ).fetchone()
    return None if row is None else {"run_id": row["id"], "status": row["status"], "finished": False}


# The calendar mirror.


async def dirty_schedules(pool: AsyncConnectionPool) -> list[dict]:
    async with pool.connection() as conn:
        return await (
            await conn.execute(
                f"SELECT {SCHEDULE_COLUMNS}, cancelled_by FROM schedules WHERE mirror_dirty ORDER BY id"
            )
        ).fetchall()


async def dirty_runs(pool: AsyncConnectionPool) -> list[dict]:
    async with pool.connection() as conn:
        return await (
            await conn.execute(
                "SELECT r.id, r.status, r.summary, r.error, r.finished_at, s.owner, s.title, s.caldav_uid "
                "FROM schedule_runs r JOIN schedules s ON s.id = r.schedule_id "
                "WHERE r.mirror_dirty ORDER BY r.id"
            )
        ).fetchall()


async def schedule_mirrored(pool: AsyncConnectionPool, schedule_id: int, status: str) -> None:
    """Clear the flag only if the schedule is still as pushed: a change meanwhile gets pushed next time."""
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE schedules SET mirror_dirty = false WHERE id = %s AND status = %s", (schedule_id, status)
        )


async def run_mirrored(pool: AsyncConnectionPool, run_id: int, status: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE schedule_runs SET mirror_dirty = false WHERE id = %s AND status = %s", (run_id, status)
        )


async def save_etag(pool: AsyncConnectionPool, schedule_id: int, etag: str | None) -> None:
    async with pool.connection() as conn:
        await conn.execute("UPDATE schedules SET caldav_etag = %s WHERE id = %s", (etag, schedule_id))


async def edited_in_nextcloud(pool: AsyncConnectionPool, owner: str, uid: str, etag: str | None) -> bool:
    """Mark the owner's active schedule with this event UID for a push when the event's ETag isn't the one
    Uriel's last PUT got; True when marked. Without both ETags there's no telling an edit from Uriel's own
    write, so nothing is marked: putting it back every pull would loop."""
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "UPDATE schedules SET mirror_dirty = true WHERE owner = %(owner)s AND caldav_uid = %(uid)s "
                "AND status = 'active' AND caldav_etag IS NOT NULL AND %(etag)s::text IS NOT NULL "
                "AND caldav_etag <> %(etag)s RETURNING id",
                {"owner": owner, "uid": uid, "etag": etag},
            )
        ).fetchone()
    return row is not None


async def calendars(pool: AsyncConnectionPool, owner: str) -> dict | None:
    async with pool.connection() as conn:
        return await (await conn.execute("SELECT * FROM caldav_sync WHERE owner = %s", (owner,))).fetchone()


async def save_calendars(pool: AsyncConnectionPool, owner: str, calendar_href: str, tasks_href: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO caldav_sync (owner, calendar_href, tasks_href) VALUES (%s, %s, %s) "
            "ON CONFLICT (owner) DO UPDATE SET calendar_href = EXCLUDED.calendar_href, "
            "tasks_href = EXCLUDED.tasks_href, sync_token = NULL",
            (owner, calendar_href, tasks_href),
        )


async def forget_calendars(pool: AsyncConnectionPool, owner: str) -> None:
    """The calendars are gone from Nextcloud: make them again on the next push, with every active schedule."""
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("DELETE FROM caldav_sync WHERE owner = %s", (owner,))
        await conn.execute(
            "UPDATE schedules SET mirror_dirty = true WHERE owner = %s AND status = 'active'", (owner,)
        )


async def all_calendars(pool: AsyncConnectionPool) -> list[dict]:
    async with pool.connection() as conn:
        return await (await conn.execute("SELECT * FROM caldav_sync ORDER BY owner")).fetchall()


async def set_sync_token(pool: AsyncConnectionPool, owner: str, token: str) -> None:
    async with pool.connection() as conn:
        await conn.execute("UPDATE caldav_sync SET sync_token = %s WHERE owner = %s", (token, owner))


async def cancel_from_nextcloud(pool: AsyncConnectionPool, owner: str, caldav_uid: str) -> str | None:
    """Cancel the owner's active schedule with this event UID; its title, or None when there's none."""
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "UPDATE schedules SET status = 'cancelled', cancelled_by = 'nextcloud', next_run_at = NULL, "
                "mirror_dirty = true WHERE owner = %s AND caldav_uid = %s AND status = 'active' "
                "RETURNING title",
                (owner, caldav_uid),
            )
        ).fetchone()
    return row["title"] if row else None
