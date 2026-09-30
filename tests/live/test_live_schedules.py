"""The calendar mirror and web search against the real services. Needs, for the calendar test,
URIEL_LIVE_NC_URL, URIEL_LIVE_NC_APP_PASSWORD (the `uriel` account's) and URIEL_LIVE_NC_SHARE_WITH (a test
user, never a real family member); for the search test, URIEL_LIVE_SEARXNG_URL."""

import os
import uuid
from datetime import UTC, datetime
from xml.etree import ElementTree as ET

import pytest

from uriel_tools.schedules import ics
from uriel_tools.schedules.caldav import CalDav, InvalidSyncToken
from uriel_tools.search import WebSearch

pytestmark = pytest.mark.live
NC_URL = os.environ.get("URIEL_LIVE_NC_URL", "")  # e.g. https://cloud.example.com
NC_USER = os.environ.get("URIEL_LIVE_NC_USER", "uriel")
NC_PASSWORD = os.environ.get("URIEL_LIVE_NC_APP_PASSWORD", "")
SHARE_WITH = os.environ.get("URIEL_LIVE_NC_SHARE_WITH", "")
SEARXNG = os.environ.get("URIEL_LIVE_SEARXNG_URL", "")  # e.g. https://searxng.example.com
DAV, OC = "{DAV:}", "{http://owncloud.org/ns}"


@pytest.fixture
async def dav():
    if not (NC_URL and NC_PASSWORD and SHARE_WITH):
        pytest.skip("URIEL_LIVE_NC_URL / _APP_PASSWORD / _SHARE_WITH not set")
    client = CalDav(NC_URL, NC_USER, NC_PASSWORD)
    yield client
    await client.aclose()


async def shared_read_write_with(dav: CalDav, href: str, user: str) -> bool:
    """Reads the share back through oc:invite: a 2xx from the share POST doesn't prove it took effect."""
    r = await dav._xml(
        "PROPFIND",
        href,
        '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:prop><oc:invite/></d:prop></d:propfind>',
        Depth="0",
    )
    assert r.status_code == 207, r.status_code
    for invitee in ET.fromstring(r.content).iter(f"{OC}user"):
        href_el = invitee.find(f"{DAV}href")
        if href_el is not None and (href_el.text or "").rstrip("/").endswith(f"/{user}"):
            access = invitee.find(f"{OC}access")
            return access is not None and access.find(f"{OC}read-write") is not None
    return False


async def test_make_share_sync_a_deletion_and_make_again(dav):
    now = datetime.now(UTC)
    slug = f"uriel-live-{uuid.uuid4().hex[:8]}"
    href = await dav.make_calendar(slug, "Uriel · live test", "VEVENT")
    try:
        await dav.share(href, SHARE_WITH)
        assert await shared_read_write_with(dav, href, SHARE_WITH)
        uid = f"uriel-{uuid.uuid4().hex}"
        event = ics.event(
            uid=uid,
            schedule_id=0,
            title="Live test",
            prompt="Nothing: a uriel-tools live test",
            tools=[],
            rrule="FREQ=DAILY",
            dtstart=datetime(2026, 10, 1, 9, 0),
            tz="America/Toronto",  # no VTIMEZONE: Nextcloud must resolve the IANA name itself
            now=now,
        )
        await dav.put(href, uid, event)
        first = await dav.sync(href, None)
        assert first.token and first.deleted == []
        # Nextcloud reads only the trailing number, so a token with junk appended (or from the future)
        # is quietly accepted; only a token outside its format is refused.
        with pytest.raises(InvalidSyncToken):
            await dav.sync(href, "not-a-sabre-token")
        await dav.delete(href, uid)
        later = await dav.sync(href, first.token)
        assert later.deleted == [uid]
    finally:
        await dav._request("DELETE", href)
    # A person deleting the calendar sends it to Nextcloud's trash bin: making it again must still work.
    assert await dav.make_calendar(slug, "Uriel · live test", "VEVENT") == href
    try:
        await dav.put(href, "uriel-again", event)
    finally:
        await dav._request("DELETE", href)


async def test_one_real_search():
    if not SEARXNG:
        pytest.skip("URIEL_LIVE_SEARXNG_URL not set")
    web = WebSearch(SEARXNG)
    try:
        out = await web.search("Nextcloud", max_results=5)
    finally:
        await web.aclose()
    assert out["results"] and all(r["url"].startswith("http") for r in out["results"])
