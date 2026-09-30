import asyncio
import re
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes.caldav import FakeCalDav
from tests.schedule_helpers import make, unattended
from uriel_tools.drafts import Drafts
from uriel_tools.gating import Caller
from uriel_tools.schedules.caldav import CalDav
from uriel_tools.schedules.mirror import Mirror, slug
from uriel_tools.schedules.service import Schedules

pytestmark = pytest.mark.db

TONY = Caller("tony", frozenset({"family"}), "r1")
EVENTS, DONE = "uriel-tony", "uriel-tony-done"


class Ticks:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def svc(pool, clock):
    return Schedules(pool, Drafts(), unattended("web_search"), "America/Toronto", clock)


@pytest.fixture
def fake():
    return FakeCalDav()


@pytest.fixture
def ticks():
    return Ticks()


@pytest.fixture
def mirror(pool, fake, clock, ticks):
    async def ready():
        return pool

    dav = CalDav("http://nc", "uriel", "pw", transport=fake.transport())
    return Mirror(ready, dav, clock=ticks, now=clock)


async def uid_of(pool, schedule_id: int) -> str:
    async with pool.connection() as conn:
        row = await (
            await conn.execute("SELECT caldav_uid FROM schedules WHERE id = %s", (schedule_id,))
        ).fetchone()
    return row["caldav_uid"]


async def dirty(pool) -> int:
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT (SELECT count(*) FROM schedules WHERE mirror_dirty) + "
                "(SELECT count(*) FROM schedule_runs WHERE mirror_dirty) AS n"
            )
        ).fetchone()
    return row["n"]


def test_calendar_names_are_safe_in_a_url_and_distinct():
    assert slug("lil-b") == "lil-b"
    assert re.fullmatch(r"anna-maria-[0-9a-f]{6}", slug("Anna.Maria"))  # changed, so made unique
    assert slug("Anna.Maria") == slug("Anna.Maria")
    assert len({slug(o) for o in ["anna.b", "anna_b", "anna-b", "Anna-B"]}) == 4


async def test_owners_whose_names_look_alike_get_their_own_calendars(svc, mirror, fake):
    anna_dot, anna_under = (
        Caller("anna.b", frozenset({"family"}), "r1"),
        Caller("anna_b", frozenset({"family"}), "r2"),
    )
    await make(svc, anna_dot)
    await make(svc, anna_under, title="Weather")
    await mirror.push_once()
    shared = {
        next(iter(cal.shares)): [i for i in cal.items]
        for cal in fake.calendars.values()
        if cal.component == "VEVENT"
    }
    assert set(shared) == {"principal:principals/users/anna.b", "principal:principals/users/anna_b"}
    assert all(len(items) == 1 for items in shared.values())
    assert all(len(cal.shares) == 1 for cal in fake.calendars.values())


async def test_the_first_schedule_makes_and_shares_both_calendars(svc, mirror, fake, pool):
    await make(svc, TONY)
    await mirror.push_once()
    assert fake.calendars[EVENTS].name == "Uriel · tony"
    assert fake.calendars[EVENTS].component == "VEVENT"
    assert (fake.calendars[DONE].name, fake.calendars[DONE].component) == ("Uriel · done", "VTODO")
    for cal in (EVENTS, DONE):
        assert fake.calendars[cal].shares == {"principal:principals/users/tony": True}
    await make(svc, TONY, title="Weather")
    await mirror.push_once()
    assert [m for m, _ in fake.requests].count("MKCALENDAR") == 2  # made once per person
    assert len(fake.calendars[EVENTS].items) == 2
    assert await dirty(pool) == 0


async def test_push_creates_and_deletes_the_event(svc, mirror, fake, pool):
    made = await make(svc, TONY)
    uid = await uid_of(pool, made["schedule_id"])
    await mirror.push_once()
    event = fake.calendars[EVENTS].items[f"{uid}.ics"]
    assert "SUMMARY:AI news" in event and "RRULE:FREQ=DAILY" in event
    await svc.cancel(TONY, made["schedule_id"])
    await mirror.push_once()
    assert fake.calendars[EVENTS].items == {}
    assert fake.calendars[DONE].items == {}  # a cancellation in the chat logs no done-task


