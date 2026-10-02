from datetime import UTC, date, datetime, time

import pytest

from uriel_tools.calendars import ical
from uriel_tools.schedules import ics

TZ = "America/Toronto"
NOW = datetime(2026, 9, 29, 23, 0, tzinfo=UTC)  # Tue 29 Sep, 19:00 in Toronto


def lines(text: str) -> list[str]:
    return text.replace("\r\n ", "").split("\r\n")


def test_a_reminder_is_a_task_whose_alarm_rings_at_the_due_time():
    text = ics.reminder(
        uid="uriel-1",
        title="Call the dentist",
        notes="Ask about Friday",
        due=datetime(2026, 10, 1, 15, 0),
        tz=TZ,
        owner="anthony-headband",
        now=NOW,
    )
    ls = lines(text)
    for want in [
        "BEGIN:VTODO",
        "UID:uriel-1",
        "SUMMARY:Call the dentist",
        "DUE;TZID=America/Toronto:20261001T150000",
        "STATUS:NEEDS-ACTION",
        "X-URIEL-OWNER:anthony-headband",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        "TRIGGER;RELATED=END:PT0S",
        "DESCRIPTION:Ask about Friday",
    ]:
        assert want in ls
    assert ls.index("END:VALARM") < ls.index("END:VTODO")


def test_a_reminder_in_the_spring_forward_gap_rings_when_the_clock_reaches_it():
    text = ics.reminder(
        uid="u", title="t", notes="", due=datetime(2026, 3, 8, 2, 30), tz=TZ, owner="o", now=NOW
    )
    assert "TRIGGER;RELATED=END:PT0S" in lines(text)  # relative to DUE, so a client that moves DUE moves it


def test_person_typed_text_cannot_break_the_calendar():
    text = ics.appointment(
        uid="u",
        title="Recital, Lil-B; hall\\2\nBEGIN:VEVENT",
        notes="a\r\nb",
        location="Hall; A",
        day=date(2026, 10, 2),
        start=time(19),
        end=time(20),
        tz=TZ,
        alarm_minutes=None,
        owner="o",
        now=NOW,
    )
    ls = lines(text)
    assert "SUMMARY:Recital\\, Lil-B\\; hall\\\\2\\nBEGIN:VEVENT" in ls
    assert ls.count("BEGIN:VEVENT") == 1
    assert "LOCATION:Hall\\; A" in ls and "DESCRIPTION:a\\nb" in ls


def test_an_event_with_an_alarm_and_an_all_day_one():
    timed = lines(
        ics.appointment(
            uid="u",
            title="t",
            notes="",
            location="",
            day=date(2026, 10, 2),
            start=time(19),
            end=time(20, 30),
            tz=TZ,
            alarm_minutes=30,
            owner="o",
            now=NOW,
        )
    )
    assert "DTSTART;TZID=America/Toronto:20261002T190000" in timed
    assert "DTEND;TZID=America/Toronto:20261002T203000" in timed and "TRIGGER:-PT30M" in timed
    allday = lines(
        ics.appointment(
            uid="u",
            title="t",
            notes="",
            location="",
            day=date(2026, 10, 2),
            start=None,
            end=None,
            tz=TZ,
            alarm_minutes=None,
            owner="o",
            now=NOW,
        )
    )
    assert "DTSTART;VALUE=DATE:20261002" in allday and "DTEND;VALUE=DATE:20261003" in allday
    assert "BEGIN:VALARM" not in allday and not any(x.startswith("LOCATION") for x in allday)


FROM_ANOTHER_CLIENT = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:x\r\nSUMMARY:Lunch\\, with Ann\r\n"
    "DTSTART:20261001T160000Z\r\nDURATION:PT1H30M\r\nLOCATION:Caf\r\n é\r\nCOLOR:red\r\nSEQUENCE:2\r\n"
    "BEGIN:VALARM\r\nTRIGGER:-PT10M\r\nACTION:DISPLAY\r\nEND:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


