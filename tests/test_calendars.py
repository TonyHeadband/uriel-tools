from datetime import UTC, datetime, timedelta

import pytest
from fastmcp import Client

from tests.fakes.caldav import FakeCalDav
from tests.fakes.models import HashEmbedder
from uriel_tools.app import build_mcp
from uriel_tools.calendars import ical
from uriel_tools.calendars.service import Calendars, Refused, item_href, item_id
from uriel_tools.drafts import Drafts
from uriel_tools.gating import Caller, ToolPolicy
from uriel_tools.schedules import store as schedule_store
from uriel_tools.schedules.caldav import CalDav
from uriel_tools.tools import Rag

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 29, 23, 0, tzinfo=UTC)  # Tue 29 Sep 2026, 19:00 in Toronto
TONY = Caller("anthony-headband", frozenset({"family"}), "r1")
LIL_B = Caller("lil-b", frozenset({"family"}), "r2")


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


@pytest.fixture
def fake():
    f = FakeCalDav()
    f.add_calendar("personal", "Personal", owner="anthony-headband")
    f.add_calendar("family", "Family", owner="uriel", sharees=("groups/family",))
    f.add_calendar("lils", "Lil-B", owner="lil-b")
    return f


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def svc(pool, fake, clock):
    return Calendars(
        pool,
        CalDav("http://nc", "uriel", "pw", transport=fake.transport()),
        Drafts(),
        "America/Toronto",
        clock,
    )


def stored(fake, slug):
    return list(fake.calendars[slug].items.values())


async def test_a_reminder_goes_straight_to_their_calendar_and_says_where(svc, fake):
    made = await svc.add_reminder(TONY, "Call the dentist", "2026-10-01", "15:00")
    assert made["added"] == "reminder" and made["calendar"] == "Personal"
    assert made["due_local"] == "Thu 1 Oct 2026, 15:00" and "undo" in made["note"]
    [text] = stored(fake, "personal")
    assert "DUE;TZID=America/Toronto:20261001T150000" in text and "X-URIEL-OWNER:anthony-headband" in text
    assert item_href(made["item_id"]).startswith("/remote.php/dav/calendars/uriel/personal/uriel-")


async def test_a_past_time_is_refused_with_the_time_now(svc):
    with pytest.raises(Refused, match=r"already passed \(it is now Tue 29 Sep 2026, 19:00 Toronto time\)"):
        await svc.add_reminder(TONY, "x", "2026-09-29", "18:00")


async def test_a_write_to_the_family_calendar_is_a_draft(svc, fake):
    draft = await svc.add_event(TONY, "Recital", "2026-10-02", "19:00", "20:00", calendar="Family")
    assert set(draft) == {"draft_id", "change", "next"}
    assert draft["change"] == 'Add "Recital" to Family, Fri 2 Oct 2026, 19:00–20:00'
    assert stored(fake, "family") == []


async def test_an_event_without_an_end_lasts_an_hour_and_all_day_needs_no_times(svc, fake):
    timed = await svc.add_event(TONY, "Lunch", "2026-10-02", "12:00")
    assert timed["when"] == "Fri 2 Oct 2026, 12:00–13:00"
    allday = await svc.add_event(TONY, "Holiday", "2026-10-12")
    assert allday["when"] == "Mon 12 Oct 2026, all day"
    with pytest.raises(Refused, match="end must be after start"):
        await svc.add_event(TONY, "x", "2026-10-02", "12:00", "11:00")
    with pytest.raises(Refused, match="needs a start"):
        await svc.add_event(TONY, "x", "2026-10-02", None, "11:00")


async def test_bad_fields_are_refused_with_a_fix(svc):
    with pytest.raises(Refused, match="HH:MM"):
        await svc.add_reminder(TONY, "x", "2026-10-01", "3pm")
    with pytest.raises(Refused, match="YYYY-MM-DD"):
        await svc.add_reminder(TONY, "x", "Thursday", "15:00")
    with pytest.raises(Refused, match="title"):
        await svc.add_reminder(TONY, "  ", "2026-10-01", "15:00")


