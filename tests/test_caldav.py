from datetime import UTC, datetime

import httpx
import pytest

from tests.fakes.caldav import FakeCalDav, etag
from uriel_tools.rag.webdav import AuthFailed, Unreachable, WebDavError
from uriel_tools.schedules import ics
from uriel_tools.schedules.caldav import (
    CalDav,
    CalendarInfo,
    CalendarMissing,
    Changed,
    InvalidSyncToken,
    Item,
    parse_calendars,
    parse_items,
    parse_sync,
)

NOW = datetime(2026, 9, 29, 16, 0, tzinfo=UTC)

EVENT = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:e1\r\nSUMMARY:Piano\r\n"
    "DTSTART;TZID=America/Toronto:20261001T160000\r\nDTEND;TZID=America/Toronto:20261001T170000\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)
TASK = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTODO\r\nUID:t1\r\nSUMMARY:Milk\r\n"
    "DUE;TZID=America/Toronto:20261001T180000\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
)


def unfold(text: str) -> list[str]:
    return text.replace("\r\n ", "").split("\r\n")


def an_event(uid: str) -> str:
    return ics.event(
        uid=uid,
        schedule_id=1,
        title="t",
        prompt="p",
        tools=[],
        rrule="FREQ=DAILY",
        dtstart=datetime(2026, 10, 1, 9, 0),
        tz="America/Toronto",
        now=NOW,
    )


def dav(fake: FakeCalDav, password: str = "pw") -> CalDav:
    return CalDav("http://nc", "uriel", password, transport=fake.transport())


def test_an_event_carries_the_schedule():
    text = ics.event(
        uid="uriel-abc",
        schedule_id=7,
        title="AI news, daily",
        prompt="A digest of AI news;\nkeep it short",
        tools=["web_search"],
        rrule="FREQ=WEEKLY;BYDAY=MO,TH",
        dtstart=datetime(2026, 10, 1, 9, 0),
        tz="America/Toronto",
        now=NOW,
    )
    lines = unfold(text)
    assert lines[0] == "BEGIN:VCALENDAR" and lines[-2] == "END:VCALENDAR" and lines[-1] == ""
    for line in [
        "UID:uriel-abc",
        "SUMMARY:AI news\\, daily",
        "DTSTART;TZID=America/Toronto:20261001T090000",
        "DURATION:PT15M",
        "RRULE:FREQ=WEEKLY;BYDAY=MO,TH",
        "X-URIEL-SCHEDULE-ID:7",
        "DTSTAMP:20260929T160000Z",
    ]:
        assert line in lines
    [description] = [x for x in lines if x.startswith("DESCRIPTION:")]
    assert "A digest of AI news\\;\\nkeep it short" in description and "web_search" in description


def test_a_one_off_event_has_no_rule():
    text = ics.event(
        uid="u",
        schedule_id=1,
        title="t",
        prompt="p",
        tools=[],
        rrule="FREQ=DAILY;COUNT=1",
        dtstart=datetime(2026, 10, 1, 9, 0),
        tz="America/Toronto",
        now=NOW,
    )
    assert "RRULE" not in text


def test_a_done_task():
    lines = unfold(
        ics.todo(uid="u-run-3", summary="AI news: ok", description="Three stories", completed=NOW, now=NOW)
    )
    for line in ["BEGIN:VTODO", "STATUS:COMPLETED", "COMPLETED:20260929T160000Z", "SUMMARY:AI news: ok"]:
        assert line in lines


def test_long_lines_fold_at_75_octets_without_splitting_characters():
    text = ics.todo(uid="u", summary="è" * 100, description="x", completed=NOW, now=NOW)
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    assert "SUMMARY:" + "è" * 100 in unfold(text)


async def test_a_calendar_can_hold_events_and_tasks():
    fake = FakeCalDav()
    await dav(fake).make_calendar("x", "X", "VEVENT", also=("VTODO",))
    assert fake.calendars["x"].component == "VEVENT"  # the fake records the first; the body carries both
    assert fake.calendars["x"].components == ("VEVENT", "VTODO")


async def test_a_calendar_can_be_shared_with_a_group():
    fake = FakeCalDav()
    client = dav(fake)
    href = await client.make_calendar("family", "Family", "VEVENT")
    await client.share_with_group(href, "family")
    assert fake.calendars["family"].shares == {"principal:principals/groups/family": True}


async def test_make_share_put_delete():
    fake = FakeCalDav()
    client = dav(fake)
    href = await client.make_calendar("uriel-tony", "Uriel · tony", "VEVENT")
    assert href == "/remote.php/dav/calendars/uriel/uriel-tony/"
    assert await client.make_calendar("uriel-tony", "Uriel · tony", "VEVENT") == href  # 405: already there
    cal = fake.calendars["uriel-tony"]
    assert (cal.name, cal.component) == ("Uriel · tony", "VEVENT")
    await client.share(href, "tony")
    assert cal.shares == {"principal:principals/users/tony": True}
    assert await client.put(href, "uriel-1", an_event("uriel-1")) == etag(an_event("uriel-1"))
    assert cal.items == {"uriel-1.ics": an_event("uriel-1")}
    await client.delete(href, "uriel-1")
    await client.delete(href, "uriel-1")  # already gone is fine
    assert cal.items == {}


