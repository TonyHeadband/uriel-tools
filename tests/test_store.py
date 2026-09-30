import pytest

from tests.dbutil import doc, folders, meta, unit
from uriel_tools.rag import store
from uriel_tools.rag.shares import Mount

pytestmark = pytest.mark.db


async def count(pool, sql, *args):
    async with pool.connection() as conn:
        return (await (await conn.execute(sql, args)).fetchone())["n"]


async def test_sync_inserts_updates_and_removes_folders(pool):
    [a] = await folders(pool, {"owner": "anthony", "path": "/Documents"})
    await doc(pool, a, "/Documents/x.txt", ["hello"])
    [a2, b] = await folders(
        pool,
        {"owner": "anthony", "path": "/Documents", "share_with": ["family"]},
        {"owner": "lil-b", "nextcloud_user": "authelia-19e1", "path": "/Scuola"},
    )
    assert a2.id == a.id and a2.share_with == ("family",)
    assert b.owners == ("authelia-19e1",)
    [only_b] = await folders(pool, {"owner": "lil-b", "nextcloud_user": "authelia-19e1", "path": "/Scuola"})
    assert only_b.id == b.id
    assert await count(pool, "SELECT count(*) AS n FROM documents") == 0  # cascaded with the unshared folder


async def test_sync_reports_new_folders_and_resumes_paused_ones(pool):
    [a] = await folders(pool, {"owner": "a", "path": "/D"})
    await store.set_all_folders_state(pool, "auth_failed")
    mounts = [Mount("/D", ("a",), "a", ()), Mount("/E", ("b",), "b", ())]
    all_folders, added = await store.sync_mounts(pool, mounts)
    assert [f.mount for f in added] == ["/E"]
    assert {f.mount: f.state for f in all_folders} == {"/D": "ok", "/E": "ok"}


async def test_save_document_replaces_chunks_atomically(pool):
    [f] = await folders(pool, {"owner": "a", "path": "/D"})
    doc_id = await doc(pool, f, "/D/x.txt", ["one", "two", "three"])
    again = await store.save_document(
        pool, f.id, meta("/D/x.txt", etag="e2"), "indexed", [store.NewChunk(1, "text", "four", unit(4))], "m"
    )
    assert again == doc_id
    assert await count(pool, "SELECT count(*) AS n FROM chunks WHERE document_id = %s", doc_id) == 1
    assert (await store.get_document(pool, f.id, "/D/x.txt"))["etag"] == "e2"


async def test_delete_documents_removes_the_subtree_only(pool):
    [f] = await folders(pool, {"owner": "a", "path": "/D"})
    for p in ("/D/a_b/x.pdf", "/D/a_b2/y.pdf", "/D/axb/z.pdf", "/D/100%/w.pdf", "/D/100%x/v.pdf"):
        await doc(pool, f, p, ["t"])
    assert await store.delete_documents(pool, f.id, "/D/a_b") == 1
    assert await store.delete_documents(pool, f.id, "/D/100%") == 1
    assert sorted(await store.folder_documents(pool, f.id)) == [
        "/D/100%x/v.pdf",
        "/D/a_b2/y.pdf",
        "/D/axb/z.pdf",
    ]


async def test_mark_failed_keeps_or_sets_the_etag(pool):
    [f] = await folders(pool, {"owner": "a", "path": "/D"})
    await doc(pool, f, "/D/x.txt", ["t"])
    await store.mark_failed(pool, f.id, "/D/x.txt", None, "boom")
    got = await store.get_document(pool, f.id, "/D/x.txt")
    assert (got["status"], got["etag"]) == ("failed", "e1")
    await store.mark_failed(pool, f.id, "/D/new.txt", "e9", "boom")
    assert (await store.get_document(pool, f.id, "/D/new.txt"))["etag"] == "e9"


async def test_stale_chunks_and_update_embeddings(pool):
    [f] = await folders(pool, {"owner": "a", "path": "/D"})
    await doc(pool, f, "/D/x.txt", ["old text"], model="old")
    [row] = await store.stale_chunks(pool, "new", 10)
    assert (row["text"], row["path"], row["folder"]) == ("old text", "/D/x.txt", "/D")
    await store.update_embeddings(pool, [(row["id"], unit(7))], "new")
    assert await store.stale_chunks(pool, "new", 10) == []


async def test_awaiting_ocr_visibility(pool):
    mine, shared, other = await folders(
        pool,
        {"owner": "a", "path": "/Mine"},
        {"owner": "b", "path": "/Shared", "share_with": ["family"]},
        {"owner": "c", "path": "/Other"},
    )
    for f in (mine, shared, other):
        await doc(pool, f, f.mount + "/scan.png", [], status="awaiting_ocr")
    assert await store.awaiting_ocr_count(pool, ["a"], ["family"]) == 2
    assert await store.awaiting_ocr_count(pool, ["a"], []) == 1
    assert await store.find_awaiting_ocr(pool, ["a"], "/Mine/scan.png") == mine.id
    assert await store.find_awaiting_ocr(pool, ["a"], "/Shared/scan.png") is None  # shared, but not the owner