def test_entries_read_utc_duration_and_folded_lines():
    [e] = ical.entries(FROM_ANOTHER_CLIENT, TZ)
    assert (e.kind, e.title, e.location, e.recurring) == ("event", "Lunch, with Ann", "Café", False)
    assert e.start == datetime(2026, 10, 1, 16, tzinfo=UTC) and e.end == datetime(
        2026, 10, 1, 17, 30, tzinfo=UTC
    )


def test_floating_and_date_values():
    floating = FROM_ANOTHER_CLIENT.replace("DTSTART:20261001T160000Z", "DTSTART:20261001T120000")
    assert ical.entries(floating, TZ)[0].start == datetime(2026, 10, 1, 16, tzinfo=UTC)  # read as local time
    allday = FROM_ANOTHER_CLIENT.replace(
        "DTSTART:20261001T160000Z\r\nDURATION:PT1H30M",
        "DTSTART;VALUE=DATE:20261001\r\nDTEND;VALUE=DATE:20261002",
    )
    [e] = ical.entries(allday, TZ)
    assert (e.start, e.end) == (date(2026, 10, 1), date(2026, 10, 2))


def test_an_expanded_series_gives_one_recurring_entry_per_occurrence():
    expanded = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:s\r\nSUMMARY:Piano\r\nRECURRENCE-ID:20261001T200000Z\r\n"
        "DTSTART:20261001T200000Z\r\nDTEND:20261001T210000Z\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:s\r\n"
        "SUMMARY:Piano\r\nRECURRENCE-ID:20261008T200000Z\r\nDTSTART:20261008T200000Z\r\n"
        "DTEND:20261008T210000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    assert [(e.start.day, e.recurring) for e in ical.entries(expanded, TZ)] == [(1, True), (8, True)]


def test_retime_moves_only_what_it_must():
    moved = ical.retime(FROM_ANOTHER_CLIENT, start=datetime(2026, 10, 2, 13, 0), end=None, tz=TZ, now=NOW)
    ls = lines(moved)
    assert "DTSTART;TZID=America/Toronto:20261002T130000" in ls and "DURATION:PT1H30M" in ls
    assert "COLOR:red" in ls and "LOCATION:Café" in ls and "TRIGGER:-PT10M" in ls and "SEQUENCE:3" in ls
    assert "DTSTAMP:20260929T230000Z" in ls and "LAST-MODIFIED:20260929T230000Z" in ls
    with_end = lines(
        ical.retime(
            FROM_ANOTHER_CLIENT,
            start=datetime(2026, 10, 2, 13),
            end=datetime(2026, 10, 2, 15),
            tz=TZ,
            now=NOW,
        )
    )
    assert "DTEND;TZID=America/Toronto:20261002T150000" in with_end and not any(
        x.startswith("DURATION") for x in with_end
    )


def test_retime_moves_a_tasks_due_and_its_alarm():
    task = ics.reminder(
        uid="u", title="t", notes="", due=datetime(2026, 10, 1, 15), tz=TZ, owner="o", now=NOW
    )
    ls = lines(ical.retime(task, start=datetime(2026, 10, 1, 17), end=None, tz=TZ, now=NOW))
    assert "DUE;TZID=America/Toronto:20261001T170000" in ls and "TRIGGER;RELATED=END:PT0S" in ls


def test_retime_still_rewrites_an_absolute_alarm_made_elsewhere():
    task = (
        "BEGIN:VCALENDAR\r\nBEGIN:VTODO\r\nUID:x\r\nDUE;TZID=America/Toronto:20261001T150000\r\n"
        "BEGIN:VALARM\r\nTRIGGER;VALUE=DATE-TIME:20261001T190000Z\r\nACTION:DISPLAY\r\nEND:VALARM\r\n"
        "END:VTODO\r\nEND:VCALENDAR\r\n"
    )
    ls = lines(ical.retime(task, start=datetime(2026, 10, 1, 17), end=None, tz=TZ, now=NOW))
    assert "TRIGGER;VALUE=DATE-TIME:20261001T210000Z" in ls


