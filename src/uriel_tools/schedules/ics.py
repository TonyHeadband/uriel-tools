"""iCalendar text (RFC 5545): the mirror's VEVENT per schedule and VTODO per finished run, and people's
reminders (a VTODO with an alarm) and events (a VEVENT)."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from uriel_tools.schedules.rules import ONCE, zone

PRODID = "-//Homelab//uriel-tools//EN"


def escape(text: str) -> str:
    """Escape a TEXT value. Every line break becomes `\\n` and other control characters are dropped, so
    person-typed text can never start a new content line (a bare CR counts as a break in some parsers)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch in "\n\t" or ch >= " " and ch != "\x7f")
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str) -> str:
    """Split a content line into lines of at most 75 octets, never inside a UTF-8 character."""
    out, current, size = [], "", 0
    for ch in line:
        n = len(ch.encode())
        if size + n > 75:
            out.append(current)
            current, size = " ", 1
        current += ch
        size += n
    out.append(current)
    return "\r\n".join(out)


def _utc(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _offset(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    sign = "-" if minutes < 0 else "+"
    return f"{sign}{abs(minutes) // 60:02d}{abs(minutes) % 60:02d}"


def _transitions(z: ZoneInfo, year: int) -> list[tuple[datetime, timedelta, timedelta]]:
    """(UTC instant, offset before, offset after) for each UTC-offset change from the year before `year`
    through ten years after it."""
    at, end = datetime(year - 1, 1, 1, tzinfo=UTC), datetime(year + 11, 1, 1, tzinfo=UTC)
    found = []
    while at < end:
        nxt = at + timedelta(hours=6)
        before, after = at.astimezone(z).utcoffset(), nxt.astimezone(z).utcoffset()
        if before != after:
            lo, hi = at, nxt
            while hi - lo > timedelta(minutes=1):
                mid = lo + timedelta(minutes=(hi - lo) // timedelta(minutes=1) // 2)
                lo, hi = (mid, hi) if mid.astimezone(z).utcoffset() == before else (lo, mid)
            found.append((hi, before, after))
        at = nxt
    return found


def vtimezone(tz: str, around: datetime) -> list[str]:
    """The VTIMEZONE RFC 5545 requires for a TZID, built from zoneinfo's transitions from the year before
    `around` to ten years after it (the mirror's series run for years). Clients that don't know the zone
    (ical.js) read their offsets from here."""
    z = zone(tz)
    lines = ["BEGIN:VTIMEZONE", f"TZID:{tz}"]
    changes = _transitions(z, around.year)
    if not changes:
        offset = _offset(around.replace(tzinfo=z).utcoffset())
        name = around.replace(tzinfo=z).tzname()
        lines += _observance("STANDARD", "19700101T000000", offset, offset, name, [])
    else:
        # One observance per distinct (from, to, name): a zone can change its standard offset inside the
        # window (Nuuk), and every RDATE of an observance shares its offsets. Larger offset is summer time,
        # even for zones (Dublin) whose tzdata flags winter.
        groups: dict[tuple[timedelta, timedelta, str | None], list[str]] = {}
        for at, before, after in changes:
            wall = f"{(at + before).replace(tzinfo=None):%Y%m%dT%H%M%S}"
            groups.setdefault((before, after, at.astimezone(z).tzname()), []).append(wall)
        for (before, after, name), walls in groups.items():
            kind = "DAYLIGHT" if after > before else "STANDARD"
            lines += _observance(kind, walls[0], _offset(before), _offset(after), name, walls[1:])
    return [*lines, "END:VTIMEZONE"]


def _observance(kind: str, start: str, was: str, now: str, name: str | None, rdates: list[str]) -> list[str]:
    return [
        f"BEGIN:{kind}",
        f"DTSTART:{start}",
        f"TZOFFSETFROM:{was}",
        f"TZOFFSETTO:{now}",
        f"TZNAME:{name}",
        *(f"RDATE:{d}" for d in rdates),
        f"END:{kind}",
    ]


def _calendar(component: list[str], tz: str | None = None, around: datetime | None = None) -> str:
    zone_lines = vtimezone(tz, around) if tz and around else []
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", *zone_lines, *component, "END:VCALENDAR"]
    return "".join(fold(line) + "\r\n" for line in lines)


def event(
    *,
    uid: str,
    schedule_id: int,
    title: str,
    prompt: str,
    tools: list[str],
    rrule: str,
    dtstart: datetime,
    tz: str,
    now: datetime,
) -> str:
    """A schedule as a recurring 15-minute event. A one-off has no RRULE."""
    description = f"{prompt}\n\nTools: {', '.join(tools) or 'none'}\nManaged by Uriel: change it in the chat."
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_utc(now)}",
        f"SUMMARY:{escape(title)}",
        f"DTSTART;TZID={tz}:{dtstart:%Y%m%dT%H%M%S}",
        "DURATION:PT15M",
        *([] if rrule == ONCE else [f"RRULE:{rrule}"]),
        f"DESCRIPTION:{escape(description)}",
        f"X-URIEL-SCHEDULE-ID:{schedule_id}",
        "END:VEVENT",
    ]
    return _calendar(lines, tz, dtstart if dtstart.year > now.year else now)


def todo(*, uid: str, summary: str, description: str, completed: datetime, now: datetime) -> str:
    """A completed task: how a run (or a cancellation) went."""
    lines = [
        "BEGIN:VTODO",
        f"UID:{uid}",
        f"DTSTAMP:{_utc(now)}",
        f"SUMMARY:{escape(summary)}",
        "STATUS:COMPLETED",
        f"COMPLETED:{_utc(completed)}",
        "PERCENT-COMPLETE:100",
        f"DESCRIPTION:{escape(description)}",
        "END:VTODO",
    ]
    return _calendar(lines)


def _alarm(trigger: str) -> list[str]:
    return ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Reminder", trigger, "END:VALARM"]


def reminder(*, uid: str, title: str, notes: str, due: datetime, tz: str, owner: str, now: datetime) -> str:
    """A person's reminder: a task due at a wall-clock time, with an alarm at that instant.

    The alarm is relative to DUE, so an app that moves DUE moves the alarm with it.
    """
    lines = [
        "BEGIN:VTODO",
        f"UID:{uid}",
        f"DTSTAMP:{_utc(now)}",
        f"SUMMARY:{escape(title)}",
        *([f"DESCRIPTION:{escape(notes)}"] if notes else []),
        f"DUE;TZID={tz}:{due:%Y%m%dT%H%M%S}",
        "STATUS:NEEDS-ACTION",
        f"X-URIEL-OWNER:{owner}",
        *_alarm("TRIGGER;RELATED=END:PT0S"),
        "END:VTODO",
    ]
    return _calendar(lines, tz, due)


def appointment(
    *,
    uid: str,
    title: str,
    notes: str,
    location: str,
    day: date,
    start: time | None,
    end: time | None,
    tz: str,
    alarm_minutes: int | None,
    owner: str,
    now: datetime,
) -> str:
    """A person's event: timed (start and end on `day`) or all-day (neither)."""
    if (start is None) != (end is None):
        raise ValueError("a timed event needs both start and end")
    zone_at = None
    if start is None or end is None:
        when = [f"DTSTART;VALUE=DATE:{day:%Y%m%d}", f"DTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}"]
    else:
        zone_at = datetime.combine(day, start)
        when = [
            f"DTSTART;TZID={tz}:{datetime.combine(day, start):%Y%m%dT%H%M%S}",
            f"DTEND;TZID={tz}:{datetime.combine(day, end):%Y%m%dT%H%M%S}",
        ]
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_utc(now)}",
        f"SUMMARY:{escape(title)}",
        *([f"DESCRIPTION:{escape(notes)}"] if notes else []),
        *([f"LOCATION:{escape(location)}"] if location else []),
        *when,
        f"X-URIEL-OWNER:{owner}",
        *(_alarm(f"TRIGGER:-PT{alarm_minutes}M") if alarm_minutes is not None else []),
        "END:VEVENT",
    ]
    return _calendar(lines, tz, zone_at)