async def test_put_into_a_missing_calendar():
    client = dav(FakeCalDav())
    with pytest.raises(CalendarMissing):
        await client.put(client.href("uriel-tony"), "uriel-1", "x")


async def test_sync_reports_404_items_as_deleted_and_the_rest_as_changed_with_their_etag():
    fake = FakeCalDav()
    client = dav(fake)
    href = await client.make_calendar("uriel-tony", "Uriel · tony", "VEVENT")
    await client.put(href, "uriel-1", an_event("uriel-1"))
    await client.put(href, "uriel-2", an_event("uriel-2"))
    first = await client.sync(href, None)
    assert first.deleted == [] and first.token
    assert first.changed == {u: etag(an_event(u)) for u in ("uriel-1", "uriel-2")}
    fake.person_deletes("uriel-tony", "uriel-1")
    fake.person_edits("uriel-tony", "uriel-2", "edited")
    later = await client.sync(href, first.token)
    assert later.deleted == ["uriel-1"] and later.token != first.token
    assert later.changed == {"uriel-2": etag("edited")}


async def test_sync_failures_are_distinct():
    fake = FakeCalDav()
    client = dav(fake)
    href = await client.make_calendar("uriel-tony", "Uriel · tony", "VEVENT")
    with pytest.raises(InvalidSyncToken):
        await client.sync(href, "http://sabre.io/ns/sync/999")
    fake.empty_report = True
    assert (await client.sync(href, None)).deleted == []
    fake.empty_report = False
    fake.fail["REPORT"] = 503
    with pytest.raises(Unreachable):
        await client.sync(href, None)
    with pytest.raises(AuthFailed):
        await dav(fake, "wrong").sync(href, None)
    del fake.calendars["uriel-tony"]
    fake.fail.clear()
    with pytest.raises(CalendarMissing):
        await client.sync(href, None)


def test_person_typed_text_cannot_inject_properties():
    hostile = "x\rEND:VEVENT\rBEGIN:VEVENT\r\nUID:evil\nA;B,C\\D\x00\x07"
    text = ics.event(
        uid="u",
        schedule_id=1,
        title=hostile,
        prompt=hostile,
        tools=[],
        rrule="FREQ=DAILY",
        dtstart=datetime(2026, 10, 1, 9, 0),
        tz="America/Toronto",
        now=NOW,
    )
    lines = unfold(text)
    assert not any(c in text.replace("\r\n", "") for c in "\r\x00\x07")
    assert lines.count("END:VEVENT") == 1 and lines.count("BEGIN:VEVENT") == 1
    assert not any(x.startswith("UID:evil") for x in lines)
    escaped = "x\\nEND:VEVENT\\nBEGIN:VEVENT\\nUID:evil\\nA\\;B\\,C\\\\D"
    assert f"SUMMARY:{escaped}" in lines
    assert any(x.startswith(f"DESCRIPTION:{escaped}") for x in lines)


async def test_put_rejects_bodies_the_calendar_would_refuse():
    fake = FakeCalDav()
    client = dav(fake)
    href = await client.make_calendar("uriel-tony", "Uriel", "VEVENT")
    with pytest.raises(WebDavError):
        await client.put(href, "u", "nonsense")
    with pytest.raises(WebDavError):
        await client.put(href, "u", ics.todo(uid="u", summary="s", description="d", completed=NOW, now=NOW))


async def test_a_uid_conflict_is_not_a_missing_calendar():
    fake = FakeCalDav()
    fake.fail["PUT"] = 409
    fake.conflict_body = "<s:exception>no-uid-conflict</s:exception>"
    client = dav(fake)
    href = await client.make_calendar("uriel-tony", "Uriel", "VEVENT")
    with pytest.raises(WebDavError) as e:
        await client.put(href, "u", an_event("u"))
    assert not isinstance(e.value, CalendarMissing)


async def test_a_malformed_report_is_a_webdav_error():
    fake = FakeCalDav()
    client = dav(fake)
    href = await client.make_calendar("uriel-tony", "Uriel", "VEVENT")
    fake.report_body = "<d:multistatus"
    with pytest.raises(WebDavError):
        await client.sync(href, None)


def test_deletions_are_found_under_a_url_prefix():
    xml = (
        b'<d:multistatus xmlns:d="DAV:"><d:response>'
        b"<d:href>/nextcloud/remote.php/dav/calendars/uriel/uriel-tony/uriel-1.ics</d:href>"
        b"<d:status>HTTP/1.1 404 Not Found</d:status></d:response></d:multistatus>"
    )
    assert parse_sync(xml, "/remote.php/dav/calendars/uriel/uriel-tony/").deleted == ["uriel-1"]


