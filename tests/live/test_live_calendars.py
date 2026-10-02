"""People's calendars against the real Nextcloud: what uriel sees of a calendar a test user shares with it.
Needs URIEL_LIVE_NC_URL, URIEL_LIVE_NC_APP_PASSWORD (uriel's), URIEL_LIVE_NC_TEST_USER (default uriel-test)
and URIEL_LIVE_NC_TEST_PASSWORD. Deletes what it makes."""

import os
import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from uriel_tools.calendars import ical
from uriel_tools.calendars.visibility import visible_to
from uriel_tools.gating import Caller
from uriel_tools.schedules import ics
from uriel_tools.schedules.caldav import CalDav, Changed

pytestmark = pytest.mark.live
NC_URL = os.environ.get("URIEL_LIVE_NC_URL", "")
URIEL_PASSWORD = os.environ.get("URIEL_LIVE_NC_APP_PASSWORD", "")
TEST_USER = os.environ.get("URIEL_LIVE_NC_TEST_USER", "uriel-test")
TEST_PASSWORD = os.environ.get("URIEL_LIVE_NC_TEST_PASSWORD", "")
GROUP = "uriel-family"
TZ = "America/Toronto"


@pytest.fixture
async def shared():
    if not (NC_URL and URIEL_PASSWORD and TEST_PASSWORD):
        pytest.skip("URIEL_LIVE_NC_URL / _APP_PASSWORD / _TEST_PASSWORD not set")
    person = CalDav(NC_URL, TEST_USER, TEST_PASSWORD)
    uriel = CalDav(NC_URL, "uriel", URIEL_PASSWORD)
    slug = f"uriel-live-{uuid.uuid4().hex[:8]}"
    href = await person.make_calendar(slug, "Live check", "VEVENT", also=("VTODO",))
    await person.share(href, "uriel")
    yield person, uriel, slug
    r = await person._request("DELETE", href)
    assert r.status_code in (204, 404)
    await person.aclose()
    await uriel.aclose()


async def test_uriel_sees_the_share_with_its_owner_and_sharees(shared):
    person, uriel, slug = shared
    cals = [c for c in await uriel.calendars() if slug in c.href]
    assert len(cals) == 1, [c.href for c in await uriel.calendars()]
    [cal] = cals
    assert cal.owner == TEST_USER and not cal.read_only  # a sharee is never shown oc:invite
    me = Caller(TEST_USER, frozenset(), "live")
    assert [(v.info.href, v.mine) for v in visible_to(cals, me)] == [(cal.href, True)]
    assert visible_to(cals, Caller("someone-else", frozenset({"family"}), "live")) == []


async def test_a_calendar_uriel_owns_and_shares_with_a_group_is_that_groups():
    if not (NC_URL and URIEL_PASSWORD):
        pytest.skip("URIEL_LIVE_NC_URL / _APP_PASSWORD not set")
    uriel = CalDav(NC_URL, "uriel", URIEL_PASSWORD)
    slug = f"uriel-live-{uuid.uuid4().hex[:8]}"
    href = await uriel.make_calendar(slug, "Live group check", "VEVENT", also=("VTODO",))
    try:
        await uriel.share_with_group(href, GROUP)  # uriel's only member: no family member is touched
        [cal] = [c for c in await uriel.calendars() if slug in c.href]
        assert cal.owner == "uriel" and f"groups/{GROUP}" in cal.sharees, cal
        member = Caller("someone", frozenset({GROUP}), "live")
        assert [(v.info.href, v.mine) for v in visible_to([cal], member)] == [(cal.href, False)]
        assert visible_to([cal], Caller("someone", frozenset({"family"}), "live")) == []
    finally:
        r = await uriel._request("DELETE", href)
        await uriel.aclose()
        assert r.status_code in (204, 404)


async def test_add_list_move_and_delete_an_event(shared):
    person, uriel, slug = shared
    [cal] = [c for c in await uriel.calendars() if slug in c.href]
    now = datetime.now(UTC)
    day = (now + timedelta(days=2)).date()
    uid = f"uriel-{uuid.uuid4().hex}"
    href = f"{cal.href}{uid}.ics"
    text = ics.appointment(
        uid=uid,
        title="Live, check; one",
        notes="n",
        location="",
        day=day,
        start=time(12),
        end=time(13),
        tz=TZ,
        alarm_minutes=10,
        owner=TEST_USER,
        now=now,
    )
    await uriel.write(href, text, new=True)
    begin = datetime.combine(day, time()).replace(tzinfo=UTC) - timedelta(days=1)
    items = await uriel.events(cal.href, begin, begin + timedelta(days=3))
    [entry] = [e for i in items for e in ical.entries(i.ics, TZ)]
    assert entry.title == "Live, check; one" and entry.start.astimezone(UTC).hour in (16, 17)
    got = await uriel.get(href)
    moved = ical.retime(got.ics, start=datetime.combine(day, time(14)), end=None, tz=TZ, now=now)
    await uriel.write(href, moved, etag=got.etag)
    with pytest.raises(Changed):
        await uriel.write(href, moved, etag=got.etag)  # the ETag is stale now
    await uriel.remove(href, etag=(await uriel.get(href)).etag)
    assert await uriel.get(href) is None


async def test_a_series_comes_back_expanded_with_recurrence_ids(shared):
    person, uriel, slug = shared
    [cal] = [c for c in await uriel.calendars() if slug in c.href]
    day = (datetime.now(UTC) + timedelta(days=1)).date()
    series = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//live//EN\r\nBEGIN:VEVENT\r\nUID:live-series\r\n"
        f"DTSTAMP:20260101T000000Z\r\nSUMMARY:Piano\r\nDTSTART;TZID={TZ}:{day:%Y%m%d}T160000\r\n"
        f"DTEND;TZID={TZ}:{day:%Y%m%d}T170000\r\nRRULE:FREQ=DAILY;COUNT=3\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    await uriel.write(f"{cal.href}live-series.ics", series, new=True)
    begin = datetime.combine(day, time()).replace(tzinfo=UTC)
    items = await uriel.events(cal.href, begin, begin + timedelta(days=4))
    entries = [e for i in items for e in ical.entries(i.ics, TZ)]
    assert len(entries) == 3 and all(e.recurring for e in entries)


async def test_a_task_with_an_alarm_and_completing_it(shared):
    person, uriel, slug = shared
    [cal] = [c for c in await uriel.calendars() if slug in c.href]
    now = datetime.now(UTC)
    uid = f"uriel-{uuid.uuid4().hex}"
    href = f"{cal.href}{uid}.ics"
    due = (now + timedelta(minutes=2)).astimezone(ZoneInfo(TZ)).replace(tzinfo=None, second=0, microsecond=0)
    text = ics.reminder(uid=uid, title="Live task", notes="", due=due, tz=TZ, owner=TEST_USER, now=now)
    await uriel.write(href, text, new=True)
    [open_task] = [i for i in await uriel.open_tasks(cal.href) if i.href.endswith(f"{uid}.ics")]
    await uriel.write(href, ical.complete(open_task.ics, now), etag=open_task.etag)
    assert all(not i.href.endswith(f"{uid}.ics") for i in await uriel.open_tasks(cal.href))
