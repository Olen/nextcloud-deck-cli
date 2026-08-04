"""Tests for label-driven Deck card recurrence."""

from dataclasses import dataclass
from datetime import date

import pytest

from deck_recur import (
    DEFAULT_LABEL_PREFIX,
    Schedule,
    ScheduleError,
    fires_on,
    parse_labels,
    parse_schedule,
)


@dataclass
class FakeLabel:
    id: int
    title: str


# --- parse_schedule -------------------------------------------------------

def test_parse_daily():
    assert parse_schedule("daily") == Schedule(kind="daily")


def test_parse_single_weekday():
    assert parse_schedule("mon") == Schedule(kind="weekdays", weekdays=frozenset({0}))


def test_parse_weekday_list():
    assert parse_schedule("mon,thu") == Schedule(
        kind="weekdays", weekdays=frozenset({0, 3})
    )


def test_parse_weekday_list_tolerates_spaces():
    assert parse_schedule(" mon , thu ") == Schedule(
        kind="weekdays", weekdays=frozenset({0, 3})
    )


def test_parse_is_case_insensitive():
    assert parse_schedule("MON") == Schedule(kind="weekdays", weekdays=frozenset({0}))


def test_parse_day_of_month():
    assert parse_schedule("25") == Schedule(kind="dayofmonth", day=25)


def test_parse_all_seven_weekdays():
    for name, number in [
        ("mon", 0), ("tue", 1), ("wed", 2), ("thu", 3),
        ("fri", 4), ("sat", 5), ("sun", 6),
    ]:
        assert parse_schedule(name).weekdays == frozenset({number})


@pytest.mark.parametrize("expr", ["modnay", "", "   ", "mon,", "mon,xyz", "0", "32", "-1"])
def test_parse_rejects_bad_expressions(expr):
    with pytest.raises(ScheduleError):
        parse_schedule(expr)


def test_parse_names_the_interval_form_specifically():
    with pytest.raises(ScheduleError, match="not implemented yet"):
        parse_schedule("14d")


def test_interval_error_quotes_the_expression():
    with pytest.raises(ScheduleError, match="14d"):
        parse_schedule("14d")


# --- fires_on -------------------------------------------------------------

def test_daily_fires_every_day():
    assert fires_on(Schedule(kind="daily"), date(2026, 8, 4))
    assert fires_on(Schedule(kind="daily"), date(2026, 8, 5))


def test_weekday_fires_on_its_day():
    monday = Schedule(kind="weekdays", weekdays=frozenset({0}))
    assert fires_on(monday, date(2026, 8, 3))       # a Monday
    assert not fires_on(monday, date(2026, 8, 4))   # a Tuesday


def test_weekday_list_fires_on_either_day():
    sched = Schedule(kind="weekdays", weekdays=frozenset({0, 3}))
    assert fires_on(sched, date(2026, 8, 3))        # Monday
    assert fires_on(sched, date(2026, 8, 6))        # Thursday
    assert not fires_on(sched, date(2026, 8, 5))    # Wednesday


def test_day_of_month_fires_on_its_day():
    sched = Schedule(kind="dayofmonth", day=25)
    assert fires_on(sched, date(2026, 8, 25))
    assert not fires_on(sched, date(2026, 8, 24))


def test_day_31_clamps_to_end_of_february():
    sched = Schedule(kind="dayofmonth", day=31)
    assert fires_on(sched, date(2026, 2, 28))
    assert not fires_on(sched, date(2026, 2, 27))


def test_day_31_clamps_to_29_february_in_a_leap_year():
    sched = Schedule(kind="dayofmonth", day=31)
    assert fires_on(sched, date(2028, 2, 29))
    assert not fires_on(sched, date(2028, 2, 28))


def test_day_31_clamps_to_30_april():
    sched = Schedule(kind="dayofmonth", day=31)
    assert fires_on(sched, date(2026, 4, 30))


def test_day_31_does_not_clamp_in_a_31_day_month():
    sched = Schedule(kind="dayofmonth", day=31)
    assert fires_on(sched, date(2026, 8, 31))
    assert not fires_on(sched, date(2026, 8, 30))


def test_day_29_does_not_fire_on_28_february_in_a_leap_year():
    # February 2028 has 29 days, so there is no clamping to do.
    sched = Schedule(kind="dayofmonth", day=29)
    assert not fires_on(sched, date(2028, 2, 28))


def test_day_30_clamps_to_end_of_february():
    sched = Schedule(kind="dayofmonth", day=30)
    assert fires_on(sched, date(2026, 2, 28))
    assert not fires_on(sched, date(2026, 2, 27))


def test_day_30_clamps_to_29_february_in_a_leap_year():
    sched = Schedule(kind="dayofmonth", day=30)
    assert fires_on(sched, date(2028, 2, 29))
    assert not fires_on(sched, date(2028, 2, 28))


def test_day_30_does_not_clamp_in_april():
    # April has exactly 30 days, so no clamping occurs.
    sched = Schedule(kind="dayofmonth", day=30)
    assert fires_on(sched, date(2026, 4, 30))
    assert not fires_on(sched, date(2026, 4, 29))


def test_day_30_does_not_clamp_in_a_31_day_month():
    # August has 31 days, and the requested day is 30, so no clamping.
    sched = Schedule(kind="dayofmonth", day=30)
    assert fires_on(sched, date(2026, 8, 30))
    assert not fires_on(sched, date(2026, 8, 31))


# --- parse_labels ---------------------------------------------------------

def test_parse_labels_picks_up_prefixed_labels():
    schedules, problems = parse_labels([FakeLabel(146, "Repeat: 25")])
    assert schedules == {146: Schedule(kind="dayofmonth", day=25)}
    assert problems == {}


def test_parse_labels_ignores_other_labels():
    schedules, problems = parse_labels(
        [FakeLabel(145, "Email"), FakeLabel(16, "Later")]
    )
    assert schedules == {}
    assert problems == {}


def test_parse_labels_matches_prefix_case_insensitively():
    schedules, _ = parse_labels([FakeLabel(1, "repeat:MON")])
    assert schedules == {1: Schedule(kind="weekdays", weekdays=frozenset({0}))}


def test_parse_labels_collects_problems_instead_of_raising():
    schedules, problems = parse_labels([FakeLabel(9, "Repeat: modnay")])
    assert schedules == {}
    assert 9 in problems
    assert "Repeat: modnay" in problems[9]


def test_parse_labels_reports_the_interval_form():
    _, problems = parse_labels([FakeLabel(9, "Repeat: 14d")])
    assert "not implemented yet" in problems[9]


def test_parse_labels_honours_a_custom_prefix():
    schedules, _ = parse_labels([FakeLabel(1, "@ mon")], prefix="@")
    assert schedules == {1: Schedule(kind="weekdays", weekdays=frozenset({0}))}


def test_default_prefix_is_repeat_colon():
    assert DEFAULT_LABEL_PREFIX == "Repeat:"
