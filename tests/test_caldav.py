from datetime import UTC, datetime

import pytest

from tests.fakes.caldav import FakeCalDav, etag
from uriel_tools.rag.webdav import AuthFailed, Unreachable, WebDavError
from uriel_tools.schedules import ics
from uriel_tools.schedules.caldav import CalDav, CalendarMissing, InvalidSyncToken, parse_sync

NOW = datetime(2026, 9, 29, 16, 0, tzinfo=UTC)


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