def test_retime_leaves_overrides_of_a_series_alone():
    # The service refuses to move a series with overrides; retime stays safe for other callers.
    series = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:s\r\nRRULE:FREQ=WEEKLY\r\n"
        "DTSTART;TZID=America/Toronto:20261001T160000\r\nDTEND;TZID=America/Toronto:20261001T170000\r\n"
        "END:VEVENT\r\nBEGIN:VEVENT\r\nUID:s\r\nRECURRENCE-ID;TZID=America/Toronto:20261008T160000\r\n"
        "DTSTART;TZID=America/Toronto:20261008T180000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    assert ical.master(series, TZ).recurring is True
    ls = lines(
        ical.retime(series, start=datetime(2026, 10, 1, 17), end=datetime(2026, 10, 1, 18), tz=TZ, now=NOW)
    )
    assert "DTSTART;TZID=America/Toronto:20261001T170000" in ls
    assert "DTSTART;TZID=America/Toronto:20261008T180000" in ls  # the override keeps its own time


def test_complete_marks_a_task_done():
    task = ics.reminder(
        uid="u", title="t", notes="", due=datetime(2026, 10, 1, 15), tz=TZ, owner="o", now=NOW
    )
    ls = lines(ical.complete(task, NOW))
    assert "STATUS:COMPLETED" in ls and "COMPLETED:20260929T230000Z" in ls and "PERCENT-COMPLETE:100" in ls
    assert "STATUS:NEEDS-ACTION" not in ls and ical.master(ical.complete(task, NOW), TZ).completed


def test_unescape_undoes_escape():
    for text in ["a\\b, c; d\ne", "a\\nb", "\\\\n,;"]:
        assert ical.unescape(ics.escape(text)) == text


def test_retime_without_an_end_keeps_the_events_length():
    event = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:x\r\nDTSTART;TZID=America/Toronto:20261001T120000\r\n"
        "DTEND;TZID=America/Toronto:20261001T130000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    ls = lines(ical.retime(event, start=datetime(2026, 10, 1, 15), end=None, tz=TZ, now=NOW))
    assert "DTSTART;TZID=America/Toronto:20261001T150000" in ls
    assert "DTEND;TZID=America/Toronto:20261001T160000" in ls
    allday = event.replace("TZID=America/Toronto:20261001T120000", "VALUE=DATE:20261001").replace(
        "TZID=America/Toronto:20261001T130000", "VALUE=DATE:20261003"
    )
    ls = lines(ical.retime(allday, start=date(2026, 10, 5), end=None, tz=TZ, now=NOW))
    assert "DTSTART;VALUE=DATE:20261005" in ls and "DTEND;VALUE=DATE:20261007" in ls


def test_a_timed_event_needs_both_start_and_end():
    with pytest.raises(ValueError, match="both start and end"):
        ics.appointment(
            uid="u", title="t", notes="", location="", day=date(2026, 10, 2), start=time(19), end=None,
            tz=TZ, alarm_minutes=None, owner="o", now=NOW,
        )  # fmt: skip


def test_editing_needs_a_main_component_and_a_whole_one():
    overrides_only = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:s\r\nRECURRENCE-ID:20261008T160000Z\r\n"
        "DTSTART:20261008T180000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    with pytest.raises(ValueError, match="no main component"):
        ical.retime(overrides_only, start=datetime(2026, 10, 1, 17), end=None, tz=TZ, now=NOW)
    with pytest.raises(ValueError, match="no main component"):
        ical.complete(overrides_only, NOW)
    truncated = "BEGIN:VCALENDAR\r\nBEGIN:VTODO\r\nUID:x\r\nBEGIN:VALARM\r\n"
    with pytest.raises(ValueError, match="cut off"):
        ical.complete(truncated, NOW)


def test_unusual_line_separators_inside_text_do_not_split_a_line():
    text = FROM_ANOTHER_CLIENT.replace("Lunch\\, with Ann", "Lunch with\x0bAnn")
    assert ical.entries(text, TZ)[0].title == "Lunch with\x0bAnn"


