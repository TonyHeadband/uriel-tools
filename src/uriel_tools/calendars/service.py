"""People's own calendars: reminders (tasks with an alarm), events, their agenda, and changes after a yes.

New items on a calendar of their own are written at once; anything on a group calendar, and every move or
delete, is a draft the person confirms (see Task 5's draft_change / confirm).
"""

import base64
import binascii
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from psycopg_pool import AsyncConnectionPool

from uriel_tools.calendars import ical, store
from uriel_tools.calendars.visibility import SHARING_STEPS, Refused, Visible, resolve, visible_to
from uriel_tools.drafts import Drafts
from uriel_tools.gating import Caller
from uriel_tools.schedules import ics, rules
from uriel_tools.schedules import store as schedule_store
from uriel_tools.schedules.caldav import CalDav, Changed, Item

log = logging.getLogger(__name__)
__all__ = ["CalendarDraft", "Calendars", "Refused", "item_href", "item_id"]
UNDO = timedelta(minutes=15)
MAX_ITEMS = 50
MAX_OVERDUE = 10  # separate from MAX_ITEMS, so a backlog of old tasks can't hide the days asked for
MAX_DAYS = 31
NEXT = (
    "Nothing has changed yet. Show the person this change and call confirm_calendar_change with this "
    "draft_id only after they say yes. If they want something different, draft again."
)
CHANGED = "it changed in Nextcloud since the draft: call draft_calendar_change again and show the new draft"
UNREADABLE = "that item can't be read: change it in the Calendar app"
DISAGREE = "the date and the time disagree: pass the date once"
# The resource names uriel and the Calendar app make; %, ?, # and the like could reach another path.
LEAF = re.compile(r"[A-Za-z0-9._@+-]+")
ADDED = "Tell the person what you added, where and when; offer to undo it (draft_calendar_change, delete)."


@dataclass(frozen=True)
class CalendarDraft:
    action: Literal["create", "move", "delete"]
    item: str  # the item's href
    calendar: str  # the calendar's href
    etag: str | None  # what the item was when drafted: the change applies only if it still is
    ics: str | None  # the new text, for create and move
    change: str  # plain language, shown to the person
    title: str


def item_id(href: str) -> str:
    return base64.urlsafe_b64encode(href.encode()).decode().rstrip("=")


def item_href(value: str) -> str | None:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def _day(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b %Y}"


def _span(entry: ical.Entry, tz: str) -> str:
    """How an item reads to a person, in local time."""
    if entry.start is None:
        return "no date"
    if not isinstance(entry.start, datetime):
        return f"due {_day(entry.start)}" if entry.kind == "task" else f"{_day(entry.start)}, all day"
    text = rules.local(entry.start, tz)
    if entry.kind == "task":
        return f"due {text}"
    if isinstance(entry.end, datetime):
        end = entry.end.astimezone(rules.zone(tz))
        return f"{text}–{end.hour}:{end.minute:02d}"
    return text


def _clock(at: datetime, tz: str) -> str:
    local = at.astimezone(rules.zone(tz))
    return f"{local.hour}:{local.minute:02d}"


def _times(entry: ical.Entry, tz: str) -> str:
    """A series' new time of day, without the first occurrence's date, which would read as a one-off."""
    text = _clock(entry.start, tz)
    return f"{text}–{_clock(entry.end, tz)}" if isinstance(entry.end, datetime) else text


def _when(day: str | None, *clocks: str | None) -> tuple[date | None, list[time | None]]:
    """The date and times; a time sent as YYYY-MM-DDTHH:MM brings its date, which must match `day`."""
    try:
        on = rules.parse_date(day) if day else None
        times: list[time | None] = []
        for clock in clocks:
            carried, at = rules.parse_when(clock) if clock else (None, None)
            if carried is not None:
                if on is not None and carried != on:
                    raise Refused(DISAGREE)
                on = carried
            times.append(at)
    except rules.RuleError as e:
        raise Refused(str(e)) from e
    return on, times


def _sort_key(entry: ical.Entry, tz: str) -> datetime:
    if isinstance(entry.start, datetime):
        return entry.start
    return rules.localise(datetime.combine(entry.start, time()), tz)


