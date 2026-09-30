"""The `edits` and `edit_items` tables: what Uriel saved, and each draft's state."""

from psycopg_pool import AsyncConnectionPool

from uriel_tools.edit.core import Item


async def record(
    pool: AsyncConnectionPool,
    *,
    folder_id: int,
    source: str,
    output: str,
    fileid: int | None,
    etag: str,
    tool: str,
    caller: str,
    items: list[Item],
) -> int:
    async with pool.connection() as conn, conn.transaction():
        row = await (
            await conn.execute(
                "INSERT INTO edits (folder_id, source_path, output_path, fileid, etag, tool, caller) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (folder_id, source, output, fileid, etag, tool, caller),
            )
        ).fetchone()
        if items:
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO edit_items (edit_id, item, value, page, rect, approximate) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    [
                        (row["id"], i.item, i.value, i.page, list(i.rect) if i.rect else None, i.approximate)
                        for i in items
                    ],
                )
    return row["id"]


async def confirm(pool: AsyncConnectionPool, folder_id: int, output: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE edits SET status = 'confirmed', updated_at = now() "
            "WHERE folder_id = %s AND output_path = %s",
            (folder_id, output),
        )
        return cur.rowcount > 0


async def mark_changed(pool: AsyncConnectionPool, folder_id: int, etags: dict[str, str]) -> int:
    """Drafts whose etag no longer matches what Uriel wrote were edited by a person. `confirmed` is final."""
    if not etags:
        return 0
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE edits e SET status = 'changed', updated_at = now() "
            "FROM unnest(%s::text[], %s::text[]) AS r(path, etag) "
            "WHERE e.folder_id = %s AND e.output_path = r.path AND e.status = 'draft' AND e.etag <> r.etag",
            (list(etags), list(etags.values()), folder_id),
        )
        return cur.rowcount