async def test_a_finished_run_becomes_a_done_task(svc, mirror, fake, clock, pool):
    made = await make(svc, TONY)
    uid = await uid_of(pool, made["schedule_id"])
    clock.at = datetime(2026, 3, 7, 14, 1, tzinfo=UTC)
    [run] = (await svc.claim_due())["runs"]
    await mirror.push_once()
    assert fake.calendars[DONE].items == {}  # a claimed run isn't done yet
    clock.at += timedelta(minutes=2)
    await svc.finish(run["run_id"], "ok", summary="Three stories about chips")
    await mirror.push_once()
    task = fake.calendars[DONE].items[f"{uid}-run-{run['run_id']}.ics"]
    for line in ["SUMMARY:AI news: ok", "STATUS:COMPLETED", "COMPLETED:20260307T140300Z"]:
        assert line in task
    assert "Three stories about chips" in task
    assert await dirty(pool) == 0


async def test_a_missed_one_off_shows_as_a_skipped_done_task(svc, mirror, fake, clock, pool):
    made = await make(svc, TONY, frequency="once", at="10:30", on="2026-03-06")
    uid = await uid_of(pool, made["schedule_id"])
    clock.at = datetime(2026, 3, 6, 16, 0, tzinfo=UTC)  # 11:00 in Toronto: half an hour late
    assert (await svc.claim_due())["runs"] == []
    await mirror.push_once()
    [(name, task)] = fake.calendars[DONE].items.items()
    assert name.startswith(f"{uid}-run-") and "SUMMARY:AI news: skipped" in task
    assert fake.calendars[EVENTS].items == {}  # the one-off is finished, so its event is gone
    assert await dirty(pool) == 0


async def test_a_calendar_deleted_between_pulls_is_made_again_on_push(svc, mirror, fake, pool):
    await make(svc, TONY)
    await mirror.push_once()
    fake.person_deletes_calendar(EVENTS)
    await make(svc, TONY, title="Weather")
    await mirror.push_once()  # the PUT finds no calendar: both are made again, and every schedule re-marked
    assert len(fake.calendars[EVENTS].items) == 1
    await mirror.push_once()
    assert len(fake.calendars[EVENTS].items) == 2


async def test_a_failed_push_stays_dirty_and_backs_off(svc, mirror, fake, pool, ticks):
    await make(svc, TONY)
    fake.fail["MKCALENDAR"] = 503
    await mirror.push_once()
    assert await dirty(pool) == 1
    fake.fail.clear()
    await mirror.push_once()  # still backing off
    assert await dirty(pool) == 1
    ticks.t += 31
    await mirror.push_once()
    assert await dirty(pool) == 0


# Pull.


async def test_deleting_the_event_in_nextcloud_cancels_the_schedule(svc, mirror, fake, pool):
    made = await make(svc, TONY)
    uid = await uid_of(pool, made["schedule_id"])
    await mirror.push_once()
    await mirror.pull_once()  # the first sync takes a token
    fake.person_deletes(EVENTS, uid)
    await mirror.pull_once()
    assert (await svc.list(TONY))["schedules"] == []
    await mirror.push_once()
    [task] = fake.calendars[DONE].items.values()
    assert "SUMMARY:AI news: cancelled in Nextcloud" in task
    async with pool.connection() as conn:
        row = await (
            await conn.execute("SELECT cancelled_by FROM schedules WHERE id = %s", (made["schedule_id"],))
        ).fetchone()
    assert row["cancelled_by"] == "nextcloud"