def reminder_text(tz: str = TZ) -> str:
    return ics.reminder(
        uid="u", title="t", notes="", due=datetime(2026, 10, 1, 15), tz=tz, owner="o", now=NOW
    )


def test_a_reminder_carries_the_vtimezone_its_tzid_needs():
    ls = lines(reminder_text())
    assert "TZID:America/Toronto" in ls
    assert ls.index("BEGIN:VTIMEZONE") < ls.index("END:VTIMEZONE") < ls.index("BEGIN:VTODO")
    zone = ls[ls.index("BEGIN:VTIMEZONE") : ls.index("END:VTIMEZONE")]
    daylight = zone[zone.index("BEGIN:DAYLIGHT") : zone.index("END:DAYLIGHT")]
    standard = zone[zone.index("BEGIN:STANDARD") : zone.index("END:STANDARD")]
    assert "TZOFFSETFROM:-0500" in daylight and "TZOFFSETTO:-0400" in daylight and "TZNAME:EDT" in daylight
    assert "TZOFFSETFROM:-0400" in standard and "TZOFFSETTO:-0500" in standard and "TZNAME:EST" in standard
    assert any(x.startswith("DTSTART:") for x in daylight) and any(x.startswith("DTSTART:") for x in standard)
    assert "RDATE:20260308T020000" in daylight and "RDATE:20261101T020000" in standard


def test_vtimezone_covers_the_year_after_too():
    zone = lines("".join(x + "\r\n" for x in ics.vtimezone(TZ, datetime(2026, 12, 1))))
    assert "RDATE:20270314T020000" in zone and "RDATE:20271107T020000" in zone


def test_a_zone_without_dst_gets_a_single_standard():
    ls = lines(reminder_text("Asia/Tokyo"))
    assert "TZID:Asia/Tokyo" in ls and "BEGIN:STANDARD" in ls and "BEGIN:DAYLIGHT" not in ls
    assert "TZOFFSETFROM:+0900" in ls and "TZOFFSETTO:+0900" in ls and "TZNAME:JST" in ls


def test_a_timed_appointment_has_a_vtimezone_and_an_all_day_one_has_none():
    def make(start, end):
        return lines(
            ics.appointment(
                uid="u",
                title="t",
                notes="",
                location="",
                day=date(2026, 10, 2),
                start=start,
                end=end,
                tz=TZ,
                alarm_minutes=None,
                owner="o",
                now=NOW,
            )
        )

    timed = make(time(19), time(20))
    assert "TZID:America/Toronto" in timed
    assert timed.index("END:VTIMEZONE") < timed.index("BEGIN:VEVENT")
    assert "BEGIN:VTIMEZONE" not in make(None, None)


def test_the_mirror_event_has_a_vtimezone():
    text = ics.event(
        uid="u",
        schedule_id=1,
        title="t",
        prompt="p",
        tools=[],
        rrule="FREQ=DAILY",
        dtstart=datetime(2026, 10, 1, 8),
        tz=TZ,
        now=NOW,
    )
    assert lines(text).count("BEGIN:VTIMEZONE") == 1


def test_retime_adds_the_vtimezone_an_item_from_elsewhere_lacks():
    bare = (
        "BEGIN:VCALENDAR\r\nBEGIN:VTODO\r\nUID:x\r\nDUE;TZID=America/Toronto:20261001T150000\r\n"
        "END:VTODO\r\nEND:VCALENDAR\r\n"
    )
    ls = lines(ical.retime(bare, start=datetime(2026, 10, 1, 17), end=None, tz=TZ, now=NOW))
    assert ls.count("BEGIN:VTIMEZONE") == 1 and "TZID:America/Toronto" in ls
    assert ls.index("END:VTIMEZONE") < ls.index("BEGIN:VTODO")
    assert ls[0] == "BEGIN:VCALENDAR" and ls[-2] == "END:VCALENDAR"


