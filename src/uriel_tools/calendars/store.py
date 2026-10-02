"""Per-person calendar settings and the record behind "undo". A function that takes `owner` touches that
person's rows only."""

from datetime import datetime, timedelta

from psycopg_pool import AsyncConnectionPool

KEEP = timedelta(days=1)  # far longer than the undo window; rows only need to outlive it


async def get_default(pool: AsyncConnectionPool, owner: str) -> str | None:
    async with pool.connection() as conn:
        row = await (
            await conn.execute("SELECT default_calendar FROM person_settings WHERE owner = %s", (owner,))
        ).fetchone()
    return row["default_calendar"] if row else None


async def set_default(pool: AsyncConnectionPool, owner: str, href: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO person_settings (owner, default_calendar) VALUES (%s, %s) "
            "ON CONFLICT (owner) DO UPDATE SET default_calendar = EXCLUDED.default_calendar, "
            "updated_at = now()",
            (owner, href),
        )


async def record_created(pool: AsyncConnectionPool, owner: str, item_href: str, at: datetime) -> None:
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM calendar_created WHERE created_at < %s", (at - KEEP,))
        await conn.execute(
            "INSERT INTO calendar_created (owner, item_path, created_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (owner, item_path) DO UPDATE SET created_at = EXCLUDED.created_at",
            (owner, item_href, at),
        )


async def created_since(pool: AsyncConnectionPool, owner: str, item_href: str, since: datetime) -> bool:
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT 1 FROM calendar_created WHERE owner = %s AND item_path = %s AND created_at >= %s",
                (owner, item_href, since),
            )
        ).fetchone()
    return row is not None