async def test_an_event_edited_in_nextcloud_is_put_back_without_echoing(svc, mirror, fake, pool):
    made = await make(svc, TONY)
    uid = await uid_of(pool, made["schedule_id"])
    await mirror.push_once()
    await mirror.pull_once()  # lists Uriel's own PUT: same ETag, so nothing to put back
    assert await dirty(pool) == 0
    fake.person_edits(EVENTS, uid, "BEGIN:VCALENDAR\r\nSUMMARY:edited\r\nEND:VCALENDAR\r\n")
    await mirror.pull_once()
    assert await dirty(pool) == 1
    await mirror.push_once()
    assert "SUMMARY:AI news" in fake.calendars[EVENTS].items[f"{uid}.ics"]  # Uriel's version wins
    await mirror.pull_once()  # the sync now reports Uriel's PUT, which it recognises by its ETag
    assert await dirty(pool) == 0
    assert (await svc.list(TONY))["schedules"][0]["schedule_id"] == made["schedule_id"]


async def stored_token(pool) -> str:
    async with pool.connection() as conn:
        row = await (await conn.execute("SELECT sync_token FROM caldav_sync")).fetchone()
    return row["sync_token"]


async def test_an_edited_event_or_the_persons_own_event_cancels_nothing(svc, mirror, fake, pool):
    made = await make(svc, TONY)
    uid = await uid_of(pool, made["schedule_id"])
    await mirror.push_once()
    await mirror.pull_once()
    fake.person_edits(EVENTS, uid, "BEGIN:VCALENDAR\r\nSUMMARY:edited\r\nEND:VCALENDAR\r\n")
    fake.person_edits(EVENTS, "dentist", "BEGIN:VCALENDAR\r\nSUMMARY:Dentist\r\nEND:VCALENDAR\r\n")
    await mirror.pull_once()
    fake.person_deletes(EVENTS, "dentist")
    await mirror.pull_once()
    assert len((await svc.list(TONY))["schedules"]) == 1
    assert await stored_token(pool) == f"http://sabre.io/ns/sync/{fake.token}"  # the pulls really ran


async def test_a_failed_empty_or_invalid_token_pull_cancels_nothing(svc, mirror, fake, pool):
    made = await make(svc, TONY)
    uid = await uid_of(pool, made["schedule_id"])
    await mirror.push_once()
    await mirror.pull_once()
    fake.person_deletes(EVENTS, uid)
    fake.fail["REPORT"] = 503
    await mirror.pull_once()
    fake.fail.clear()
    fake.empty_report = True
    await mirror.pull_once()
    fake.empty_report = False
    async with pool.connection() as conn:
        await conn.execute("UPDATE caldav_sync SET sync_token = 'http://sabre.io/ns/sync/999'")
    await mirror.pull_once()  # invalid token: a full resync with a fresh token, which cancels nothing
    await mirror.pull_once()  # and nothing changed since that token
    assert len((await svc.list(TONY))["schedules"]) == 1
    assert await stored_token(pool) == f"http://sabre.io/ns/sync/{fake.token}"


async def test_a_deleted_calendar_is_made_again_and_nothing_is_cancelled(svc, mirror, fake, pool):
    await make(svc, TONY)
    await make(svc, TONY, title="Weather")
    await mirror.push_once()
    await mirror.pull_once()
    fake.person_deletes_calendar(EVENTS)
    await mirror.pull_once()
    assert len((await svc.list(TONY))["schedules"]) == 2
    await mirror.push_once()
    assert len(fake.calendars[EVENTS].items) == 2
    assert fake.calendars[EVENTS].shares == {"principal:principals/users/tony": True}


async def test_the_loop_pulls_then_pushes_survives_a_failure_and_stops(mirror, monkeypatch):
    calls = []
    pushed_twice = asyncio.Event()

    async def pull():
        calls.append("pull")
        if len(calls) == 1:
            raise RuntimeError("boom")

    async def push():
        calls.append("push")
        if calls.count("push") == 2:
            pushed_twice.set()

    monkeypatch.setattr(mirror, "pull_once", pull)
    monkeypatch.setattr(mirror, "push_once", push)
    stop = asyncio.Event()
    task = asyncio.create_task(mirror.run(stop, push_s=0.01, pull_s=1000))
    await asyncio.wait_for(pushed_twice.wait(), 2)
    stop.set()
    await asyncio.wait_for(task, 1)
    # The failed pull skipped its push and stays due; the retry pulls, then pushes; no pull is due after.
    assert calls[:3] == ["pull", "pull", "push"]
    assert calls.count("pull") == 2
