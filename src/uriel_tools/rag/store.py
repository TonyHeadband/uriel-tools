from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from psycopg_pool import AsyncConnectionPool

from uriel_tools.rag.shares import Mount
from uriel_tools.rag.sources import normalise

_SYNC_LOCK = 7_272_743


def vector_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


@dataclass(frozen=True)
class Folder:
    id: int
    mount: str  # path in uriel's tree; every document path under it starts with it
    owners: tuple[str, ...]  # Nextcloud uids
    shared_by: str
    share_with: tuple[str, ...]
    state: str
    can_create: bool = False
    personal: bool = False


@dataclass(frozen=True)
class DocumentMeta:
    path: str
    etag: str
    fileid: int | None
    mime: str
    size: int
    modified: datetime | None


@dataclass(frozen=True)
class NewChunk:
    page: int
    source: str
    text: str
    embedding: list[float]


def _folder(row: dict) -> Folder:
    return Folder(
        row["id"],
        row["mount"],
        tuple(row["owners"]),
        row["shared_by"],
        tuple(row["share_with"]),
        row["state"],
        row["can_create"],
        row["personal"],
    )


async def sync_mounts(pool: AsyncConnectionPool, mounts: list[Mount]) -> tuple[list[Folder], list[Folder]]:
    """Make `folders` match what is shared with uriel. Returns (all folders, the ones just added).

    A mount that is no longer shared loses its documents at once (cascade): unsharing is how an owner
    revokes access. A successful listing also proves the credential works, so every folder is `ok` again.
    """
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SYNC_LOCK,))
        known = {r["mount"] for r in await (await conn.execute("SELECT mount FROM folders")).fetchall()}
        for m in mounts:
            await conn.execute(
                "INSERT INTO folders (mount, owners, shared_by, share_with, can_create, personal, state) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'ok') "
                "ON CONFLICT (mount) DO UPDATE SET owners = EXCLUDED.owners, shared_by = EXCLUDED.shared_by, "
                "share_with = EXCLUDED.share_with, can_create = EXCLUDED.can_create, "
                "personal = EXCLUDED.personal, state = 'ok'",
                (m.path, list(m.owners), m.shared_by, list(m.share_with), m.can_create, m.personal),
            )
        await conn.execute("DELETE FROM folders WHERE NOT (mount = ANY(%s))", ([m.path for m in mounts],))
    folders = await list_folders(pool)
    return folders, [f for f in folders if f.mount not in known]


async def set_all_folders_state(pool: AsyncConnectionPool, state: str) -> None:
    async with pool.connection() as conn:
        await conn.execute("UPDATE folders SET state = %s", (state,))


async def list_folders(pool: AsyncConnectionPool) -> list[Folder]:
    async with pool.connection() as conn:
        rows = await (await conn.execute("SELECT * FROM folders ORDER BY id")).fetchall()
    return [_folder(r) for r in rows]


async def get_folder(pool: AsyncConnectionPool, folder_id: int) -> Folder | None:
    async with pool.connection() as conn:
        row = await (await conn.execute("SELECT * FROM folders WHERE id = %s", (folder_id,))).fetchone()
    return _folder(row) if row else None


async def get_document(pool: AsyncConnectionPool, folder_id: int, path: str) -> dict | None:
    async with pool.connection() as conn:
        return await (
            await conn.execute(
                "SELECT id, etag, status FROM documents WHERE folder_id = %s AND path = %s", (folder_id, path)
            )
        ).fetchone()


