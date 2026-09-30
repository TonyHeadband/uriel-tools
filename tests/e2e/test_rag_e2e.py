import asyncio
import json
import os
import time

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from tests import filegen

pytestmark = pytest.mark.e2e

MCP = os.environ.get("URIEL_E2E_MCP_URL", "")
EVENTS = os.environ.get("URIEL_E2E_EVENTS_URL", "")
WEBDAV = os.environ.get("URIEL_E2E_WEBDAV_URL", "")
NODE = "OCP\\Files\\Events\\Node\\"


@pytest.fixture(autouse=True)
def _configured():
    if not (MCP and EVENTS and WEBDAV):
        pytest.skip("URIEL_E2E_*_URL not set")


def meta(user, groups):
    return {"user": user, "groups": groups, "request_id": "e2e"}


async def tool(name, args, user, groups=("family",)):
    transport = StreamableHttpTransport(MCP, headers={"x-api-key": "dev-mcp-key"})
    async with Client(transport) as c:
        res = await c.call_tool(name, args, meta=meta(user, list(groups)), raise_on_error=False)
    assert not res.is_error, res.content
    return res.structured_content


async def nextcloud_changes(user, path, data: bytes | None, mime="text/plain", event="NodeWrittenEvent"):
    """Change a shared file (as it appears in uriel's tree), then send the webhook Nextcloud would send for
    its owner."""
    async with httpx.AsyncClient() as h:
        url = f"{WEBDAV}/_fake/files/uriel{path}"
        r = await (
            h.put(url, content=data, headers={"content-type": mime}) if data is not None else h.delete(url)
        )
        r.raise_for_status()
        payload = {
            "event": {"class": NODE + event, "node": {"id": 1, "path": f"/{user}/files{path}"}},
            "user": {"uid": user},
        }
        r = await h.post(
            f"{EVENTS}/events/nextcloud",
            content=json.dumps(payload),
            headers={"x-uriel-source-key": "dev-events-key", "content-type": "application/json"},
        )
        assert r.status_code == 202, r.text


async def eventually(check, timeout_s=60):
    deadline = time.monotonic() + timeout_s
    while True:
        result = await check()
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(1)


async def hits(user, query, groups=("family",)):
    return (await tool("search_documents", {"query": query, "k": 10}, user, groups))["hits"]


async def titles(user, query, groups=("family",)):
    return [h["title"] for h in await hits(user, query, groups)]


async def test_a_private_share_is_searchable_by_its_owner_only():
    await nextcloud_changes("alice", "/Documents/lavatrice.txt", b"Lavatrice: programma lana a 30 gradi")
    await eventually(lambda: _has(titles("alice", "lavatrice"), "lavatrice.txt"))
    assert "lavatrice.txt" not in await titles("bob", "lavatrice")  # family, but not shared with the family
    await nextcloud_changes("alice", "/Documents/lavatrice.txt", None, event="NodeDeletedEvent")
    await eventually(lambda: _lacks(titles("alice", "lavatrice"), "lavatrice.txt"))


async def test_a_uriel_family_share_is_searchable_by_the_family():
    await nextcloud_changes("bob", "/Famiglia/caldaia.txt", b"Caldaia: premere reset per cinque secondi")
    await eventually(lambda: _has(titles("alice", "caldaia"), "caldaia.txt"))
    [hit] = [h for h in await hits("alice", "caldaia") if h["title"] == "caldaia.txt"]
    assert hit["shared_by"] == "Bob"
    # Callers outside `family` never get search_documents at all (tools.yaml); the unit tests cover a
    # family-share hidden from a caller without the group.


async def test_sharing_and_unsharing_take_effect_within_a_poll():
    async with httpx.AsyncClient() as h:
        await h.put(f"{WEBDAV}/_fake/files/uriel/Ricette/torta.txt", content=b"Torta: farina, uova, zucchero")
        share = {"id": "9", "share_type": 0, "share_with": "uriel", "uid_owner": "carol", "path": "/Ricette"}
        (await h.post(f"{WEBDAV}/_fake/shares", json=share)).raise_for_status()
        await eventually(lambda: _has(titles("carol", "torta"), "torta.txt"), timeout_s=120)
        (await h.delete(f"{WEBDAV}/_fake/shares/9")).raise_for_status()
        await eventually(lambda: _lacks(titles("carol", "torta"), "torta.txt"), timeout_s=120)


async def test_scan_is_searchable_after_ocr_now():
    await nextcloud_changes(
        "alice", "/Documents/ricevuta.png", filegen.text_png("ricevuta"), mime="image/png"
    )
    await eventually(lambda: _awaiting(tool("search_documents", {"query": "ricevuta"}, "alice")))
    queued = await tool("ocr_now", {"path": "/Documents/ricevuta.png"}, "alice")
    assert queued["queued"] is True
    hits = await eventually(lambda: _ocr_hits(tool("search_documents", {"query": "RICEVUTA 7781"}, "alice")))
    assert hits[0]["url"].startswith("https://cloud.example/f/")


async def _has(titles_coro, title):
    return title in await titles_coro


async def _lacks(titles_coro, title):
    return title not in await titles_coro


async def _awaiting(result_coro):
    return (await result_coro)["awaiting_ocr"] >= 1


async def _ocr_hits(result_coro):
    hits = [h for h in (await result_coro)["hits"] if h["source"] == "ocr"]
    return hits or None


async def call(name, args, user, groups=("family",)):
    transport = StreamableHttpTransport(MCP, headers={"x-api-key": "dev-mcp-key"})
    async with Client(transport) as c:
        return await c.call_tool(name, args, meta=meta(user, list(groups)), raise_on_error=False)


async def test_edit_saves_a_copy_in_a_writable_share_and_explains_a_read_only_one():
    async with httpx.AsyncClient() as h:
        form = filegen.form_pdf()
        for path in ("/Famiglia/iscrizione.pdf", "/Documents/iscrizione.pdf"):
            (await h.put(f"{WEBDAV}/_fake/files/uriel{path}", content=form)).raise_for_status()
        ok = await call(
            "fill_form", {"path": "/Famiglia/iscrizione.pdf", "values": {"cognome": "Rossi"}}, "alice"
        )
        assert not ok.is_error, ok.content
        assert ok.structured_content["saved"] == "/Famiglia/edit_iscrizione.pdf"
        saved = await h.get(
            f"{WEBDAV}/remote.php/dav/files/uriel/Famiglia/edit_iscrizione.pdf", auth=("uriel", "uriel-pw")
        )
        assert saved.status_code == 200 and saved.content != form
        refused = await call(
            "fill_form", {"path": "/Documents/iscrizione.pdf", "values": {"cognome": "x"}}, "alice"
        )
        assert refused.is_error and "Allow creating" in refused.content[0].text
