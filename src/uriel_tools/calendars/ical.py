"""Just enough iCalendar reading and editing for people's calendars: items other clients made included.

Edits change the lines they must and keep every other one, so what the Calendar app set (colour, attendees,
categories) survives a move.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from uriel_tools.schedules import ics, rules

_DURATION = re.compile(r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


@dataclass(frozen=True)
class Prop:
    name: str
    params: dict[str, str]
    value: str


@dataclass(frozen=True)
class Entry:
    kind: Literal["event", "task"]
    title: str
    start: datetime | date | None  # an event's start, a task's due; datetimes are UTC
    end: datetime | date | None
    location: str
    recurring: bool
    completed: bool


def unfold(text: str) -> list[str]:
    # Not str.splitlines(): it also breaks on U+2028, U+0085 and friends, which are legal inside TEXT.
    return [line for line in re.split(r"\r?\n", re.sub(r"\r?\n[ \t]", "", text)) if line]


def parse(line: str) -> Prop:
    head, _, value = _split(line)
    name, *params = head.split(";")
    pairs = (p.partition("=") for p in params)
    return Prop(name.upper(), {k.upper(): v.strip('"') for k, _, v in pairs}, value)


def _split(line: str) -> tuple[str, str, str]:
    """name;params : value — the first colon outside a quoted parameter value."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            return line[:i], ":", line[i + 1 :]
    return line, "", ""


def unescape(value: str) -> str:
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m[1] in "nN" else m[1], value)


def _components(lines: list[str]) -> list[tuple[str, list[str]]]:
    """Each top-level VEVENT/VTODO with its own lines; nested VALARM lines are left out."""
    found, depth, current, kind = [], 0, [], ""
    for line in lines:
        upper = line.upper()
        if upper in ("BEGIN:VEVENT", "BEGIN:VTODO") and depth == 0:
            kind, current, depth = upper[6:], [], 1
        elif upper.startswith("BEGIN:") and depth:
            depth += 1
        elif upper.startswith("END:") and depth:
            depth -= 1
            if depth == 0:
                found.append((kind, current))
        elif depth == 1:
            current.append(line)
    return found


def when(prop: Prop, tz: str) -> datetime | date:
    value = prop.value.strip()
    if prop.params.get("VALUE", "").upper() == "DATE" or len(value) == 8:
        return datetime.strptime(value, "%Y%m%d").date()
    naive = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return naive.replace(tzinfo=UTC)
    # A TZID we can't resolve (Outlook names) reads as the person's own zone rather than failing.
    zone_name = prop.params.get("TZID", tz)
    try:
        return rules.localise(naive, zone_name)
    except rules.RuleError:
        return rules.localise(naive, tz)


def duration(value: str) -> timedelta:
    m = _DURATION.match(value.strip())
    if m is None:
        return timedelta()
    sign, weeks, days, hours, minutes, seconds = m.groups()
    span = timedelta(
        weeks=int(weeks or 0),
        days=int(days or 0),
        hours=int(hours or 0),
        minutes=int(minutes or 0),
        seconds=int(seconds or 0),
    )
    return -span if sign == "-" else span


def _entry(kind: str, lines: list[str], tz: str) -> Entry:
    props: dict[str, Prop] = {}
    for line in lines:
        p = parse(line)
        props.setdefault(p.name, p)
    task = kind == "VTODO"
    first = props.get("DUE" if task else "DTSTART") or (props.get("DTSTART") if task else None)
    start = when(first, tz) if first else None
    end = None
    if not task:
        if "DTEND" in props:
            end = when(props["DTEND"], tz)
        elif "DURATION" in props and start is not None:
            end = start + duration(props["DURATION"].value)
    status = props["STATUS"].value.upper() if "STATUS" in props else ""
    return Entry(
        kind="task" if task else "event",
        title=unescape(props["SUMMARY"].value) if "SUMMARY" in props else "(untitled)",
        start=start,
        end=end,
        location=unescape(props["LOCATION"].value) if "LOCATION" in props else "",
        recurring="RRULE" in props or "RECURRENCE-ID" in props,
        completed=status == "COMPLETED" or "COMPLETED" in props,
    )


def entries(text: str, tz: str) -> list[Entry]:
    return [_entry(kind, lines, tz) for kind, lines in _components(unfold(text))]


def master(text: str, tz: str) -> Entry | None:
    for kind, lines in _components(unfold(text)):
        if not any(parse(line).name == "RECURRENCE-ID" for line in lines):
            return _entry(kind, lines, tz)
    return None


def has_exceptions(text: str) -> bool:
    """A series with occurrences cancelled (EXDATE), added (RDATE) or changed (a RECURRENCE-ID override).
    Those are pinned to the old times, so retiming the series alone would leave them behind."""
    exceptions = ("EXDATE", "RDATE", "RECURRENCE-ID")
    return any(parse(line).name in exceptions for _, lines in _components(unfold(text)) for line in lines)


def _stamp(at: datetime | date, tz: str) -> tuple[str, str]:
    if isinstance(at, datetime):
        return f";TZID={tz}", f"{at:%Y%m%dT%H%M%S}"
    return ";VALUE=DATE", f"{at:%Y%m%d}"


