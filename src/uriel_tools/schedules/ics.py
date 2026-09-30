"""iCalendar text for the mirror: one VEVENT per schedule and one VTODO per finished run (RFC 5545)."""

from datetime import UTC, datetime

from uriel_tools.schedules.rules import ONCE

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


def _calendar(component: list[str]) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", *component, "END:VCALENDAR"]
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
    return _calendar(lines)


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