class Calendars:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        dav: CalDav,
        drafts: Drafts[CalendarDraft],
        default_tz: str = rules.DEFAULT_TZ,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        *,
        uriel: str = "uriel",
    ):
        self.pool, self.dav, self.drafts, self.default_tz, self.now = pool, dav, drafts, default_tz, clock
        self.uriel = uriel

    async def _visible(self, caller: Caller) -> list[Visible]:
        infos = await self.dav.calendars()
        return visible_to(infos, caller, uriel=self.uriel)

    async def _tz(self, caller: Caller) -> str:
        return await schedule_store.get_tz(self.pool, caller.user, self.default_tz)

    def _future(self, at: datetime, tz: str) -> None:
        if at <= self.now():
            raise Refused(
                f"that time has already passed (it is now {rules.now_in(self.now(), tz)}): "
                "pick a later date or time"
            )

    @staticmethod
    def _fields(title: str, day: str | None, *clocks: str | None) -> tuple[str, date, list[time | None]]:
        title = " ".join(title.split())
        if not title:
            raise Refused("a reminder or event needs a title")
        on, times = _when(day, *clocks)
        if on is None:
            raise Refused(rules.DATE_FORMAT)
        return title, on, times

    @staticmethod
    def _writable(cal: Visible) -> None:
        if cal.info.read_only:
            raise Refused(f'give uriel "can edit" on {cal.info.name}: it is shared read-only')

    async def _target(self, caller: Caller, calendar: str, component: str) -> Visible:
        cals = await self._visible(caller)
        cal = resolve(cals, calendar, component, await store.get_default(self.pool, caller.user))
        self._writable(cal)
        return cal

    def _draft(self, caller: Caller, draft: CalendarDraft) -> dict:
        return {"draft_id": self.drafts.add(caller, draft), "change": draft.change, "next": NEXT}

    async def _create(self, caller: Caller, cal: Visible, uid: str, text: str, change: str) -> dict | None:
        """Writes a new item on the person's own calendar, or returns a draft for a group calendar."""
        href = f"{cal.info.href}{uid}.ics"
        if not cal.mine:
            title = ical.master(text, "UTC").title
            return self._draft(
                caller, CalendarDraft("create", href, cal.info.href, None, text, change, title)
            )
        await self.dav.write(href, text, new=True)
        await store.record_created(self.pool, caller.user, href, self.now())
        return None

    async def add_reminder(
        self,
        caller: Caller,
        title: str,
        date: str | None,
        time: str,
        notes: str = "",
        calendar: str = "mine",
    ) -> dict:
        title, day, [at] = self._fields(title, date, time)
        tz = await self._tz(caller)
        due = datetime.combine(day, at)
        instant = rules.localise(due, tz)
        self._future(instant, tz)
        cal = await self._target(caller, calendar, "VTODO")
        uid = f"uriel-{uuid.uuid4().hex}"
        text = ics.reminder(
            uid=uid, title=title, notes=notes.strip(), due=due, tz=tz, owner=caller.user, now=self.now()
        )
        when = rules.local(instant, tz)
        drafted = await self._create(caller, cal, uid, text, f'Remind: "{title}" on {cal.info.name}, {when}')
        if drafted:
            return drafted
        return {
            "added": "reminder",
            "title": title,
            "calendar": cal.info.name,
            "due_local": when,
            "item_id": item_id(f"{cal.info.href}{uid}.ics"),
            "note": ADDED,
        }

    async def add_event(
        self,
        caller: Caller,
        title: str,
        date: str | None,
        start: str | None = None,
        end: str | None = None,
        location: str = "",
        notes: str = "",
        alarm_minutes_before: int | None = None,
        calendar: str = "mine",
    ) -> dict:
        title, day, [begin, finish] = self._fields(title, date, start, end)
        if begin is None and finish is not None:
            raise Refused("an event with an end needs a start time too")
        if alarm_minutes_before is not None and not 0 <= alarm_minutes_before <= 7 * 24 * 60:
            raise Refused("alarm_minutes_before is from 0 to 10080 (a week)")
        tz = await self._tz(caller)
        if begin is not None:
            if finish is None:
                later = datetime.combine(day, begin) + timedelta(hours=1)
                finish = later.time() if later.date() == day else time(23, 59)  # events stay within their day
            if finish <= begin:
                raise Refused("end must be after start, on the same day")
            self._future(rules.localise(datetime.combine(day, begin), tz), tz)
            entry = ical.Entry(
                "event",
                title,
                rules.localise(datetime.combine(day, begin), tz),
                rules.localise(datetime.combine(day, finish), tz),
                location,
                False,
                False,
            )
        else:
            if day < self.now().astimezone(rules.zone(tz)).date():
                raise Refused(f"that day has already passed (it is now {rules.now_in(self.now(), tz)})")
            entry = ical.Entry("event", title, day, day + timedelta(days=1), location, False, False)
        cal = await self._target(caller, calendar, "VEVENT")
        uid = f"uriel-{uuid.uuid4().hex}"
        text = ics.appointment(
            uid=uid,
            title=title,
            notes=notes.strip(),
            location=location.strip(),
            day=day,
            start=begin,
            end=finish,
            tz=tz,
            alarm_minutes=alarm_minutes_before,
            owner=caller.user,
            now=self.now(),
        )
        when = _span(entry, tz)
        drafted = await self._create(caller, cal, uid, text, f'Add "{title}" to {cal.info.name}, {when}')
        if drafted:
            return drafted
        return {
            "added": "event",
            "title": title,
            "calendar": cal.info.name,
            "when": when,
            "item_id": item_id(f"{cal.info.href}{uid}.ics"),
            "note": ADDED,
        }

    async def _item(self, caller: Caller, value: str, tz: str) -> tuple[Visible, Item, ical.Entry]:
        """An item on a calendar the caller sees; anything else is "no such item", never a hint it exists."""
        href = item_href(value)
        missing = Refused("no such item: call calendar_agenda for current item_ids")
        if not href or "/" not in href:
            raise missing
        leaf = href.rsplit("/", 1)[1]
        if not LEAF.fullmatch(leaf) or ".." in leaf or leaf == ".":
            raise missing
        cal_href = href.rsplit("/", 1)[0] + "/"
        cal = next((c for c in await self._visible(caller) if c.info.href == cal_href), None)
        if cal is None:
            raise missing
        found = await self.dav.get(href)
        try:
            entry = ical.master(found.ics, tz) if found else None
        except ValueError as e:
            raise Refused(UNREADABLE) from e
        if entry is None:
            raise missing
        return cal, found, entry

    async def complete(self, caller: Caller, item_id: str) -> dict:
        cal, found, entry = await self._item(caller, item_id, await self._tz(caller))
        if entry.kind != "task":
            raise Refused("that is an event, not a reminder: only reminders can be ticked off")
        if entry.completed:
            return {"completed": True, "already": True, "title": entry.title}
        self._writable(cal)
        try:
            await self.dav.write(found.href, ical.complete(found.ics, self.now()), etag=found.etag)
        except Changed as e:
            raise Refused("it changed in Nextcloud just now: try again") from e
        except ValueError as e:
            raise Refused(UNREADABLE) from e
        return {"completed": True, "title": entry.title, "calendar": cal.info.name}

    async def draft_change(
        self,
        caller: Caller,
        item_id: str,
        action: Literal["move", "delete"],
        date: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> dict:
        tz = await self._tz(caller)
        cal, found, entry = await self._item(caller, item_id, tz)
        self._writable(cal)
        was = _span(entry, tz)
        if action == "delete":
            gone = CalendarDraft("delete", found.href, cal.info.href, found.etag, None, "", entry.title)
            if cal.mine and await store.created_since(self.pool, caller.user, found.href, self.now() - UNDO):
                try:
                    await self._apply(gone)  # undo of something just added: no draft
                except Changed as e:
                    raise Refused(CHANGED) from e
                return {"deleted": True, "title": entry.title, "calendar": cal.info.name}
            if entry.recurring:
                change = f'Delete "{entry.title}" from {cal.info.name} and every occurrence'
            else:
                change = f'Delete "{entry.title}" ({was}) from {cal.info.name}'
            return self._draft(caller, replace(gone, change=change))
        if not (date or start or end):
            raise Refused("a move needs a new date or a time")
        if ical.has_exceptions(found.ics):  # RDATE makes a series even without an RRULE
            raise Refused("this series has exceptions: change it in the Calendar app")
        new_start, new_end = self._moved(entry, tz, date, start, end)
        try:
            text = ical.retime(found.ics, start=new_start, end=new_end, tz=tz, now=self.now())
        except ValueError as e:
            raise Refused(UNREADABLE) from e
        moved = _as_local(entry, new_start, new_end, tz)
        if entry.recurring:
            change = f'Move "{entry.title}" on {cal.info.name} to {_times(moved, tz)} (every occurrence)'
        else:
            change = f'Move "{entry.title}" on {cal.info.name} from {was} to {_span(moved, tz)}'
        draft = CalendarDraft("move", found.href, cal.info.href, found.etag, text, change, entry.title)
        return self._draft(caller, draft)

    def _moved(
        self, entry: ical.Entry, tz: str, on: str | None, start: str | None, end: str | None
    ) -> tuple[datetime | date, datetime | date | None]:
        """The new start and end: naive wall-clock datetimes in tz, or dates for an all-day event.
        A timed event keeps its length unless a new end is given."""
        day, [begin, finish] = _when(on, start, end)
        if entry.recurring and day is not None:  # also a date carried in by an ISO start
            raise Refused("this repeats: only its time can change here; change the days in the Calendar app")
        if not isinstance(entry.start, datetime):  # all-day, or a reminder due on a day with no time
            if begin or finish or day is None:
                if entry.kind == "task":
                    raise Refused("that reminder has no time: pass only its new date")
                raise Refused("that is an all-day event: pass only its new date")
            if day < self.now().astimezone(rules.zone(tz)).date():
                raise Refused(f"that day has already passed (it is now {rules.now_in(self.now(), tz)})")
            length = entry.end - entry.start if isinstance(entry.end, date) else timedelta(days=1)
            return day, day + length
        local = entry.start.astimezone(rules.zone(tz)).replace(tzinfo=None)
        new_start = datetime.combine(day or local.date(), begin or local.time())
        if entry.kind == "task":
            if finish is not None:
                raise Refused("a reminder has a due time, not an end")
            self._future(rules.localise(new_start, tz), tz)
            return new_start, None
        # A series keeps its original dates, which may be past; an end-only change leaves the start alone.
        if not entry.recurring and (day or begin):
            self._future(rules.localise(new_start, tz), tz)
        if finish is not None:
            new_end = datetime.combine(new_start.date(), finish)
            if new_end <= new_start:
                raise Refused("end must be after start, on the same day")
            return new_start, new_end
        if isinstance(entry.end, datetime):
            return new_start, new_start + (entry.end - entry.start)
        return new_start, None

    async def _apply(self, draft: CalendarDraft) -> None:
        if draft.action == "create":
            await self.dav.write(draft.item, draft.ics, new=True)
        elif draft.action == "move":
            await self.dav.write(draft.item, draft.ics, etag=draft.etag)
        else:
            await self.dav.remove(draft.item, etag=draft.etag)

    async def confirm(self, caller: Caller, draft_id: str) -> dict:
        stored = self.drafts.get(caller, draft_id)
        if stored is None:
            raise Refused("no such draft (it may have expired): draft the change again and show it")
        if stored.result is not None:
            return stored.result
        draft = stored.draft
        cal = next((c for c in await self._visible(caller) if c.info.href == draft.calendar), None)
        if cal is None:
            raise Refused("you no longer have that calendar: nothing was changed")
        self._writable(cal)
        try:
            await self._apply(draft)
        except Changed as e:
            if draft.action == "create":
                raise Refused("something already exists there: add it again") from e
            raise Refused(CHANGED) from e
        done = {"title": draft.title, "calendar": cal.info.name}
        if draft.action == "create":
            await store.record_created(self.pool, caller.user, draft.item, self.now())
            tz = await self._tz(caller)
            entry = ical.master(draft.ics, tz)
            kind = "reminder" if entry.kind == "task" else "event"
            stored.result = {
                "added": kind,
                **done,
                "when": _span(entry, tz),
                "item_id": item_id(draft.item),
                "note": ADDED,
            }
        elif draft.action == "move":
            stored.result = {"moved": True, **done, "change": draft.change}
        else:
            stored.result = {"deleted": True, **done}
        return stored.result

    async def set_default(self, caller: Caller, calendar: str) -> dict:
        cals = await self._visible(caller)
        mine = [c for c in cals if c.mine and c.info.name.lower() == calendar.strip().lower()]
        if not mine:
            yours = ", ".join(c.info.name for c in cals if c.mine) or "none shared yet"
            raise Refused(f"that isn't one of yours; yours: {yours}")
        await store.set_default(self.pool, caller.user, mine[0].info.href)
        return {"default": mine[0].info.name}

    @staticmethod
    def _readable(item: Item, tz: str) -> list[ical.Entry]:
        """Another client's malformed item is skipped, so it can't hide the rest of the agenda."""
        try:
            return ical.entries(item.ics, tz)
        except (ValueError, OverflowError):  # OverflowError: an absurd DURATION
            log.warning("skipping unreadable calendar item %s", item.href)
            return []

    async def agenda(
        self, caller: Caller, start: str | None = None, end: str | None = None, calendar: str | None = None
    ) -> dict:
        tz = await self._tz(caller)
        today = self.now().astimezone(rules.zone(tz)).date()
        try:
            first = rules.parse_date(start) if start else today
            last = rules.parse_date(end) if end else first + timedelta(days=1)
        except rules.RuleError as e:
            raise Refused(str(e)) from e
        if last < first:
            raise Refused("the end date is before the start date")
        if (last - first).days >= MAX_DAYS:
            raise Refused(f"ask for at most {MAX_DAYS} days at a time")
        begin = rules.localise(datetime.combine(first, time()), tz)
        finish = rules.localise(datetime.combine(last + timedelta(days=1), time()), tz)
        cals = await self._visible(caller)
        if calendar:
            named = [c for c in cals if c.info.name.lower() == calendar.strip().lower()]
            if not named:
                usable = ", ".join(c.info.name for c in cals) or "none"
                raise Refused(f"no calendar called {calendar!r}; the ones you can use: {usable}")
            cals = named
        elif not cals:
            raise Refused(f"you haven't shared a calendar with Uriel yet. To share one: {SHARING_STEPS}")
        found: list[tuple[ical.Entry, str, str]] = []  # entry, calendar name, item href
        for cal in cals:
            if "VEVENT" in cal.info.components:
                for item in await self.dav.events(cal.info.href, begin, finish):
                    found += [
                        (e, cal.info.name, item.href)
                        for e in self._readable(item, tz)
                        if e.kind == "event" and _overlaps(e, begin, finish, tz)
                    ]
            if "VTODO" in cal.info.components:
                for item in await self.dav.open_tasks(cal.info.href):
                    if _cancelled(item.ics):
                        continue
                    found += [
                        (e, cal.info.name, item.href)
                        for e in self._readable(item, tz)
                        if e.kind == "task"
                        and not e.completed
                        and e.start is not None
                        and _sort_key(e, tz) < finish
                    ]
        found.sort(key=lambda f: _sort_key(f[0], tz))
        late: list[tuple[ical.Entry, str, str]] = []
        window: list[tuple[ical.Entry, str, str]] = []
        for f in found:
            (late if f[0].kind == "task" and _overdue(f[0], self.now(), tz) else window).append(f)
        shown = late[-MAX_OVERDUE:] + window[:MAX_ITEMS]  # the most recent overdue ones
        shown.sort(key=lambda f: _sort_key(f[0], tz))
        items = [
            {
                "item_id": item_id(href),
                "kind": e.kind,
                "title": e.title,
                "calendar": name,
                "when": _span(e, tz),
                "recurring": e.recurring,
                **({"location": e.location} if e.location else {}),
                **({"overdue": _overdue(e, self.now(), tz)} if e.kind == "task" else {}),
            }
            for e, name, href in shown
        ]
        extra = {"more": len(window) - MAX_ITEMS} if len(window) > MAX_ITEMS else {}
        if len(late) > MAX_OVERDUE:
            extra["overdue_more"] = len(late) - MAX_OVERDUE
        return {"from": _day(first), "to": _day(last), "tz": tz, "items": items} | extra


def _as_local(entry: ical.Entry, start: datetime | date, end: datetime | date | None, tz: str) -> ical.Entry:
    """`entry` at new wall-clock times, as `_span` reads it: the change text comes from the values written."""

    def utc(value):
        return rules.localise(value, tz) if isinstance(value, datetime) else value

    return replace(entry, start=utc(start), end=utc(end) if end is not None else None)


def _overdue(entry: ical.Entry, now: datetime, tz: str) -> bool:
    """A date-only due (no time set) is late only once its day is over."""
    if isinstance(entry.start, datetime):
        return entry.start < now
    return entry.start < now.astimezone(rules.zone(tz)).date()


def _cancelled(text: str) -> bool:
    """The server's open-task query still returns tasks the person cancelled."""
    props = (ical.parse(line) for line in ical.unfold(text))
    return any(p.name == "STATUS" and p.value.upper() == "CANCELLED" for p in props)


def _overlaps(entry: ical.Entry, begin: datetime, finish: datetime, tz: str) -> bool:
    start = _sort_key(entry, tz)
    if entry.end is None:
        end = start
    elif isinstance(entry.end, datetime):
        end = entry.end
    else:
        end = rules.localise(datetime.combine(entry.end, time()), tz)
    return start < finish and (end > begin or start >= begin)