def _edit_master(text: str, change: Callable[[str, list[str]], list[str]]) -> str:
    """Rewrites the master component (the one without RECURRENCE-ID) through `change(kind, its_lines)`."""
    lines = unfold(text)
    out, i = [], 0
    done = False
    while i < len(lines):
        upper = lines[i].upper()
        if not done and upper in ("BEGIN:VEVENT", "BEGIN:VTODO"):
            j, depth = i + 1, 1
            while depth:
                if j >= len(lines):
                    raise ValueError(f"the {upper[6:]} is cut off: it has no END line")
                u = lines[j].upper()
                depth += u.startswith("BEGIN:") - u.startswith("END:")
                j += 1
            block = lines[i:j]
            if not any(parse(x).name == "RECURRENCE-ID" for x in block[1:-1]):
                out.extend(change(upper[6:], block))
                done = True
            else:
                out.extend(block)
            i = j
            continue
        out.append(lines[i])
        i += 1
    if not done:
        raise ValueError("the item has no main component to edit (only overrides of a series)")
    return "".join(ics.fold(line) + "\r\n" for line in out)


def _replace(block: list[str], name: str, new: list[str]) -> list[str]:
    """`block` with its own `name` lines (not a nested alarm's) replaced by `new`, placed before END."""
    kept, depth = [], 0
    for line in block[1:-1]:
        u = line.upper()
        if u.startswith("BEGIN:"):
            depth += 1
        elif u.startswith("END:"):
            depth -= 1
        if depth == 0 and not u.startswith("END:") and parse(line).name == name:
            continue
        kept.append(line)
    return [block[0], *kept, *new, block[-1]]


def _own(block: list[str], name: str) -> Prop | None:
    """The component's own `name` property, not a nested alarm's."""
    depth = 0
    for line in block[1:-1]:
        u = line.upper()
        if u.startswith("BEGIN:"):
            depth += 1
        elif u.startswith("END:"):
            depth -= 1
        elif depth == 0 and (p := parse(line)).name == name:
            return p
    return None


def _append(block: list[str], new: list[str]) -> list[str]:
    return [*block[:-1], *new, block[-1]]


def _stamps(block: list[str], now: datetime) -> list[str]:
    seq = next((int(p.value) for p in map(parse, block) if p.name == "SEQUENCE" and p.value.isdigit()), 0)
    for name in ("DTSTAMP", "LAST-MODIFIED", "SEQUENCE"):
        block = _replace(block, name, [])
    stamp = ics._utc(now)
    return _append(block, [f"DTSTAMP:{stamp}", f"LAST-MODIFIED:{stamp}", f"SEQUENCE:{seq + 1}"])


def _is_absolute_trigger(line: str) -> bool:
    p = parse(line)
    return p.name == "TRIGGER" and p.params.get("VALUE", "").upper() == "DATE-TIME"


def _kept_length(old_start: Prop, old_end: Prop, start: datetime | date, tz: str) -> list[str]:
    """The DTEND line that keeps the event as long as it was when it starts at `start`."""
    was_start, was_end = when(old_start, tz), when(old_end, tz)
    if isinstance(start, datetime) and isinstance(was_start, datetime) and isinstance(was_end, datetime):
        end = (
            (rules.localise(start, tz) + (was_end - was_start))
            .astimezone(rules.zone(tz))
            .replace(tzinfo=None)
        )
    elif not isinstance(start, datetime) and not isinstance(was_start, datetime) and type(was_end) is date:
        end = was_end + (start - was_start)
    else:
        return []  # timed became all-day or the reverse: there is no length to keep
    param, value = _stamp(end, tz)
    return [f"DTEND{param}:{value}"]


def retime(text: str, *, start: datetime | date, end: datetime | date | None, tz: str, now: datetime) -> str:
    """`start` and `end` are naive wall-clock datetimes in `tz`, or dates for all-day items."""

    def change(kind: str, block: list[str]) -> list[str]:
        param, value = _stamp(start, tz)
        if kind == "VTODO":
            block = _replace(block, "DUE", [f"DUE{param}:{value}"])
            if isinstance(start, datetime):
                at = ics._utc(rules.localise(start, tz))
                block = [f"TRIGGER;VALUE=DATE-TIME:{at}" if _is_absolute_trigger(x) else x for x in block]
        else:
            old_start, old_end = _own(block, "DTSTART"), _own(block, "DTEND")
            block = _replace(block, "DTSTART", [f"DTSTART{param}:{value}"])
            if end is not None:
                end_param, end_value = _stamp(end, tz)
                block = _replace(_replace(block, "DURATION", []), "DTEND", [f"DTEND{end_param}:{end_value}"])
            elif old_start is not None and old_end is not None:
                block = _replace(block, "DTEND", _kept_length(old_start, old_end, start, tz))
        return _stamps(block, now)

    return _with_timezone(_edit_master(text, change), tz, start)


def _with_timezone(text: str, tz: str, at: datetime | date) -> str:
    """`text` with a VTIMEZONE for `tz` if it writes a TZID and has none: RFC 5545 requires one, and
    clients that don't know the zone (ical.js) corrupt the time without it."""
    lines = unfold(text)
    if not isinstance(at, datetime) or f"TZID:{tz}".upper() in (x.upper() for x in lines):
        return text
    first = next(i for i, x in enumerate(lines) if x.upper() in ("BEGIN:VEVENT", "BEGIN:VTODO"))
    lines[first:first] = ics.vtimezone(tz, at)
    return "".join(ics.fold(line) + "\r\n" for line in lines)


def complete(text: str, now: datetime) -> str:
    stamp = ics._utc(now)

    def change(kind: str, block: list[str]) -> list[str]:
        for name, value in (("STATUS", "COMPLETED"), ("COMPLETED", stamp), ("PERCENT-COMPLETE", "100")):
            block = _replace(block, name, [f"{name}:{value}"])
        return _stamps(block, now)

    return _edit_master(text, change)