async def save_document(
    pool: AsyncConnectionPool,
    folder_id: int,
    meta: DocumentMeta,
    status: str,
    chunks: list[NewChunk],
    embed_model: str,
    error: str | None = None,
) -> int:
    """Upsert the document and swap its chunks in one transaction, so search never sees half a document."""
    async with pool.connection() as conn, conn.transaction():
        row = await (
            await conn.execute(
                "INSERT INTO documents (folder_id, path, fileid, etag, mime, size, modified, status, error) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (folder_id, path) DO UPDATE SET "
                "fileid = EXCLUDED.fileid, etag = EXCLUDED.etag, mime = EXCLUDED.mime, size = EXCLUDED.size, "
                "modified = EXCLUDED.modified, status = EXCLUDED.status, error = EXCLUDED.error, "
                "indexed_at = now() RETURNING id",
                (
                    folder_id,
                    meta.path,
                    meta.fileid,
                    meta.etag,
                    meta.mime,
                    meta.size,
                    meta.modified,
                    status,
                    error,
                ),
            )
        ).fetchone()
        await conn.execute("DELETE FROM chunks WHERE document_id = %s", (row["id"],))
        if chunks:
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO chunks (document_id, ord, page, source, text, embedding, embed_model) "
                    "VALUES (%s, %s, %s, %s, %s, %s::vector, %s)",
                    [
                        (row["id"], i, c.page, c.source, c.text, vector_literal(c.embedding), embed_model)
                        for i, c in enumerate(chunks)
                    ],
                )
    return row["id"]


async def mark_failed(
    pool: AsyncConnectionPool, folder_id: int, path: str, etag: str | None, error: str
) -> None:
    """Record the final failure.

    Storing the failing etag stops reconcile from retrying until the file changes.
    """
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO documents (folder_id, path, etag, status, error) "
            "VALUES (%(folder)s, %(path)s, coalesce(%(etag)s, ''), 'failed', %(error)s) "
            "ON CONFLICT (folder_id, path) DO UPDATE SET status = 'failed', error = EXCLUDED.error, "
            "etag = coalesce(%(etag)s, documents.etag)",
            {"folder": folder_id, "path": path, "etag": etag, "error": error},
        )


async def delete_documents(pool: AsyncConnectionPool, folder_id: int, path: str) -> int:
    path = normalise(path)
    async with pool.connection() as conn:
        cur = await conn.execute(
            # starts_with, not LIKE: '_' and '%' in file names are literal
            "DELETE FROM documents WHERE folder_id = %s AND (path = %s OR starts_with(path, %s))",
            (folder_id, path, path.rstrip("/") + "/"),
        )
        return cur.rowcount


async def folder_documents(pool: AsyncConnectionPool, folder_id: int) -> dict[str, dict]:
    async with pool.connection() as conn:
        rows = await (
            await conn.execute("SELECT path, etag, status FROM documents WHERE folder_id = %s", (folder_id,))
        ).fetchall()
    return {r["path"]: {"etag": r["etag"], "status": r["status"]} for r in rows}


async def stale_chunks(pool: AsyncConnectionPool, model: str, limit: int) -> list[dict]:
    async with pool.connection() as conn:
        return await (
            await conn.execute(
                "SELECT c.id, c.text, d.path, f.mount AS folder FROM chunks c "
                "JOIN documents d ON d.id = c.document_id JOIN folders f ON f.id = d.folder_id "
                "WHERE c.embed_model <> %s ORDER BY c.id LIMIT %s",
                (model, limit),
            )
        ).fetchall()


async def update_embeddings(
    pool: AsyncConnectionPool, rows: list[tuple[int, list[float]]], model: str
) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.executemany(
            "UPDATE chunks SET embedding = %s::vector, embed_model = %s WHERE id = %s",
            [(vector_literal(v), model, chunk_id) for chunk_id, v in rows],
        )


async def awaiting_ocr_count(pool: AsyncConnectionPool, ids: Iterable[str], groups: Iterable[str]) -> int:
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT count(*) AS n FROM documents d JOIN folders f ON f.id = d.folder_id "
                "WHERE d.status = 'awaiting_ocr' AND (f.owners && %s::text[] OR f.share_with && %s::text[])",
                (list(ids), list(groups)),
            )
        ).fetchone()
    return row["n"]


async def find_awaiting_ocr(pool: AsyncConnectionPool, ids: Iterable[str], path: str) -> int | None:
    """Only an owner may spend GPU time on a scan, not everyone the folder is shared with."""
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT d.folder_id FROM documents d JOIN folders f ON f.id = d.folder_id "
                "WHERE f.owners && %s::text[] AND d.path = %s AND d.status = 'awaiting_ocr' LIMIT 1",
                (list(ids), normalise(path)),
            )
        ).fetchone()
    return row["folder_id"] if row else None
