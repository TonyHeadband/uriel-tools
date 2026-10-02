from datetime import UTC, date, datetime, time, timedelta

import pytest

from uriel_tools.schedules import rules
from uriel_tools.schedules.rules import RuleError

TORONTO = "America/Toronto"
# Friday 6 March 2026, 10:00 in Toronto (EST, UTC-5). DST starts Sunday 8 March and ends Sunday 1 November.
NOW = datetime(2026, 3, 6, 15, 0, tzinfo=UTC)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.mark.parametrize(
    ("frequency", "extra", "rrule", "dtstart"),
    [
        ("once", {"on": "2026-03-10"}, "FREQ=DAILY;COUNT=1", datetime(2026, 3, 10, 9, 0)),
        ("daily", {}, "FREQ=DAILY", datetime(2026, 3, 7, 9, 0)),
        ("weekdays", {}, "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", datetime(2026, 3, 9, 9, 0)),
        ("weekly", {"weekdays": ["thu", "mon"]}, "FREQ=WEEKLY;BYDAY=MO,TH", datetime(2026, 3, 9, 9, 0)),
        ("monthly", {"day_of_month": 15}, "FREQ=MONTHLY;BYMONTHDAY=15", datetime(2026, 3, 15, 9, 0)),
    ],
)
def test_every_frequency_builds_its_rule_from_the_first_run_after_now(frequency, extra, rrule, dtstart):
    rule = rules.build(frequency, "09:00", TORONTO, NOW, **extra)
    assert (rule.rrule, rule.dtstart, rule.tz) == (rrule, dtstart, TORONTO)


def test_nine_oclock_stays_nine_oclock_across_dst():
    rule = rules.build("daily", "9:00", TORONTO, NOW)
    runs = rules.upcoming(rule.rrule, rule.dtstart, rule.tz, NOW, 3)
    assert runs == [utc(2026, 3, 7, 14, 0), utc(2026, 3, 8, 13, 0), utc(2026, 3, 9, 13, 0)]
    autumn = rules.upcoming(rule.rrule, rule.dtstart, rule.tz, utc(2026, 10, 31, 12, 0), 2)
    assert autumn == [utc(2026, 10, 31, 13, 0), utc(2026, 11, 1, 14, 0)]


def test_a_time_in_the_spring_forward_gap_runs_at_the_first_valid_minute():
    rule = rules.build("daily", "02:30", TORONTO, NOW)
    runs = rules.upcoming(rule.rrule, rule.dtstart, rule.tz, NOW, 3)
    # 7 March 2:30 EST; 8 March 2:30 doesn't exist, so 3:00 EDT; 9 March 2:30 EDT.
    assert runs == [utc(2026, 3, 7, 7, 30), utc(2026, 3, 8, 7, 0), utc(2026, 3, 9, 6, 30)]


def test_a_time_that_happens_twice_runs_once_at_the_first():
    rule = rules.build("daily", "01:30", TORONTO, utc(2026, 10, 30, 12, 0))
    runs = rules.upcoming(rule.rrule, rule.dtstart, rule.tz, utc(2026, 10, 30, 12, 0), 3)
    # 1 November 1:30 happens at 5:30 UTC (EDT) and again at 6:30 UTC (EST): only the first counts.
    assert runs == [utc(2026, 10, 31, 5, 30), utc(2026, 11, 1, 5, 30), utc(2026, 11, 2, 6, 30)]


def test_occurrences_are_utc_instants():
    rule = rules.build("weekdays", "18:15", "Europe/Rome", NOW)
    [first] = rules.upcoming(rule.rrule, rule.dtstart, rule.tz, NOW, 1)
    assert first.tzinfo is UTC
    assert first == utc(2026, 3, 6, 17, 15)  # Friday 18:15 in Rome (CET, UTC+1) is still ahead of NOW


