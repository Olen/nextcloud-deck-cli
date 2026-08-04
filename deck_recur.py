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
