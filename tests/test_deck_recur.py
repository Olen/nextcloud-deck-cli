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


from deck_recur import (
    Config,
    RecurAction,
    execute_plan,
    plan_recurrence,
    run,
)


@dataclass
class FakeCard:
    id: int
    title: str
    labels: list
    archived: bool = False
    deleted_at: int = 0
    stack_id: int = 9


@dataclass
class FakeStack:
    id: int
    title: str
    cards: list


class FakeDeck:
    """Records move_card calls instead of performing them."""

    def __init__(self, board_labels=None, stacks=None, fail_on=()):
        self._board_labels = board_labels or []
        self._stacks = stacks or []
        self._fail_on = set(fail_on)
        self.moves = []

    def get_board(self, board_id=None):
        return type("B", (), {"labels": self._board_labels})()

    def get_stacks(self, include_archived=True, board_id=None):
        return self._stacks

    def move_card(self, card, target_stack_id, order=0, clear_done=False,
                  board_id=None):
        if card.id in self._fail_on:
            raise RuntimeError("boom")
        self.moves.append((card.id, target_stack_id, order, clear_done))


DUE = Schedule(kind="dayofmonth", day=25)
THE_25TH = date(2026, 8, 25)
THE_24TH = date(2026, 8, 24)


# --- plan_recurrence ------------------------------------------------------

def test_plans_a_move_for_a_card_that_is_due():
    card = FakeCard(323, "Timeliste", [FakeLabel(146, "Repeat: 25")])
    actions = plan_recurrence([card], {146: DUE}, THE_25TH, target_stack_id=22)
    assert len(actions) == 1
    assert actions[0].card is card
    assert actions[0].target_stack_id == 22
    assert actions[0].label_titles == ("Repeat: 25",)


def test_plans_nothing_when_not_due():
    card = FakeCard(323, "Timeliste", [FakeLabel(146, "Repeat: 25")])
    assert plan_recurrence([card], {146: DUE}, THE_24TH, 22) == []


def test_skips_cards_without_a_recurrence_label():
    card = FakeCard(110, "Vanne planter", [FakeLabel(145, "Email")])
    assert plan_recurrence([card], {146: DUE}, THE_25TH, 22) == []


def test_skips_cards_with_no_labels_at_all():
    card = FakeCard(145, "Migrere lupus", [])
    assert plan_recurrence([card], {146: DUE}, THE_25TH, 22) == []


def test_skips_archived_cards():
    card = FakeCard(323, "Timeliste", [FakeLabel(146, "Repeat: 25")], archived=True)
    assert plan_recurrence([card], {146: DUE}, THE_25TH, 22) == []


def test_skips_trashed_cards():
    card = FakeCard(323, "Timeliste", [FakeLabel(146, "Repeat: 25")],
                    deleted_at=1754300000)
    assert plan_recurrence([card], {146: DUE}, THE_25TH, 22) == []


def test_multiple_labels_union_so_either_can_fire():
    card = FakeCard(
        1, "Chore", [FakeLabel(146, "Repeat: 25"), FakeLabel(147, "Repeat: mon")]
    )
    schedules = {146: DUE, 147: Schedule(kind="weekdays", weekdays=frozenset({0}))}
    # 2026-08-03 is a Monday and not the 25th: the weekday label alone fires.
    actions = plan_recurrence([card], schedules, date(2026, 8, 3), 22)
    assert len(actions) == 1
    assert actions[0].label_titles == ("Repeat: mon",)


def test_a_card_matching_two_labels_yields_one_action():
    card = FakeCard(
        1, "Chore", [FakeLabel(146, "Repeat: 25"), FakeLabel(148, "Repeat: daily")]
    )
    schedules = {146: DUE, 148: Schedule(kind="daily")}
    actions = plan_recurrence([card], schedules, THE_25TH, 22)
    assert len(actions) == 1
    assert set(actions[0].label_titles) == {"Repeat: 25", "Repeat: daily"}


# --- execute_plan ---------------------------------------------------------

def test_execute_moves_with_order_zero_and_clears_done():
    card = FakeCard(323, "Timeliste", [FakeLabel(146, "Repeat: 25")])
    deck = FakeDeck()
    summary = execute_plan([RecurAction(card, 22, ("Repeat: 25",))], deck)
    assert deck.moves == [(323, 22, 0, True)]
    assert summary.moved == 1
    assert summary.failures == 0