async def test_the_agenda_shows_their_items_in_local_time(svc, fake):
    await svc.add_reminder(TONY, "Milk", "2026-09-30", "18:00")
    await svc.add_event(TONY, "Lunch", "2026-09-30", "12:00", "13:00")
    await svc.add_reminder(LIL_B, "Homework", "2026-09-30", "17:00")
    agenda = await svc.agenda(TONY)
    assert (agenda["from"], agenda["to"]) == ("Tue 29 Sep 2026", "Wed 30 Sep 2026")
    assert [(i["title"], i["kind"], i["calendar"], i["when"]) for i in agenda["items"]] == [
        ("Lunch", "event", "Personal", "Wed 30 Sep 2026, 12:00–13:00"),
        ("Milk", "task", "Personal", "due Wed 30 Sep 2026, 18:00"),
    ]
    assert all("item_id" in i for i in agenda["items"])


async def test_overdue_open_tasks_are_flagged_and_done_ones_left_out(svc, fake, clock):
    made = await svc.add_reminder(TONY, "Old", "2026-09-30", "10:00")
    done = await svc.add_reminder(TONY, "Done", "2026-09-30", "11:00")
    await svc.complete(TONY, done["item_id"])
    clock.at = NOW + timedelta(days=3)
    items = (await svc.agenda(TONY))["items"]
    assert [(i["title"], i["overdue"]) for i in items] == [("Old", True)]
    assert items[0]["item_id"] == made["item_id"]


async def test_the_agenda_range_and_a_calendar_filter(svc, fake):
    await svc.add_event(TONY, "Far", "2026-10-20", "12:00")
    assert (await svc.agenda(TONY, "2026-10-20", "2026-10-20"))["items"][0]["title"] == "Far"
    assert (await svc.agenda(TONY, calendar="Family"))["items"] == []
    with pytest.raises(Refused, match="31 days"):
        await svc.agenda(TONY, "2026-10-01", "2026-12-01")
    with pytest.raises(Refused, match="before"):
        await svc.agenda(TONY, "2026-10-05", "2026-10-01")


async def test_expanded_occurrences_are_listed_as_recurring(svc, fake):
    href = "/remote.php/dav/calendars/uriel/personal/s.ics"
    fake.calendars["personal"].items["s.ics"] = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:s\r\nSUMMARY:Piano\r\nRRULE:FREQ=DAILY\r\n"
        "DTSTART;TZID=America/Toronto:20260929T200000\r\nDTEND;TZID=America/Toronto:20260929T210000\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    fake.expanded[href] = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:s\r\nSUMMARY:Piano\r\nRECURRENCE-ID:20260930T000000Z\r\n"
        "DTSTART:20260930T000000Z\r\nDTEND:20260930T010000Z\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:s\r\n"
        "SUMMARY:Piano\r\nRECURRENCE-ID:20261001T000000Z\r\nDTSTART:20261001T000000Z\r\n"
        "DTEND:20261001T010000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    items = (await svc.agenda(TONY))["items"]
    assert [(i["when"], i["recurring"]) for i in items] == [
        ("Tue 29 Sep 2026, 20:00–21:00", True),
        ("Wed 30 Sep 2026, 20:00–21:00", True),
    ]
    assert items[0]["item_id"] == item_id(href)


