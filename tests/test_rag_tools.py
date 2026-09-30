import psycopg
import pytest
from fastmcp import Client

from tests.dbutil import folders, meta
from tests.fakes.models import HashEmbedder, hash_vector
from uriel_tools.app import build_mcp
from uriel_tools.db import make_pool
from uriel_tools.gating import ToolPolicy
from uriel_tools.rag import store
from uriel_tools.tools import Rag

POLICY = ToolPolicy.model_validate(
    {
        "tools": {
            "search_documents": {"groups": ["family"], "category": "test"},
            "ocr_now": {"groups": ["family"], "category": "test"},
        }
    }
)
ALICE = {"user": "alice", "groups": ["family"], "request_id": "r1"}
BOB = {"user": "bob", "groups": ["family"], "request_id": "r2"}


async def call(rag, tool, args, meta_):
    async with Client(build_mcp(POLICY, rag)) as c:
        return await c.call_tool(tool, args, meta=meta_, raise_on_error=False)


@pytest.mark.db
async def test_search_and_ocr_now(pool):
    [f] = await folders(pool, {"owner": "alice", "path": "/Documents"})
    text = "Contratto affitto via Roma"
    await store.save_document(
        pool,
        f.id,
        meta("/Documents/affitto.pdf"),
        "indexed",
        [store.NewChunk(1, "text", text, hash_vector(text))],
        "hash-embed",
    )
    await store.save_document(pool, f.id, meta("/Documents/scan.png"), "awaiting_ocr", [], "hash-embed")
    rag = Rag(pool, HashEmbedder(), "https://cloud.example")

    res = await call(rag, "search_documents", {"query": "affitto"}, ALICE)
    assert not res.is_error
    assert res.structured_content["hits"][0]["url"] == "https://cloud.example/f/1"
    assert res.structured_content["awaiting_ocr"] == 1
    assert (await call(rag, "search_documents", {"query": "affitto"}, BOB)).structured_content["hits"] == []

    queued = await call(rag, "ocr_now", {"path": "/Documents/scan.png"}, ALICE)
    assert queued.structured_content == {"queued": True, "jobs_ahead": 0}
    refused = await call(rag, "ocr_now", {"path": "/Documents/scan.png"}, BOB)
    assert refused.structured_content["queued"] is False


async def test_tools_without_rag_say_not_configured():
    res = await call(None, "search_documents", {"query": "x"}, ALICE)
    assert res.is_error and "not configured" in res.content[0].text


async def test_database_down_is_a_readable_error():
    rag = Rag(make_pool("postgresql://nobody@127.0.0.1:1/none", timeout=1), HashEmbedder())
    try:
        res = await call(rag, "search_documents", {"query": "x"}, ALICE)
        assert res.is_error and "unavailable" in res.content[0].text
    finally:
        # ready() opened the pool before it failed to connect; close it explicitly here (the tool code
        # must not, since a transient outage would otherwise permanently disable document search).
        await rag.pool.close()


async def test_rag_ready_retries_after_failure():
    rag = Rag(make_pool("postgresql://nobody@127.0.0.1:1/none", timeout=1), HashEmbedder())
    try:
        with pytest.raises(psycopg.Error):
            await rag.ready()
        assert rag._ready is False
    finally:
        await rag.pool.close()
