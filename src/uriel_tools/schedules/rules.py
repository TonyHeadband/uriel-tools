"""Recurrence: structured fields in, an RFC 5545 rule out, occurrences as UTC instants.

Rules run on wall-clock time in the schedule's own timezone, so "9:00 daily" stays 9:00 across DST.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from itertools import islice
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from dateutil.rrule import rrulestr

DEFAULT_TZ = "America/Toronto"
ONCE = "FREQ=DAILY;COUNT=1"
ONCE_GRACE = timedelta(minutes=10)
MAX_GRACE = timedelta(hours=2)

Frequency = Literal["once", "daily", "weekdays", "weekly", "monthly"]
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
BYDAY = {"mon": "MO", "tue": "TU", "wed": "WE", "thu": "TH", "fri": "FR", "sat": "SA", "sun": "SU"}
DAY_NAMES = {
    "MO": "Monday",
    "TU": "Tuesday",
    "WE": "Wednesday",
    "TH": "Thursday",
    "FR": "Friday",
    "SA": "Saturday",
    "SU": "Sunday",
}
WEEKDAYS = "MO,TU,WE,TH,FR"
_CLOCK = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_WHEN = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{1,2}:\d{2})(?::[0-5]\d)?$")
DATE_FORMAT = "date must be YYYY-MM-DD, e.g. 2026-10-03"


class RuleError(ValueError):
    """Fields that don't make a schedule; the message says what to fix."""


@dataclass(frozen=True)
class Rule:
    rrule: str  # RFC 5545, without DTSTART
    dtstart: datetime  # naive wall-clock time in tz: the first occurrence
    tz: str


@cache
def _zone_names() -> dict[str, str]:
    return {name.lower(): name for name in available_timezones()}


def zone_name(tz: str) -> str:
    """The canonical IANA name for `tz`, matched without regard to case ("america/toronto" works)."""
    name = _zone_names().get(tz.strip().lower())
    if name is None:
        raise RuleError(f"{tz!r} is not a timezone name: use one like America/Toronto or Europe/Rome")
    return name


def zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise RuleError(f"{tz!r} is not a timezone name: use one like America/Toronto or Europe/Rome") from e


def localise(naive: datetime, tz: str) -> datetime:
    """A wall-clock time in `tz` as a UTC instant.

    A time that doesn't exist (the spring-forward gap) moves to the first minute after it that does. A time
    that happens twice (fall-back) is the first of the two, which is what fold=0 means.
    """
    z = zone(tz)
    probe = naive
    for _ in range(24 * 60 + 1):  # the longest gap on record is a whole day (Samoa, 2011)
        aware = probe.replace(tzinfo=z)
        if aware.astimezone(UTC).astimezone(z).replace(tzinfo=None) == probe:
            return aware.astimezone(UTC)
        probe += timedelta(minutes=1)
    raise RuleError(f"{naive:%Y-%m-%d %H:%M} doesn't exist in {tz}")


def parse_clock(at: str) -> time:
    match = _CLOCK.match(at.strip())
    if match is None:
        raise RuleError("time must be HH:MM on a 24-hour clock, e.g. 09:00 or 18:30")
    return time(int(match[1]), int(match[2]))


def parse_date(on: str) -> date:
    try:
        return date.fromisoformat(on.strip())
    except ValueError as e:
        raise RuleError(DATE_FORMAT) from e


def parse_when(value: str) -> tuple[date | None, time]:
    """HH:MM, or YYYY-MM-DDTHH:MM[:SS] with its date: models often send a time as a full ISO datetime.
    Seconds are dropped."""
    match = _WHEN.match(value.strip())
    if match is None:
        return None, parse_clock(value)
    return parse_date(match[1]), parse_clock(match[2])


def occurrences(rrule: str, dtstart: datetime, tz: str, after: datetime) -> Iterator[datetime]:
    """Occurrences strictly after the UTC instant `after`, in order, as UTC instants."""
    z = zone(tz)
    # Start a day early in wall time: a localised occurrence can land hours away from its wall-clock reading.
    begin = max(dtstart, after.astimezone(z).replace(tzinfo=None) - timedelta(days=1))
    for naive in rrulestr(rrule, dtstart=dtstart).xafter(begin, inc=True):
        at = localise(naive, tz)
        if at > after:
            yield at


def upcoming(rrule: str, dtstart: datetime, tz: str, after: datetime, count: int) -> list[datetime]:
    return list(islice(occurrences(rrule, dtstart, tz, after), count))


