#!/usr/bin/env python3
"""
Label-driven recurrence for Nextcloud Deck cards.

A card carrying a label whose title starts with `Repeat:` returns from the
Done stack to the Todo stack on the days its schedule names. Labels are
created by hand in the Deck UI; this module only reads them.

The trigger model is deliberately edge-triggered by the move itself: once a
card has been moved out of the source stack it no longer matches, so there
is no per-card state to keep and no state file. That only works because the
job runs once a day — at a shorter interval, re-completing a card on its own
trigger day would send it straight back.
"""
from __future__ import annotations

import calendar
import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_LABEL_PREFIX = "Repeat:"

WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

# Recognised so it can be rejected with a message that names it, rather than
# falling through to the generic "unknown weekday" error.
INTERVAL_RE = re.compile(r"^(\d+)d$")


class ScheduleError(ValueError):
    """A `Repeat:` label carries an expression we cannot use."""


@dataclass(frozen=True)
class Schedule:
    """A parsed recurrence expression.

    `kind` is one of "daily", "weekdays", "dayofmonth". Weekday numbers are
    Python's: 0 is Monday.
    """
    kind: str
    weekdays: frozenset[int] = frozenset()
    day: int | None = None


def parse_schedule(expr: str) -> Schedule:
    """Parse the part of a label title that follows the prefix.

    Raises ScheduleError on anything unusable, including the reserved
    interval form, whose message names it specifically.
    """
    text = (expr or "").strip().lower()
    if not text:
        raise ScheduleError("empty schedule expression")

    if text == "daily":
        return Schedule(kind="daily")

    if INTERVAL_RE.match(text):
        raise ScheduleError(
            f"interval schedules like {text!r} are not implemented yet"
        )

    if text.isdigit():
        day = int(text)
        if not 1 <= day <= 31:
            raise ScheduleError(f"day-of-month {day} is outside 1..31")
        return Schedule(kind="dayofmonth", day=day)

    parts = [p.strip() for p in text.split(",")]
    if not all(parts):
        raise ScheduleError(f"malformed weekday list {expr!r}")
    unknown = sorted({p for p in parts if p not in WEEKDAYS})
    if unknown:
        raise ScheduleError(
            f"unknown weekday(s) {', '.join(unknown)} in {expr!r}"
        )
    return Schedule(
        kind="weekdays",
        weekdays=frozenset(WEEKDAYS[p] for p in parts),
    )


def fires_on(schedule: Schedule, on: date) -> bool:
    """Does `schedule` name `on` as a trigger day?"""
    if schedule.kind == "daily":
        return True

    if schedule.kind == "weekdays":
        return on.weekday() in schedule.weekdays

    if schedule.kind == "dayofmonth":
        if on.day == schedule.day:
            return True
        # A day-of-month past the end of a short month fires on its last day,
        # so "Repeat: 31" still happens in February.
        last = calendar.monthrange(on.year, on.month)[1]
        return schedule.day > last and on.day == last

    raise ScheduleError(f"unknown schedule kind {schedule.kind!r}")


def parse_labels(
    labels, prefix: str = DEFAULT_LABEL_PREFIX
) -> tuple[dict[int, Schedule], dict[int, str]]:
    """Read every recurrence label on a board.

    Returns `({label_id: Schedule}, {label_id: problem_message})`. Problems
    are collected rather than logged so this stays pure and testable; the
    caller logs them, and can name the cards affected.
    """
    pref = (prefix or "").strip().lower()
    schedules: dict[int, Schedule] = {}
    problems: dict[int, str] = {}

    for label in labels or []:
        title = (getattr(label, "title", "") or "").strip()
        if not title.lower().startswith(pref):
            continue
        try:
            schedules[label.id] = parse_schedule(title[len(pref):])
        except ScheduleError as e:
            problems[label.id] = f"Ignoring label {title!r}: {e}"

    return schedules, problems


@dataclass(frozen=True)
class RecurAction:
    """Return a card to the target stack because a schedule fired today."""
    card: Any               # ncdeck.Card
    target_stack_id: int
    label_titles: tuple[str, ...]


@dataclass
class ExecutionSummary:
    """Counts of actions actually applied during execute_plan."""
    moved: int = 0
    failures: int = 0


def plan_recurrence(
    cards, schedules: dict[int, Schedule], on: date, target_stack_id: int
) -> list[RecurAction]:
    """Pure planner. One action per card that is due today.

    A card is due when it carries at least one recurrence label whose
    schedule fires on `on`. Archived and trashed cards are skipped: a chore
    deliberately archived must not resurrect, and Deck's update endpoint
    would restore a trashed card as a side effect of touching it.
    """
    actions: list[RecurAction] = []

    for card in cards or []:
        if getattr(card, "archived", False):
            continue
        if getattr(card, "deleted_at", 0):
            continue

        matched = [
            (getattr(label, "title", "") or "")
            for label in (getattr(card, "labels", None) or [])
            if (sched := schedules.get(getattr(label, "id", None))) is not None
            and fires_on(sched, on)
        ]
        if matched:
            actions.append(
                RecurAction(
                    card=card,
                    target_stack_id=target_stack_id,
                    label_titles=tuple(matched),
                )
            )

    return actions