def test_execute_dry_run_performs_no_moves():
    card = FakeCard(323, "Timeliste", [FakeLabel(146, "Repeat: 25")])
    deck = FakeDeck()
    summary = execute_plan([RecurAction(card, 22, ("Repeat: 25",))], deck, dry_run=True)
    assert deck.moves == []
    assert summary.moved == 1


def test_execute_counts_a_failure_and_keeps_going():
    a = FakeCard(1, "A", [])
    b = FakeCard(2, "B", [])
    deck = FakeDeck(fail_on={1})
    summary = execute_plan(
        [RecurAction(a, 22, ()), RecurAction(b, 22, ())], deck
    )
    assert summary.failures == 1
    assert summary.moved == 1
    assert deck.moves == [(2, 22, 0, True)]


# --- run ------------------------------------------------------------------

def base_config(**kw):
    defaults = dict(
        nc_url="https://example.invalid",
        nc_username="u",
        nc_password="p",
        nc_board_id=4,
    )
    defaults.update(kw)
    return Config(**defaults)


def test_run_moves_a_due_card_end_to_end():
    card = FakeCard(323, "Timeliste", [FakeLabel(146, "Repeat: 25")])
    deck = FakeDeck(
        board_labels=[FakeLabel(146, "Repeat: 25")],
        stacks=[FakeStack(22, "Todo", []), FakeStack(9, "Done", [card])],
    )
    rc = run(base_config(on_date=THE_25TH), deck=deck)
    assert rc == 0
    assert deck.moves == [(323, 22, 0, True)]


def test_run_moves_nothing_on_a_non_trigger_day():
    card = FakeCard(323, "Timeliste", [FakeLabel(146, "Repeat: 25")])
    deck = FakeDeck(
        board_labels=[FakeLabel(146, "Repeat: 25")],
        stacks=[FakeStack(22, "Todo", []), FakeStack(9, "Done", [card])],
    )
    assert run(base_config(on_date=THE_24TH), deck=deck) == 0
    assert deck.moves == []


def test_run_dry_run_performs_no_moves():
    card = FakeCard(323, "Timeliste", [FakeLabel(146, "Repeat: 25")])
    deck = FakeDeck(
        board_labels=[FakeLabel(146, "Repeat: 25")],
        stacks=[FakeStack(22, "Todo", []), FakeStack(9, "Done", [card])],
    )
    assert run(base_config(on_date=THE_25TH, dry_run=True), deck=deck) == 0
    assert deck.moves == []


def test_run_exits_1_when_a_stack_is_missing():
    deck = FakeDeck(
        board_labels=[FakeLabel(146, "Repeat: 25")],
        stacks=[FakeStack(9, "Done", [])],          # no Todo
    )
    assert run(base_config(on_date=THE_25TH), deck=deck) == 1


def test_run_exits_0_when_the_board_has_no_recurrence_labels():
    deck = FakeDeck(
        board_labels=[FakeLabel(145, "Email")],
        stacks=[FakeStack(22, "Todo", []), FakeStack(9, "Done", [])],
    )
    assert run(base_config(on_date=THE_25TH), deck=deck) == 0


def test_run_exits_2_when_a_move_fails():
    card = FakeCard(323, "Timeliste", [FakeLabel(146, "Repeat: 25")])
    deck = FakeDeck(
        board_labels=[FakeLabel(146, "Repeat: 25")],
        stacks=[FakeStack(22, "Todo", []), FakeStack(9, "Done", [card])],
        fail_on={323},
    )
    assert run(base_config(on_date=THE_25TH), deck=deck) == 2


def test_run_warns_about_an_unparseable_label_and_names_the_cards(caplog):
    card = FakeCard(1, "Chore", [FakeLabel(9, "Repeat: modnay")])
    deck = FakeDeck(
        board_labels=[FakeLabel(9, "Repeat: modnay")],
        stacks=[FakeStack(22, "Todo", []), FakeStack(9, "Done", [card])],
    )
    with caplog.at_level("WARNING"):
        run(base_config(on_date=THE_25TH), deck=deck)
    text = caplog.text
    assert "Repeat: modnay" in text
    assert "Chore" in text