def grace(rrule: str, dtstart: datetime, tz: str, due: datetime) -> timedelta:
    """How late an occurrence may still run: 10 minutes for a one-off, otherwise a quarter of the time to the
    next occurrence, at most 2 hours."""
    if rrule == ONCE:
        return ONCE_GRACE
    following = next(occurrences(rrule, dtstart, tz, due), None)
    return MAX_GRACE if following is None else min((following - due) / 4, MAX_GRACE)


def build(
    frequency: Frequency,
    at: str,
    tz: str,
    now: datetime,
    *,
    weekdays: list[str] | None = None,
    on: str | None = None,
    day_of_month: int | None = None,
) -> Rule:
    """The rule for the structured fields, starting at its first occurrence after `now` (UTC)."""
    tz = zone_name(tz)
    clock = parse_clock(at)
    if frequency == "once":
        if not on:
            raise RuleError("a one-off schedule needs a date (YYYY-MM-DD)")
        start = datetime.combine(parse_date(on), clock)
        if localise(start, tz) <= now:
            raise RuleError(
                f"that time has already passed (it is now {now_in(now, tz)}): pick a later date or time"
            )
        return Rule(ONCE, start, tz)
    if frequency == "daily":
        rrule = "FREQ=DAILY"
    elif frequency == "weekdays":
        rrule = f"FREQ=WEEKLY;BYDAY={WEEKDAYS}"
    elif frequency == "weekly":
        if not weekdays:
            raise RuleError('a weekly schedule needs weekdays, e.g. ["mon", "thu"]')
        wanted = {w.strip().lower()[:3] for w in weekdays}
        unknown = wanted - BYDAY.keys()
        if unknown:
            raise RuleError(f"unknown weekdays {sorted(unknown)}: use mon, tue, wed, thu, fri, sat, sun")
        rrule = "FREQ=WEEKLY;BYDAY=" + ",".join(code for day, code in BYDAY.items() if day in wanted)
    elif frequency == "monthly":
        if day_of_month is None or not 1 <= day_of_month <= 31:
            raise RuleError("a monthly schedule needs day_of_month, from 1 to 31")
        rrule = f"FREQ=MONTHLY;BYMONTHDAY={day_of_month}"
    else:
        raise RuleError("frequency must be once, daily, weekdays, weekly or monthly")
    today = now.astimezone(zone(tz)).date()
    candidate = datetime.combine(today, clock)
    first = next(occurrences(rrule, candidate, tz, now))
    local = first.astimezone(zone(tz)).replace(tzinfo=None)
    # A first run moved out of a DST gap keeps its wall-clock reading, so later runs stay at `at`.
    return Rule(rrule, datetime.combine(local.date(), clock), tz)


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _clock(t: datetime) -> str:
    return f"{t.hour}:{t.minute:02d}"


def place(tz: str) -> str:
    return tz.rsplit("/", 1)[-1].replace("_", " ")


def describe(rrule: str, dtstart: datetime, tz: str) -> str:
    """Plain language for a rule, e.g. "every weekday at 9:00 (Toronto time)"."""
    parts = dict(p.split("=", 1) for p in rrule.split(";"))
    clock = _clock(dtstart)
    if rrule == ONCE:
        text = f"once, on {dtstart:%A} {dtstart.day} {dtstart:%B %Y} at {clock}"
    elif parts["FREQ"] == "DAILY":
        text = f"every day at {clock}"
    elif parts.get("BYDAY") == WEEKDAYS:
        text = f"every weekday at {clock}"
    elif parts["FREQ"] == "WEEKLY":
        text = f"every {_join([DAY_NAMES[d] for d in parts['BYDAY'].split(',')])} at {clock}"
    else:
        day = int(parts["BYMONTHDAY"])
        text = f"on day {day} of every month at {clock}"
        if day > 28:
            text += ", skipping months without that day"
    return f"{text} ({place(tz)} time)"


def now_in(now: datetime, tz: str) -> str:
    """The time in `tz`, for a model that may not know today's date: "Fri 6 Mar 2026, 10:00 Toronto time"."""
    return f"{local(now, tz)} {place(tz)} time"


def iso(at: datetime) -> str:
    """A UTC instant as ISO 8601 with Z."""
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def local(at: datetime, tz: str) -> str:
    """A UTC instant for people, in `tz`: "Wed 30 Sep 2026, 9:00"."""
    t = at.astimezone(zone(tz))
    return f"{t:%a} {t.day} {t:%b %Y}, {_clock(t)}"