def execute_plan(actions, deck, dry_run: bool = False) -> ExecutionSummary:
    """Apply a plan from plan_recurrence() against a Deck client.

    Per-action failures are logged and counted; the rest of the plan still
    runs, because the next daily run would retry a card that is still due.

    `deck` must duck-type as ncdeck.DeckClient (we call `.move_card(...)`).
    """
    summary = ExecutionSummary()

    for action in actions:
        matched = ", ".join(action.label_titles)
        try:
            if dry_run:
                log.info(
                    "[dry-run] would move card #%s %r to stack #%s and clear "
                    "its done-tick (matched %s)",
                    action.card.id, action.card.title, action.target_stack_id,
                    matched,
                )
            else:
                log.info(
                    "Moving card #%s %r to stack #%s and clearing its "
                    "done-tick (matched %s)",
                    action.card.id, action.card.title, action.target_stack_id,
                    matched,
                )
                deck.move_card(
                    action.card,
                    action.target_stack_id,
                    order=0,
                    clear_done=True,
                )
            summary.moved += 1
        except Exception as e:
            log.warning("Failed to recur card #%s: %s", action.card.id, e)
            summary.failures += 1

    return summary


@dataclass
class Config:
    nc_url: str
    nc_username: str
    nc_password: str
    nc_board_id: int
    source_stack_name: str = "Done"
    target_stack_name: str = "Todo"
    label_prefix: str = DEFAULT_LABEL_PREFIX
    on_date: date | None = None
    dry_run: bool = False
    verbose: bool = False


def _find_stack(stacks, name: str):
    target = (name or "").strip().lower()
    for s in stacks:
        if (getattr(s, "title", "") or "").strip().lower() == target:
            return s
    return None


def _cards_carrying(cards, label_id: int) -> list[str]:
    return [
        getattr(c, "title", "") or ""
        for c in cards
        if any(
            getattr(lb, "id", None) == label_id
            for lb in (getattr(c, "labels", None) or [])
        )
    ]


def run(config: Config, deck=None) -> int:
    """
    Run one recurrence pass. Returns a process exit code:
      0 — clean run (including "nothing to do")
      1 — setup/connection error, or a configured stack does not exist
      2 — run completed but at least one card failed to move

    `deck` is an optional pre-built client, for tests. In production it is
    None and a DeckClient is built from `config`.
    """
    if deck is None:
        from ncdeck import DeckClient
        deck = DeckClient(
            config.nc_url,
            config.nc_username,
            config.nc_password,
            config.nc_board_id,
        )

    try:
        board = deck.get_board()
        stacks = deck.get_stacks(include_archived=False)
    except Exception as e:
        log.error("Failed to fetch Deck board or stacks: %s", e)
        return 1

    source = _find_stack(stacks, config.source_stack_name)
    target = _find_stack(stacks, config.target_stack_name)
    missing = [
        n for n, s in (
            (config.source_stack_name, source),
            (config.target_stack_name, target),
        ) if s is None
    ]
    if missing:
        log.error(
            "Could not find required stack(s): %s on board %s",
            ", ".join(missing), config.nc_board_id,
        )
        return 1

    source_cards = getattr(source, "cards", None) or []
    schedules, problems = parse_labels(
        getattr(board, "labels", None) or [], config.label_prefix
    )

    for label_id, problem in problems.items():
        carriers = _cards_carrying(source_cards, label_id)
        if carriers:
            log.warning(
                "%s - affects %d card(s) in %s: %s",
                problem, len(carriers), config.source_stack_name,
                "; ".join(carriers),
            )
        else:
            log.warning("%s", problem)

    if not schedules:
        log.info(
            "No usable %s label(s) on board %s - nothing to do",
            config.label_prefix, config.nc_board_id,
        )
        return 0

    on = config.on_date or date.today()
    actions = plan_recurrence(source_cards, schedules, on, target.id)
    log.info(
        "Recurrence pass for %s: %d of %d card(s) in %s due "
        "(%d schedule(s) parsed, %d label(s) skipped)",
        on.isoformat(), len(actions), len(source_cards),
        config.source_stack_name, len(schedules), len(problems),
    )

    summary = execute_plan(actions, deck, dry_run=config.dry_run)
    log.info(
        "Done: moved=%d failures=%d%s",
        summary.moved, summary.failures,
        " (dry-run)" if config.dry_run else "",
    )
    return 0 if summary.failures == 0 else 2
