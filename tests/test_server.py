import asyncio

import httpx
import pytest
from fastmcp import Client
from mcp.types import PaginatedRequestParams

from uriel_tools.app import build_app, build_mcp
from uriel_tools.gating import ToolPolicy

POLICY = ToolPolicy.model_validate(
    {
        "tools": {
            "homelab_status": {"groups": ["admins"], "category": "test"},
            "search_documents": {"groups": ["family"], "category": "test"},
            "door_camera_last_event": {"groups": ["family"], "category": "test"},
        }
    }
)
ADMIN = {"user": "dad", "groups": ["admins", "family"], "request_id": "r1"}
FAMILY = {"user": "kid", "groups": ["family"], "request_id": "r2"}
NOBODY = {"user": "guest", "groups": [], "request_id": "r3"}

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


async def listed(meta):
    async with Client(build_mcp(POLICY)) as c:
        res = await c.session.list_tools(params=PaginatedRequestParams(_meta=meta))
        return sorted(t.name for t in res.tools)


async def test_tool_list_is_filtered_by_group():
    assert await listed(ADMIN) == ["door_camera_last_event", "homelab_status", "search_documents"]
    assert await listed(FAMILY) == ["door_camera_last_event", "search_documents"]
    assert await listed(NOBODY) == []


async def test_allowed_call_returns_data():
    async with Client(build_mcp(POLICY)) as c:
        res = await c.call_tool("homelab_status", {}, meta=ADMIN)
        assert not res.is_error
        assert "nodes" in res.structured_content


async def test_denied_call_is_a_readable_error():
    async with Client(build_mcp(POLICY)) as c:
        res = await c.call_tool("homelab_status", {}, meta=FAMILY, raise_on_error=False)
        assert res.is_error
        assert "not permitted for kid" in res.content[0].text


async def test_call_without_identity_is_denied():
    async with Client(build_mcp(POLICY)) as c:
        res = await c.call_tool("search_documents", {"query": "x"}, raise_on_error=False)
        assert res.is_error


def test_unlisted_tool_is_denied():
    assert not POLICY.allowed("rm_rf", {"admins"})


@pytest.mark.parametrize(("key", "status"), [(None, 401), ("wrong", 401), ("k1", 200), ("k2", 200)])
async def test_api_key_is_required(key, status):
    app = build_app(POLICY, ["k1", "k2"])
    headers = dict(MCP_HEADERS) | ({"x-api-key": key} if key else {})
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as h:
            r = await h.post("/mcp", json=INIT, headers=headers)
    assert r.status_code == status


async def test_background_jobs_run_with_the_server_and_stop_with_it():
    events: list[str] = []

    async def job(stop):
        events.append("started")
        await stop.wait()
        events.append("stopped")

    app = build_app(POLICY, ["k1"], background=[job])
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        assert events == ["started"]
    assert events == ["started", "stopped"]