async def test_calendars_lists_what_is_shared_with_uriel():
    fake = FakeCalDav()
    home = fake.add_calendar(
        "personal_shared_by_anthony-headband", "Personal", owner="anthony-headband", sharees=("users/uriel",)
    )
    fake.add_calendar(
        "family_shared_by_lil-b",
        "Family",
        owner="lil-b",
        sharees=("groups/uriel-family",),
        components=("VEVENT",),
        read_only=True,
    )
    cals = await dav(fake).calendars()
    assert cals == [
        CalendarInfo(
            home,
            "Personal",
            "anthony-headband",
            False,
            frozenset({"users/uriel"}),
            frozenset({"VEVENT", "VTODO"}),
        ),
        CalendarInfo(
            "/remote.php/dav/calendars/uriel/family_shared_by_lil-b/",
            "Family",
            "lil-b",
            True,
            frozenset({"groups/uriel-family"}),
            frozenset({"VEVENT"}),
        ),
    ]  # the home collection itself is not a calendar


def test_parse_calendars_reads_nextclouds_forms():
    xml = (
        b'<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" '
        b'xmlns:c="urn:ietf:params:xml:ns:caldav">'
        b"<d:response><d:href>/nc/remote.php/dav/calendars/uriel/p%20x/</d:href><d:propstat><d:prop>"
        b"<d:resourcetype><d:collection/><c:calendar/></d:resourcetype><d:displayname>P x</d:displayname>"
        b"<oc:owner-principal>principals/users/anna.b</oc:owner-principal><oc:read-only>1</oc:read-only>"
        b"<oc:invite><oc:user><d:href>/nc/remote.php/dav/principals/users/uriel/</d:href></oc:user></oc:invite>"
        b"</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
        b"<d:propstat><d:prop><c:supported-calendar-component-set/></d:prop>"
        b"<d:status>HTTP/1.1 404 Not Found</d:status></d:propstat></d:response></d:multistatus>"
    )
    [cal] = parse_calendars(xml)
    assert (cal.href, cal.owner, cal.read_only, cal.sharees) == (
        "/nc/remote.php/dav/calendars/uriel/p x/",
        "anna.b",
        True,
        frozenset({"users/uriel"}),
    )
    assert cal.components == frozenset({"VEVENT", "VTODO"})  # unknown: assume both, as Nextcloud's default


async def test_events_and_open_tasks_return_items_with_etags():
    fake = FakeCalDav()
    href = fake.add_calendar("p", "Personal", owner="anthony-headband", sharees=("users/uriel",))
    fake.calendars["p"].items |= {"e1.ics": EVENT, "t1.ics": TASK}
    client = dav(fake)
    [event] = await client.events(href, NOW, NOW)
    assert event == Item(f"{href}e1.ics", etag(EVENT), EVENT)
    assert [t.href for t in await client.open_tasks(href)] == [f"{href}t1.ics"]


def test_parse_items_unescapes_calendar_data():
    xml = (
        b'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response>'
        b'<d:href>/c/p/a%20b.ics</d:href><d:propstat><d:prop><d:getetag>"1"</d:getetag>'
        b"<c:calendar-data>BEGIN:VCALENDAR&#13;\nSUMMARY:R&amp;D&#13;\nEND:VCALENDAR&#13;\n</c:calendar-data>"
        b"</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>"
    )
    assert parse_items(xml) == [
        Item("/c/p/a b.ics", '"1"', "BEGIN:VCALENDAR\r\nSUMMARY:R&D\r\nEND:VCALENDAR\r\n")
    ]


async def test_get_write_and_remove_are_conditional():
    fake = FakeCalDav()
    href = fake.add_calendar("p", "Personal", owner="x")
    client = dav(fake)
    item = f"{href}t1.ics"
    assert await client.get(item) is None
    first = await client.write(item, TASK, new=True)
    assert first == etag(TASK)
    with pytest.raises(Changed):
        await client.write(item, TASK, new=True)  # something is already there
    got = await client.get(item)
    assert got == Item(item, etag(TASK), TASK)
    fake.person_edits("p", "t1", TASK.replace("Milk", "Bread"))
    with pytest.raises(Changed):
        await client.write(item, TASK, etag=got.etag)
    with pytest.raises(Changed):
        await client.remove(item, etag=got.etag)
    await client.remove(item, etag=etag(fake.item(item)))
    assert fake.item(item) is None
    await client.remove(item)  # already gone is fine


async def test_a_read_only_share_refuses_writes():
    fake = FakeCalDav()
    href = fake.add_calendar("f", "Family", owner="lil-b", read_only=True)
    with pytest.raises(WebDavError):
        await dav(fake).write(f"{href}x.ics", TASK, new=True)


async def test_the_event_query_asks_for_expansion_in_the_range():
    sent = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request.content.decode())
        return httpx.Response(207, content=b'<d:multistatus xmlns:d="DAV:"/>')

    client = CalDav("http://nc", "uriel", "pw", transport=httpx.MockTransport(answer))
    href = "/remote.php/dav/calendars/uriel/p/"
    assert (
        await client.events(href, datetime(2026, 10, 1, 4, tzinfo=UTC), datetime(2026, 10, 3, 4, tzinfo=UTC))
        == []
    )
    assert '<c:expand start="20261001T040000Z" end="20261003T040000Z"/>' in sent[0]
    assert '<c:time-range start="20261001T040000Z" end="20261003T040000Z"/>' in sent[0]
