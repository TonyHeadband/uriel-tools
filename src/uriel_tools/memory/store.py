"""The `memory_notes` table: one row per note, or per skipped questionnaire question."""

from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool


@dataclass(frozen=True)
class Row:
    id: int
    section: str
    note: str | None
    skipped: bool


async def rows(pool: AsyncConnectionPool, owner: str, file: str) -> list[Row]:
    async with pool.connection() as conn:
        found = await (
            await conn.execute(
                "SELECT id, section, note, skipped FROM memory_notes "
                "WHERE owner = %s AND file = %s ORDER BY id",
                (owner, file),
            )
        ).fetchall()
    return [Row(**r) for r in found]


async def add(
    pool: AsyncConnectionPool, owner: str, file: str, section: str, note: str | None, *, clear_section: bool
) -> None:
    """Save a note, or a skip row when `note` is None. The section's old skip row goes: a note answers it."""
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM memory_notes WHERE owner = %s AND file = %s AND lower(section) = lower(%s)"
            + ("" if clear_section else " AND skipped"),
            (owner, file, section),
        )
        await conn.execute(
            "INSERT INTO memory_notes (owner, file, section, note, skipped) VALUES (%s, %s, %s, %s, %s)",
            (owner, file, section, note, note is None),
        )


async def delete(pool: AsyncConnectionPool, ids: list[int]) -> None:
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM memory_notes WHERE id = ANY(%s)", (ids,))