def test_a_one_off_runs_once_and_never_in_the_past():
    rule = rules.build("once", "09:00", TORONTO, NOW, on="2026-03-10")
    assert rules.upcoming(rule.rrule, rule.dtstart, rule.tz, NOW, 5) == [utc(2026, 3, 10, 13, 0)]
    # The model may not know today's date, so the refusal tells it.
    with pytest.raises(RuleError, match=r"already passed \(it is now Fri 6 Mar 2026, 10:00 Toronto time\)"):
        rules.build("once", "09:00", TORONTO, NOW, on="2026-03-06")  # an hour ago
    with pytest.raises(RuleError, match="needs a date"):
        rules.build("once", "09:00", TORONTO, NOW)


def test_the_first_run_is_tomorrow_when_todays_time_has_passed():
    rule = rules.build("daily", "08:00", TORONTO, NOW)
    assert rule.dtstart == datetime(2026, 3, 7, 8, 0)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("daily", "9am", TORONTO), "HH:MM"),
        (("daily", "24:00", TORONTO), "HH:MM"),
        (("daily", "09:00", "Mars/Olympus"), "not a timezone"),
        (("weekly", "09:00", TORONTO), "needs weekdays"),
        (("monthly", "09:00", TORONTO), "day_of_month"),
    ],
)
def test_bad_fields_say_what_to_fix(args, message):
    with pytest.raises(RuleError, match=message):
        rules.build(*args, NOW)


def test_bad_optional_fields_say_what_to_fix():
    with pytest.raises(RuleError, match="day_of_month"):
        rules.build("monthly", "09:00", TORONTO, NOW, day_of_month=32)
    with pytest.raises(RuleError, match="unknown weekdays"):
        rules.build("weekly", "09:00", TORONTO, NOW, weekdays=["funday"])
    with pytest.raises(RuleError, match="YYYY-MM-DD"):
        rules.build("once", "09:00", TORONTO, NOW, on="next tuesday")


def test_timezone_names_match_without_case():
    assert rules.zone_name("america/toronto") == TORONTO
    assert rules.build("daily", "09:00", " europe/ROME ", NOW).tz == "Europe/Rome"


def test_describe_in_plain_language():
    def said(frequency, **extra):
        rule = rules.build(frequency, "09:05", TORONTO, NOW, **extra)
        return rules.describe(rule.rrule, rule.dtstart, rule.tz)

    assert said("once", on="2026-03-10") == "once, on Tuesday 10 March 2026 at 9:05 (Toronto time)"
    assert said("daily") == "every day at 9:05 (Toronto time)"
    assert said("weekdays") == "every weekday at 9:05 (Toronto time)"
    assert said("weekly", weekdays=["mon"]) == "every Monday at 9:05 (Toronto time)"
    assert said("weekly", weekdays=["mon", "wed", "fri"]) == (
        "every Monday, Wednesday and Friday at 9:05 (Toronto time)"
    )
    assert said("monthly", day_of_month=31) == (
        "on day 31 of every month at 9:05, skipping months without that day (Toronto time)"
    )
    assert rules.describe("FREQ=DAILY", datetime(2026, 3, 7, 9, 0), "America/Argentina/Buenos_Aires") == (
        "every day at 9:00 (Buenos Aires time)"
    )


def test_grace_is_ten_minutes_for_a_one_off_and_at_most_two_hours():
    once = rules.build("once", "09:00", TORONTO, NOW, on="2026-03-10")
    assert rules.grace(once.rrule, once.dtstart, once.tz, utc(2026, 3, 10, 13, 0)) == timedelta(minutes=10)
    daily = rules.build("daily", "09:00", TORONTO, NOW)
    assert rules.grace(daily.rrule, daily.dtstart, daily.tz, utc(2026, 3, 7, 14, 0)) == timedelta(hours=2)


def test_times_for_the_wire_and_for_people():
    at = utc(2026, 9, 30, 13, 0)
    assert rules.iso(at) == "2026-09-30T13:00:00Z"
    assert rules.local(at, TORONTO) == "Wed 30 Sep 2026, 9:00"


def test_clock_and_date_fields_are_parsed_or_refused():
    assert rules.parse_clock(" 9:05 ") == time(9, 5)
    assert rules.parse_date("2026-10-03") == date(2026, 10, 3)
    with pytest.raises(RuleError, match="HH:MM"):
        rules.parse_clock("9am")
    with pytest.raises(RuleError, match="YYYY-MM-DD"):
        rules.parse_date("3 Oct")
