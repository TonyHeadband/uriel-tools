"""The wire contract from docs/contract.md, spoken as raw JSON-RPC so no client library can paper over it."""

import json

import httpx

from uriel_tools.app import build_app
from uriel_tools.gating import ToolPolicy

POLICY = ToolPolicy.model_validate(
    {
        "tools": {
            "homelab_status": {"groups": ["admins"], "category": "homelab"},
            "search_documents": {"groups": ["family"], "category": "documents"},
        }
    }
)
HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
    "x-api-key": "k1",
}


def rpc_result(r: httpx.Response) -> dict:
    assert r.status_code == 200, r.text
    if r.headers["content-type"].startswith("text/event-stream"):
        data = [line[5:] for line in r.text.splitlines() if line.startswith("data:")]
        return json.loads(data[-1])["result"]
    return r.json()["result"]


async def session(h: httpx.AsyncClient) -> dict:
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    }
    r = await h.post("/mcp", json=init, headers=HEADERS)
    rpc_result(r)
    headers = HEADERS | {"mcp-session-id": r.headers["mcp-session-id"]}
    await h.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
    return headers


async def call(method: str, params: dict, policy: ToolPolicy = POLICY) -> dict:
    app = build_app(policy, ["k1"])
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as h:
            headers = await session(h)
            body = {"jsonrpc": "2.0", "id": 2, "method": method, "params": params}
            return rpc_result(await h.post("/mcp", json=body, headers=headers))


async def test_list_is_filtered_by_meta_groups():
    res = await call("tools/list", {"_meta": {"user": "kid", "groups": ["family"], "request_id": "r1"}})
    assert [t["name"] for t in res["tools"]] == ["search_documents"]


async def test_list_carries_each_tools_category():
    res = await call("tools/list", {"_meta": {"user": "kid", "groups": ["family"], "request_id": "r1"}})
    assert [t["_meta"]["uriel"] for t in res["tools"]] == [
        {"category": "documents", "hidden": False, "unattended": False}
    ]


async def test_call_is_gated_by_meta_groups():
    meta = {"user": "dad", "groups": ["admins"], "request_id": "r2"}
    ok = await call("tools/call", {"name": "homelab_status", "arguments": {}, "_meta": meta})
    assert not ok["isError"]
    meta = {"user": "kid", "groups": ["family"], "request_id": "r3"}
    denied = await call("tools/call", {"name": "homelab_status", "arguments": {}, "_meta": meta})
    assert denied["isError"]
    assert "not permitted for kid" in denied["content"][0]["text"]


KID = {"user": "kid", "groups": ["family"], "request_id": "r5"}


async def test_list_carries_hidden_and_unattended_next_to_the_category():
    policy = ToolPolicy.model_validate(
        {
            "tools": {
                "search_documents": {"groups": ["family"], "category": "documents", "unattended": True},
                "memory_context": {"groups": ["family"], "category": "memory", "hidden": True},
            }
        }
    )
    res = await call("tools/list", {"_meta": KID}, policy)
    uriel = {t["name"]: t["_meta"]["uriel"] for t in res["tools"]}
    assert uriel == {
        "search_documents": {"category": "documents", "hidden": False, "unattended": True},
        "memory_context": {"category": "memory", "hidden": True, "unattended": False},
    }


async def test_the_runner_tools_are_hidden_and_only_for_uriel_internal():
    policy = ToolPolicy.model_validate(
        {
            "tools": {
                "list_schedules": {"groups": ["family"], "category": "schedules", "unattended": True},
                "claim_due_runs": {"groups": ["uriel-internal"], "category": "schedules", "hidden": True},
                "finish_run": {"groups": ["uriel-internal"], "category": "schedules", "hidden": True},
            }
        }
    )
    gateway = {"user": "uriel-gateway", "groups": ["uriel-internal"], "request_id": "r6"}
    res = await call("tools/list", {"_meta": gateway}, policy)
    uriel = {t["name"]: t["_meta"]["uriel"] for t in res["tools"]}
    assert sorted(uriel) == ["claim_due_runs", "finish_run"]
    assert all(u["hidden"] is True and u["unattended"] is False for u in uriel.values())
    denied = await call("tools/call", {"name": "claim_due_runs", "arguments": {}, "_meta": KID}, policy)
    assert denied["isError"] and "not permitted for kid" in denied["content"][0]["text"]