async def test_the_agenda_is_capped(svc, fake):
    for n in range(55):
        fake.calendars["personal"].items[f"e{n}.ics"] = (
            f"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:e{n}\r\nSUMMARY:E{n}\r\n"
            f"DTSTART:20260930T{n // 4 + 10:02d}{n % 4 * 15:02d}00Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
    agenda = await svc.agenda(TONY)
    assert len(agenda["items"]) == 50 and agenda["more"] == 5


async def test_a_malformed_item_from_another_client_does_not_break_the_agenda(svc, fake, caplog):
    fake.calendars["personal"].items["bad.ics"] = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:bad\r\nSUMMARY:Broken\r\nDTSTART:not-a-date\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    fake.calendars["personal"].items["good.ics"] = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:good\r\nSUMMARY:Fine\r\nDTSTART:20260930T160000Z\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    with caplog.at_level("WARNING"):
        items = (await svc.agenda(TONY))["items"]
    assert [i["title"] for i in items] == ["Fine"]
    assert "bad.ics" in caplog.text


async def test_a_cancelled_task_is_not_on_the_agenda(svc, fake):
    fake.calendars["personal"].items["c.ics"] = (
        "BEGIN:VCALENDAR\r\nBEGIN:VTODO\r\nUID:c\r\nSUMMARY:Called off\r\nSTATUS:CANCELLED\r\n"
        "DUE:20260930T160000Z\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
    )
    await svc.add_reminder(TONY, "Milk", "2026-09-30", "18:00")
    assert [i["title"] for i in (await svc.agenda(TONY))["items"]] == ["Milk"]


async def test_complete_ticks_a_task_and_refuses_an_event(svc, fake):
    made = await svc.add_reminder(TONY, "Milk", "2026-09-30", "18:00")
    assert (await svc.complete(TONY, made["item_id"]))["completed"] is True
    assert ical.master(stored(fake, "personal")[0], "America/Toronto").completed
    assert (await svc.complete(TONY, made["item_id"]))["already"] is True
    event = await svc.add_event(TONY, "Lunch", "2026-09-30", "12:00")
    with pytest.raises(Refused, match="not a reminder"):
        await svc.complete(TONY, event["item_id"])


async def test_an_item_id_from_someone_elses_calendar_is_no_such_item(svc):
    theirs = await svc.add_reminder(LIL_B, "Homework", "2026-09-30", "17:00")
    with pytest.raises(Refused, match="no such item"):
        await svc.complete(TONY, theirs["item_id"])
    with pytest.raises(Refused, match="no such item"):
        await svc.complete(TONY, "not base64 at all!")


async def test_the_default_calendar_decides_mine(svc, fake):
    fake.add_calendar("work", "Work", owner="anthony-headband")
    with pytest.raises(Refused, match="which calendar: Personal, Work"):
        await svc.add_reminder(TONY, "x", "2026-10-01", "15:00")
    assert (await svc.set_default(TONY, "work"))["default"] == "Work"
    assert (await svc.add_reminder(TONY, "x", "2026-10-01", "15:00"))["calendar"] == "Work"
    with pytest.raises(Refused, match="yours"):
        await svc.set_default(TONY, "Family")  # a group calendar can't be "mine"


async def test_a_read_only_calendar_refuses_writes_with_the_fix(svc, fake):
    fake.calendars["personal"].read_only = True
    with pytest.raises(Refused, match='give uriel "can edit" on Personal'):
        await svc.add_reminder(TONY, "x", "2026-10-01", "15:00")


async def test_a_dst_day_reminder(svc, fake):
    made = await svc.add_reminder(TONY, "Clocks", "2026-11-01", "01:30")  # fall-back: the first 01:30
    assert made["due_local"] == "Sun 1 Nov 2026, 1:30"
    assert "TRIGGER;RELATED=END:PT0S" in stored(fake, "personal")[0].replace("\r\n ", "")


async def test_a_late_event_without_an_end_stops_at_the_end_of_its_day(svc):
    made = await svc.add_event(TONY, "Late call", "2026-10-02", "23:30")
    assert made["when"] == "Fri 2 Oct 2026, 23:30–23:59"


def _task(uid, due, extra=""):
    return (
        f"BEGIN:VCALENDAR\r\nBEGIN:VTODO\r\nUID:{uid}\r\nSUMMARY:{uid}\r\n{extra}{due}\r\n"
        "END:VTODO\r\nEND:VCALENDAR\r\n"
    )


async def test_a_date_only_due_is_overdue_only_after_its_day(svc, fake):
    fake.calendars["personal"].items["t.ics"] = _task("today", "DUE;VALUE=DATE:20260929")
    fake.calendars["personal"].items["y.ics"] = _task("yest", "DUE;VALUE=DATE:20260928")
    items = (await svc.agenda(TONY))["items"]
    assert [(i["title"], i["overdue"], i["when"]) for i in items] == [
        ("yest", True, "due Mon 28 Sep 2026"),
        ("today", False, "due Tue 29 Sep 2026"),
    ]


async def test_an_unknown_calendar_filter_lists_the_usable_ones(svc):
    with pytest.raises(Refused, match="Famly.*Personal, Family"):
        await svc.agenda(TONY, calendar="Famly")


async def test_an_item_id_pointing_outside_the_calendar_is_no_such_item(svc, fake):
    base = "/remote.php/dav/calendars/uriel/personal/"
    for tail in ("..", ".", "a..b.ics"):
        with pytest.raises(Refused, match="no such item"):
            await svc.complete(TONY, item_id(base + tail))
    assert ("GET", "personal/..") not in fake.requests


async def test_an_encoded_path_in_an_item_id_is_no_such_item_and_never_fetched(svc, fake):
    base = "/remote.php/dav/calendars/uriel/personal/"
    for tail in ("%2e%2e%2flils%2fx.ics", "x.ics?a=b", "x.ics#y", "x y.ics", "x.ics\n", ""):
        with pytest.raises(Refused, match="no such item"):
            await svc.complete(TONY, item_id(base + tail))
    assert not [r for r in fake.requests if r[0] == "GET"]


async def test_completing_a_malformed_item_says_to_use_the_calendar_app(svc, fake):
    fake.calendars["personal"].items["bad.ics"] = _task("bad", "DUE:not-a-date")
    href = "/remote.php/dav/calendars/uriel/personal/bad.ics"
    with pytest.raises(Refused, match="can't be read: change it in the Calendar app"):
        await svc.complete(TONY, item_id(href))


async def test_an_absurd_duration_does_not_break_the_agenda(svc, fake):
    fake.calendars["personal"].items["huge.ics"] = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:h\r\nSUMMARY:Huge\r\nDTSTART:20260930T160000Z\r\n"
        "DURATION:P999999999D\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    fake.calendars["personal"].items["good.ics"] = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:g\r\nSUMMARY:Fine\r\nDTSTART:20260930T170000Z\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    assert [i["title"] for i in (await svc.agenda(TONY))["items"]] == ["Fine"]


async def test_a_spring_forward_reminder_lands_on_the_next_valid_time(svc, fake, clock):
    clock.at = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    made = await svc.add_reminder(TONY, "Clocks", "2026-03-08", "02:30")
    assert made["due_local"] == "Sun 8 Mar 2026, 3:00"
    assert "TRIGGER;RELATED=END:PT0S" in stored(fake, "personal")[0].replace("\r\n ", "")


async def test_undo_within_fifteen_minutes_is_direct(svc, fake, clock):
    made = await svc.add_reminder(TONY, "Milk", "2026-10-01", "18:00")
    clock.at = NOW + timedelta(minutes=14)
    gone = await svc.draft_change(TONY, made["item_id"], "delete")
    assert gone["deleted"] is True and stored(fake, "personal") == []


async def test_after_fifteen_minutes_a_delete_is_a_draft(svc, fake, clock):
    made = await svc.add_reminder(TONY, "Milk", "2026-10-01", "18:00")
    clock.at = NOW + timedelta(minutes=16)
    draft = await svc.draft_change(TONY, made["item_id"], "delete")
    assert draft["change"] == 'Delete "Milk" (due Thu 1 Oct 2026, 18:00) from Personal'
    assert len(stored(fake, "personal")) == 1
    done = await svc.confirm(TONY, draft["draft_id"])
    assert done == {"deleted": True, "title": "Milk", "calendar": "Personal"}
    assert stored(fake, "personal") == [] and await svc.confirm(TONY, draft["draft_id"]) == done  # idempotent


async def test_a_move_is_drafted_then_confirmed_and_keeps_the_rest(svc, fake):
    made = await svc.add_event(TONY, "Lunch", "2026-10-02", "12:00", "13:00", location="Café")
    draft = await svc.draft_change(TONY, made["item_id"], "move", start="13:30")
    assert (
        draft["change"]
        == 'Move "Lunch" on Personal from Fri 2 Oct 2026, 12:00–13:00 to Fri 2 Oct 2026, 13:30–14:30'
    )
    await svc.confirm(TONY, draft["draft_id"])
    [text] = stored(fake, "personal")
    assert "DTSTART;TZID=America/Toronto:20261002T133000" in text and "LOCATION:Café" in text


async def test_a_move_to_the_past_is_refused(svc):
    made = await svc.add_reminder(TONY, "Milk", "2026-10-01", "18:00")
    with pytest.raises(Refused, match="already passed"):
        await svc.draft_change(TONY, made["item_id"], "move", date="2026-09-28")


async def test_a_change_made_in_nextcloud_after_the_draft_wins(svc, fake):
    made = await svc.add_reminder(TONY, "Milk", "2026-10-01", "18:00")
    draft = await svc.draft_change(TONY, made["item_id"], "move", start="19:00")
    [name] = fake.calendars["personal"].items
    fake.person_edits(
        "personal", name.removesuffix(".ics"), stored(fake, "personal")[0].replace("Milk", "Bread")
    )
    with pytest.raises(Refused, match="changed in Nextcloud since the draft"):
        await svc.confirm(TONY, draft["draft_id"])
    assert "Bread" in stored(fake, "personal")[0]


async def test_a_series_moves_only_its_time_and_deletes_whole(svc, fake):
    fake.calendars["personal"].items["s.ics"] = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:s\r\nSUMMARY:Piano\r\nRRULE:FREQ=WEEKLY\r\n"
        "DTSTART;TZID=America/Toronto:20261001T160000\r\nDTEND;TZID=America/Toronto:20261001T170000\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    sid = item_id("/remote.php/dav/calendars/uriel/personal/s.ics")
    with pytest.raises(Refused, match="change the days in the Calendar app"):
        await svc.draft_change(TONY, sid, "move", date="2026-10-02")
    with pytest.raises(Refused, match="change the days in the Calendar app"):
        await svc.draft_change(TONY, sid, "move", start="2026-10-08T17:00")
    moved = await svc.draft_change(TONY, sid, "move", start="17:00", end="18:00")
    assert moved["change"] == 'Move "Piano" on Personal to 17:00–18:00 (every occurrence)'
    deleted = await svc.draft_change(TONY, sid, "delete")
    assert deleted["change"] == 'Delete "Piano" from Personal and every occurrence'


async def test_a_family_event_is_created_only_on_the_yes(svc, fake):
    draft = await svc.add_event(TONY, "Recital", "2026-10-02", "19:00", "20:00", calendar="Family")
    made = await svc.confirm(TONY, draft["draft_id"])
    assert made["added"] == "event" and made["calendar"] == "Family"
    assert len(stored(fake, "family")) == 1
    assert await svc.confirm(TONY, draft["draft_id"]) == made


async def test_drafts_belong_to_whoever_made_them(svc):
    draft = await svc.add_event(TONY, "Recital", "2026-10-02", "19:00", calendar="Family")
    with pytest.raises(Refused, match="no such draft"):
        await svc.confirm(LIL_B, draft["draft_id"])


async def test_losing_the_calendar_before_the_yes_refuses_the_change(svc, fake):
    draft = await svc.add_event(TONY, "Recital", "2026-10-02", "19:00", calendar="Family")
    fake.calendars["family"].sharees = ()
    with pytest.raises(Refused, match="no longer"):
        await svc.confirm(TONY, draft["draft_id"])


async def test_move_needs_something_to_change(svc):
    made = await svc.add_reminder(TONY, "Milk", "2026-10-01", "18:00")
    with pytest.raises(Refused, match="date or a time"):
        await svc.draft_change(TONY, made["item_id"], "move")


async def test_moving_an_event_with_a_duration_keeps_its_length(svc, fake):
    fake.calendars["personal"].items["m.ics"] = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:m\r\nSUMMARY:Odd\r\nDURATION:PT1H\r\n"
        "DTSTART;TZID=America/Toronto:20261001T160000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    mid = item_id("/remote.php/dav/calendars/uriel/personal/m.ics")
    moved = await svc.draft_change(TONY, mid, "move", start="17:00")
    assert moved["change"].startswith('Move "Odd"')
    await svc.confirm(TONY, moved["draft_id"])
    [text] = stored(fake, "personal")
    assert "DTEND;TZID=America/Toronto:20261001T180000" in text and "DURATION" not in text


WEEKLY = (
    "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:s\r\nSUMMARY:Piano\r\nRRULE:FREQ=WEEKLY\r\n"
    "DTSTART;TZID=America/Toronto:20260901T160000\r\nDTEND;TZID=America/Toronto:20260901T170000\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


async def test_a_series_that_began_in_the_past_can_change_its_time(svc, fake):
    fake.calendars["personal"].items["s.ics"] = WEEKLY
    sid = item_id("/remote.php/dav/calendars/uriel/personal/s.ics")
    draft = await svc.draft_change(TONY, sid, "move", start="17:00", end="18:00")
    await svc.confirm(TONY, draft["draft_id"])
    [text] = stored(fake, "personal")
    assert "RRULE:FREQ=WEEKLY" in text and "DTSTART;TZID=America/Toronto:20260901T170000" in text
    assert "DTEND;TZID=America/Toronto:20260901T180000" in text


async def test_an_undo_that_races_a_nextcloud_edit_is_refused(svc, fake):
    made = await svc.add_reminder(TONY, "Milk", "2026-10-01", "18:00")
    fake.fail["DELETE"] = 412
    with pytest.raises(Refused, match="changed in Nextcloud"):
        await svc.draft_change(TONY, made["item_id"], "delete")


async def test_confirming_a_create_onto_an_existing_item_says_so(svc, fake):
    draft = await svc.add_event(TONY, "Recital", "2026-10-02", "19:00", calendar="Family")
    fake.fail["PUT"] = 412  # what the server answers when If-None-Match finds an item at that href
    with pytest.raises(Refused, match="something already exists there"):
        await svc.confirm(TONY, draft["draft_id"])


async def test_a_reminder_has_no_end(svc):
    made = await svc.add_reminder(TONY, "Milk", "2026-10-01", "18:00")
    with pytest.raises(Refused, match="due time, not an end"):
        await svc.draft_change(TONY, made["item_id"], "move", end="19:00")


async def test_changing_only_the_end_of_a_started_event_is_allowed(svc, clock):
    made = await svc.add_event(TONY, "Call", "2026-09-29", "19:30", "20:30")
    clock.at = NOW + timedelta(minutes=45)
    draft = await svc.draft_change(TONY, made["item_id"], "move", end="21:00")
    assert "19:30–21:00" in draft["change"]
    with pytest.raises(Refused, match="end must be after start"):
        await svc.draft_change(TONY, made["item_id"], "move", end="19:00")


async def test_deleting_a_fresh_family_item_is_still_a_draft(svc, fake):
    draft = await svc.add_event(TONY, "Recital", "2026-10-02", "19:00", calendar="Family")
    made = await svc.confirm(TONY, draft["draft_id"])
    gone = await svc.draft_change(TONY, made["item_id"], "delete")
    assert "draft_id" in gone and len(stored(fake, "family")) == 1


def _series(extra=""):
    return WEEKLY.replace("END:VEVENT\r\nEND:VCALENDAR", f"{extra}END:VEVENT\r\nEND:VCALENDAR", 1)


OVERRIDE = (
    "END:VEVENT\r\nBEGIN:VEVENT\r\nUID:s\r\nSUMMARY:Piano\r\n"
    "RECURRENCE-ID;TZID=America/Toronto:20261006T160000\r\n"
    "DTSTART;TZID=America/Toronto:20261006T180000\r\nDTEND;TZID=America/Toronto:20261006T190000\r\n"
)


async def test_a_series_with_exceptions_is_not_moved_by_chat(svc, fake):
    sid = item_id("/remote.php/dav/calendars/uriel/personal/s.ics")
    for text in (
        _series("EXDATE;TZID=America/Toronto:20261006T160000\r\n"),
        _series("RDATE;TZID=America/Toronto:20261007T160000\r\n"),
        _series(OVERRIDE),
    ):
        fake.calendars["personal"].items["s.ics"] = text
        with pytest.raises(Refused, match="this series has exceptions: change it in the Calendar app"):
            await svc.draft_change(TONY, sid, "move", start="17:00")
    assert "draft_id" in await svc.draft_change(TONY, sid, "delete")  # deleting takes the exceptions too
    fake.calendars["personal"].items["s.ics"] = _series()
    assert "draft_id" in await svc.draft_change(TONY, sid, "move", start="17:00")


async def test_an_iso_datetime_in_a_time_field_brings_its_date(svc, fake):
    made = await svc.add_reminder(TONY, "Dentist", None, "2026-10-01T15:00")
    assert made["due_local"] == "Thu 1 Oct 2026, 15:00"
    same = await svc.add_reminder(TONY, "Dentist", "2026-10-01", "2026-10-01T15:00:00")
    assert same["due_local"] == "Thu 1 Oct 2026, 15:00"
    event = await svc.add_event(TONY, "Lunch", "", "2026-10-02T12:00", "2026-10-02T13:30")
    assert event["when"] == "Fri 2 Oct 2026, 12:00–13:30"
    moved = await svc.draft_change(TONY, event["item_id"], "move", start="2026-10-03T16:00")
    assert moved["change"].endswith("to Sat 3 Oct 2026, 16:00–17:30")


async def test_a_date_and_a_time_that_disagree_are_refused(svc):
    with pytest.raises(Refused, match="the date and the time disagree: pass the date once"):
        await svc.add_reminder(TONY, "x", "2026-10-01", "2026-10-02T15:00")
    with pytest.raises(Refused, match="disagree"):
        await svc.add_event(TONY, "x", "2026-10-02", "12:00", "2026-10-03T13:00")
    made = await svc.add_event(TONY, "Lunch", "2026-10-02", "12:00")
    with pytest.raises(Refused, match="disagree"):
        await svc.draft_change(TONY, made["item_id"], "move", date="2026-10-04", start="2026-10-05T12:00")
    with pytest.raises(Refused, match="YYYY-MM-DD"):
        await svc.add_reminder(TONY, "x", None, "15:00")
    with pytest.raises(Refused, match="HH:MM"):
        await svc.add_reminder(TONY, "x", "2026-10-01", "2026-10-01T25:00")


async def test_overdue_tasks_are_capped_apart_from_the_days_asked_for(svc, fake):
    for n in range(15):
        fake.calendars["personal"].items[f"t{n}.ics"] = _task(
            f"old{n:02d}", f"DUE:202609{n + 10:02d}T160000Z"
        )
    await svc.add_event(TONY, "Lunch", "2026-09-30", "12:00")
    await svc.add_event(TONY, "Dinner", "2026-09-30", "18:00")
    agenda = await svc.agenda(TONY)
    assert [i["title"] for i in agenda["items"]] == [f"old{n:02d}" for n in range(5, 15)] + [
        "Lunch",
        "Dinner",
    ]
    assert agenda["overdue_more"] == 5 and "more" not in agenda


async def test_moving_a_reminder_due_on_a_day_with_a_time_says_it_has_no_time(svc, fake):
    fake.calendars["personal"].items["d.ics"] = _task("day", "DUE;VALUE=DATE:20261001")
    did = item_id("/remote.php/dav/calendars/uriel/personal/d.ics")
    with pytest.raises(Refused, match="that reminder has no time: pass only its new date"):
        await svc.draft_change(TONY, did, "move", start="15:00")


CALENDAR_TOOLS = [
    "calendar_agenda",
    "add_reminder",
    "add_event",
    "complete_reminder",
    "draft_calendar_change",
    "confirm_calendar_change",
    "set_default_calendar",
]
POLICY = ToolPolicy.model_validate(
    {"tools": {t: {"groups": ["family"], "category": "calendar"} for t in CALENDAR_TOOLS}}
)
TONY_META = {"user": "anthony-headband", "groups": ["family"], "request_id": "r1"}


async def test_reminder_agenda_and_undo_through_the_server(pool, fake):
    dav = CalDav("http://nc", "uriel", "pw", transport=fake.transport())
    async with Client(build_mcp(POLICY, Rag(pool, HashEmbedder()), calendars=dav)) as c:
        made = await c.call_tool(
            "add_reminder",
            {"title": "Call the dentist", "date": "2099-10-01", "time": "15:00"},
            meta=TONY_META,
            raise_on_error=False,
        )
        assert not made.is_error and made.structured_content["calendar"] == "Personal"
        agenda = await c.call_tool(
            "calendar_agenda",
            {"start": "2099-10-01", "end": "2099-10-01"},
            meta=TONY_META,
            raise_on_error=False,
        )
        assert [i["title"] for i in agenda.structured_content["items"]] == ["Call the dentist"]
        undo = await c.call_tool(
            "draft_calendar_change",
            {"item_id": made.structured_content["item_id"], "action": "delete"},
            meta=TONY_META,
            raise_on_error=False,
        )
        assert undo.structured_content["deleted"] is True
        past = await c.call_tool(
            "add_reminder",
            {"title": "x", "date": "2000-01-01", "time": "09:00"},
            meta=TONY_META,
            raise_on_error=False,
        )
        assert past.is_error and "already passed" in past.content[0].text


async def test_nextcloud_down_says_calendar_is_unavailable(pool, fake):
    fake.fail["PROPFIND"] = 503
    dav = CalDav("http://nc", "uriel", "pw", transport=fake.transport())
    async with Client(build_mcp(POLICY, Rag(pool, HashEmbedder()), calendars=dav)) as c:
        res = await c.call_tool("calendar_agenda", {}, meta=TONY_META, raise_on_error=False)
    assert res.is_error and res.content[0].text == "calendar is unavailable right now"


async def test_without_nextcloud_the_tools_say_so(pool):
    async with Client(build_mcp(POLICY, Rag(pool, HashEmbedder()))) as c:
        res = await c.call_tool("calendar_agenda", {}, meta=TONY_META, raise_on_error=False)
    assert res.is_error and "not configured" in res.content[0].text


async def test_a_bad_saved_timezone_and_an_out_of_range_date_are_refusals(pool, fake):
    dav = CalDav("http://nc", "uriel", "pw", transport=fake.transport())
    async with Client(build_mcp(POLICY, Rag(pool, HashEmbedder()), calendars=dav)) as c:
        huge = await c.call_tool(
            "add_event",
            {"title": "x", "date": "9999-12-31", "start": "23:30"},
            meta=TONY_META,
            raise_on_error=False,
        )
        assert huge.is_error and huge.content[0].text == "that date is out of range"
        await schedule_store.set_tz(pool, "anthony-headband", "Mars/Olympus")
        res = await c.call_tool("calendar_agenda", {}, meta=TONY_META, raise_on_error=False)
    assert res.is_error and "'Mars/Olympus' is not a timezone name" in res.content[0].text