def test_retime_does_not_duplicate_an_existing_vtimezone():
    ls = lines(ical.retime(reminder_text(), start=datetime(2026, 10, 1, 17), end=None, tz=TZ, now=NOW))
    assert ls.count("BEGIN:VTIMEZONE") == 1


def test_retime_to_all_day_adds_no_vtimezone():
    bare = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:x\r\nDTSTART:20261001T150000Z\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    ls = lines(ical.retime(bare, start=date(2026, 10, 2), end=None, tz=TZ, now=NOW))
    assert "BEGIN:VTIMEZONE" not in ls


def test_when_reads_the_tzid_plus_z_that_nextclouds_web_app_writes():
    prop = ical.parse("DUE;TZID=America/Toronto:20260930T033500Z")
    assert ical.when(prop, TZ) == datetime(2026, 9, 30, 3, 35, tzinfo=UTC)


def test_vtimezone_reaches_a_decade_out_for_long_running_series():
    zone = lines("".join(x + "\r\n" for x in ics.vtimezone(TZ, datetime(2026, 9, 1))))
    assert "RDATE:20350311T020000" in zone  # DST starts Sun 11 Mar 2035
    assert "RDATE:20351104T020000" in zone  # and ends Sun 4 Nov 2035
    assert "DTSTART:20250309T020000" in zone  # the year before
    assert zone.count("BEGIN:DAYLIGHT") == 1 and zone.count("BEGIN:STANDARD") == 1


def zone_of(tz: str, around: datetime) -> list[str]:
    return lines("".join(x + "\r\n" for x in ics.vtimezone(tz, around)))


def observances(zone: list[str]) -> list[dict[str, str]]:
    """Each STANDARD/DAYLIGHT block as {kind, DTSTART, TZOFFSETFROM, TZOFFSETTO, TZNAME, RDATEs}."""
    out: list[dict[str, str]] = []
    for line in zone:
        if line in ("BEGIN:STANDARD", "BEGIN:DAYLIGHT"):
            out.append({"kind": line[6:], "RDATE": ""})
        elif out and ":" in line and not line.startswith(("END:", "TZID")):
            key, _, value = line.partition(":")
            out[-1][key] = out[-1][key] + " " + value if key == "RDATE" else value
    return out


def test_a_zone_whose_changes_all_go_one_way_still_builds():
    # Asia/Almaty moved from +0600 to +0500 on 1 Mar 2024 and had no other change in 2023-2034.
    obs = observances(zone_of("Asia/Almaty", datetime(2024, 6, 1)))
    assert [(o["kind"], o["TZOFFSETFROM"], o["TZOFFSETTO"]) for o in obs] == [("STANDARD", "+0600", "+0500")]


def test_transitions_with_different_offsets_get_their_own_observance():
    # America/Nuuk: -0300 to -0200 in March 2023, then summer time -0200 to -0100 from 2024.
    obs = observances(zone_of("America/Nuuk", datetime(2024, 6, 1)))
    pairs = {(o["kind"], o["TZOFFSETFROM"], o["TZOFFSETTO"]) for o in obs}
    assert ("DAYLIGHT", "-0200", "-0100") in pairs and ("STANDARD", "-0100", "-0200") in pairs
    assert all(o["TZOFFSETFROM"] != o["TZOFFSETTO"] for o in obs)


def test_a_southern_hemisphere_zone_starts_daylight_in_october():
    obs = {o["kind"]: o for o in observances(zone_of("Australia/Sydney", datetime(2026, 9, 1)))}
    assert obs["DAYLIGHT"]["DTSTART"] == "20251005T020000" or "20261004T020000" in obs["DAYLIGHT"]["RDATE"]
    assert (obs["DAYLIGHT"]["TZOFFSETFROM"], obs["DAYLIGHT"]["TZOFFSETTO"]) == ("+1000", "+1100")
    assert "20260405T030000" in obs["STANDARD"]["RDATE"] or obs["STANDARD"]["DTSTART"] == "20260405T030000"
    assert (obs["STANDARD"]["TZOFFSETFROM"], obs["STANDARD"]["TZOFFSETTO"]) == ("+1100", "+1000")
