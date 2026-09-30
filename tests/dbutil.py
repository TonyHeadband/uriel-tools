from datetime import UTC, datetime

from uriel_tools.rag import store
from uriel_tools.rag.shares import Mount
from uriel_tools.rag.sources import normalise

DIM = 1024


def unit(i: int) -> list[float]:
    v = [0.0] * DIM
    v[i % DIM] = 1.0
    return v


def meta(path: str, etag: str = "e1", fileid: int | None = 1) -> store.DocumentMeta:
    return store.DocumentMeta(path, etag, fileid, "text/plain", 10, datetime(2026, 3, 2, tzinfo=UTC))


async def folders(pool, *specs: dict) -> list[store.Folder]:
    """Sync folders from dicts: {"path": mount, "owner": uid, "share_with": [...]}; the Nextcloud uid is
    `nextcloud_user` when given (an authelia-<sub> account), else `owner`."""
    mounts = [
        Mount(
            normalise(s["path"]),
            (s.get("nextcloud_user") or s["owner"],),
            s.get("shared_by", s["owner"]),
            tuple(s.get("share_with", ())),
        )
        for s in specs
    ]
    all_folders, _ = await store.sync_mounts(pool, mounts)
    return [next(f for f in all_folders if f.mount == m.path) for m in mounts]


async def set_state(pool, folder_id: int, state: str) -> None:
    async with pool.connection() as conn:
        await conn.execute("UPDATE folders SET state = %s WHERE id = %s", (state, folder_id))


async def doc(
    pool, folder: store.Folder, path: str, texts: list[str], status: str = "indexed", model: str = "m"
) -> int:
    chunks = [store.NewChunk(1, "text", t, unit(i)) for i, t in enumerate(texts)]
    return await store.save_document(pool, folder.id, meta(path), status, chunks, model)
