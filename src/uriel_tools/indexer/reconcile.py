from psycopg_pool import AsyncConnectionPool

from uriel_tools.edit import records
from uriel_tools.edit.names import is_draft
from uriel_tools.rag import jobs, store
from uriel_tools.rag.store import Folder
from uriel_tools.rag.webdav import NotFound, WebDav


async def reconcile(pool: AsyncConnectionPool, dav: WebDav, folder: Folder) -> tuple[int, int]:
    """Catch up on missed webhooks: queue new or changed files, and deletions for vanished ones.

    A document that failed keeps its failing etag, so it is only retried once the file changes.
    """
    known = await store.folder_documents(pool, folder.id)
    if folder.personal:  # a `Uriel` folder holds personal items, not documents; drop any indexed before 0.4.0
        for path in known:
            await jobs.enqueue(pool, "delete", folder.id, path)
        return 0, len(known)
    remote = {}
    try:
        async for entry in dav.walk(folder.mount):
            remote[entry.path] = entry.etag
    except NotFound:
        try:
            await dav.stat(folder.mount)
        except NotFound:
            pass  # the shared folder itself was deleted or moved out of the share: everything under it goes
        else:
            raise  # a subfolder vanished mid-walk while the root still exists: retry the reconcile later
    # A draft whose etag moved since Uriel wrote it was edited by a person.
    await records.mark_changed(pool, folder.id, {p: e for p, e in remote.items() if is_draft(p)})
    queued = 0
    for path, etag in remote.items():
        if path not in known or known[path]["etag"] != etag:
            await jobs.enqueue(pool, "index", folder.id, path)
            queued += 1
    gone = [path for path in known if path not in remote]
    for path in gone:
        await jobs.enqueue(pool, "delete", folder.id, path)
    return queued, len(gone)
