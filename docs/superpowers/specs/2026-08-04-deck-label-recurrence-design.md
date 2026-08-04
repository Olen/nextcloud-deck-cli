# Label-Driven Card Recurrence — Design

**Date:** 2026-08-04
**Status:** Approved

## Goal

Let a Deck label express a repeating schedule, so that a card sitting in
`Done` returns to `Todo` automatically on its next scheduled day.

The deliverable is the grammar and the engine. Labels are created by hand in
the Deck UI as they are needed; the script never creates one.

## Motivation

`Timeliste` and `Visma Expenses` are due on the 25th of every month. Today
they are completed, dragged to `Done`, and then manually dragged back a month
later. The same shape covers weekly chores and daily habits.

## Non-Goals

- Creating labels. The board is the configuration; the script only reads it.
- Interval schedules measured from completion (`every 14 days`). See
  "Deferred" below.
- Any change to how cards reach `Done`. Completion stays a manual UI action.
- Recurrence for boards other than the one passed as `--board-id`.

## The Label Grammar

A recurrence label is any Deck label whose title starts with a configurable
prefix, default `Repeat:`. The remainder is the schedule expression.

```
label-title := prefix WS* expr
expr        := "daily"
             | weekday ("," WS* weekday)*
             | day-of-month
weekday     := "mon" | "tue" | "wed" | "thu" | "fri" | "sat" | "sun"
day-of-month:= integer 1..31

; recognised but rejected, see "Deferred"
interval    := integer "d"
```

| Label title | Fires on |
|---|---|
| `Repeat: daily` | every day |
| `Repeat: mon` | every Monday |
| `Repeat: mon,thu` | every Monday and Thursday |
| `Repeat: 1` | the 1st of each month |
| `Repeat: 25` | the 25th of each month |

Matching rules:

- The prefix is matched **case-insensitively**, and whitespace around the
  expression is stripped. `repeat:MON` and `Repeat:  mon ` are both valid.
- **Multiple recurrence labels on one card union together.** A card with both
  `Repeat: mon` and `Repeat: 25` fires on Mondays and on the 25th.
- **Day-of-month clamps to the end of short months.** `Repeat: 31` fires on
  28 February (29 February in a leap year) and on 30 April. Formally: a
  day-of-month `n` fires on date `D` when `D.day == n`, or when
  `n > days_in_month(D)` and `D.day == days_in_month(D)`.
- **An unparseable expression is loud.** `Repeat: modnay` emits a WARNING
  naming the label title and every card carrying it, and those cards are
  skipped. Silent failure is the dominant risk in this feature: a typo would
  otherwise disable a chore permanently with no signal.
- **`Nd` gets a specific message.** `Repeat: 14d` parses as a recognised but
  unimplemented form and warns `interval schedules like '14d' are not
  implemented yet`, distinct from the generic parse error.

## Trigger Model

The script runs **once a day** and evaluates a single date: today.

> For each card in the source stack carrying at least one parseable
> recurrence label, if any of its schedules fires today, move it to the
> target stack and clear its done-tick.

Idempotency is structural. The move is the state change: once the card is in
`Todo` it no longer matches the source-stack filter. Re-completing
`Timeliste` on the 25th at 14:00 leaves it in `Done`, because the next run is
on the 26th, which is not a trigger day.

There is **no state file**. The service runs on lupus, which is always on, so
a catch-up window would solve a problem that does not exist here. The
accepted consequence: if the host is down for the whole of a trigger day,
those cards wait for the next occurrence. `OnFailure=notify-failure@%n.service`
reports a failed run, and `--date YYYY-MM-DD` replays a missed day in one
command.

### Why daily, and not inside imap-deck-sync

The 5-minute sync is the wrong host for this. At that cadence "today is the
25th" is true 288 times, so avoiding a re-move after re-completion would
require per-card bookkeeping. A daily run makes the schedule granularity
(days) equal the run granularity (days), and the rule becomes naturally
edge-triggered. Recurrence is also unrelated to IMAP, and folding it in would
give `imap_deck_sync.py` a second unrelated concern.

## Card Eligibility

A card is acted on when **all** of these hold:

1. It is in the source stack (default `Done`).
2. `archived` is falsy. A deliberately archived chore must not resurrect.
3. `deletedAt` is 0. Trashed cards are left in the trash.
4. It carries at least one recurrence label that parses.
5. At least one of those schedules fires on the evaluation date.

## Failure Modes

- **A named stack does not exist.** If `--source-name` or `--target-name`
  matches no stack on the board, the run logs an error and exits `1` without
  touching any card. A typo in a stack name must not be a silent no-op.
- **The board has no recurrence labels.** Logged at INFO, exit `0`. This is
  the expected state of a board nobody has configured yet.
- **A single card fails to move.** The remaining actions still run; the run
  exits `2`.

## The Move

`ncdeck`'s `move_card` is used, with `order=0` so the card lands at the top of
`Todo` where it is visible as newly due. When several cards fire on the same
day they are moved in board order, each to `order=0`, so they end up at the
top of `Todo` in reverse. Their relative order among themselves does not
matter and is not specified further.

Deck's done-tick must be cleared. Both current `Repeat: 25` cards carry
`done='2026-08-03T17:08:13+00:00'`; moving them while still ticked would
deliver them pre-completed. `move_card` today deliberately re-sends `done`
to preserve it, because Deck's `CardService::update()` collapses an omitted
`done` and an explicitly-null `done` to `setDone(null)`.

### ncdeck 1.1.0

Add `clear_done` to `move_card` on **both** clients:

```python
def move_card(self, card, target_stack_id, order=0, clear_done=False, board_id=None) -> Card
```

- `clear_done=False` (default) preserves current behaviour, so
  `imap_deck_sync` is unaffected.
- `clear_done=True` omits `done` from the payload, which is how Deck clears
  it.
- `board_id` stays last, per the parity convention. `tests/test_parity.py`
  compares public signatures across the two clients and will fail on a
  one-sided change.

Building the payload inside `deck_recur.py` instead was rejected: it would
duplicate the full-payload logic that exists precisely because partial
payloads silently blank `description` and `duedate`, leaving two copies to
track Deck's quirks.

## Collision With the Archive Pass

`imap_deck_sync.plan_archive` archives `Email`-labelled cards after 7 days in
`Done`. A card carrying both `Email` and a recurrence label would be archived
before it could ever recur. `plan_archive` gains a guard: cards carrying a
recurrence label are excluded from archiving. Recurrence wins.

## Why Not lastModified

`lastModified` cannot anchor anything on this board. Nine of the eleven cards
in `Done`, including cards created in 2022, share
`lastModified = 2026-08-04 16:11:11` to the second. The activity log names the
cause at that timestamp: a single card was dragged into `Done` via the web UI.

The ncdeck 1.0.0 fix stopped *ncdeck's* `move_card` from calling `/reorder`.
The Deck **web UI** still uses `/reorder` for drag-and-drop, and Deck's
`CardService::reorder()` restamps every card in the stack. Since completing a
chore is a UI drag, `lastModified` on a `Done` card records "when did anything
last land in this stack", not when that card was completed.

## Components

| File | Responsibility |
|---|---|
| `deck_recur.py` | `Schedule` model, label parsing, trigger evaluation, plan building (pure), plan execution |
| `nextcloud-deck-recur.py` | CLI entrypoint: argparse, olen logging, Discord alerting, exit codes |
| `deck-recur-wrapper.sh` | `HOME` resolution, 1Password lookups, invocation |
| `tests/test_deck_recur.py` | Table-driven tests over the pure functions; fake client for execution |
| chezmoi `dot_config/systemd/user/deck-recur.{service,timer}` | Daily unit |
| `README.md` | Grammar table and operation |

This mirrors the existing split: `imap_deck_sync.py` (logic) plus
`nextcloud-deck-imap-sync.py` (CLI) plus `imap-deck-sync-wrapper.sh`
(secrets).

### Data flow

```
wrapper.sh  --(op lookups)-->  nextcloud-deck-recur.py
                                      |
                              Config, logging, alerting
                                      |
                                 deck_recur.run()
                                      |
        get_board() -> labels ---> parse_labels() -> {label_id: Schedule}
        get_stacks() -> cards  ---> plan_recurrence(cards, schedules, date)
                                      |
                                 [RecurAction, ...]
                                      |
                              execute_plan(deck, dry_run)
                                      |
                        move_card(card, todo_id, order=0, clear_done=True)
```

### Key interfaces

```python
@dataclass(frozen=True)
class Schedule:
    kind: str           # "daily" | "weekdays" | "dayofmonth"
    weekdays: frozenset[int] = frozenset()   # 0=Monday
    day: int | None = None

def parse_schedule(expr: str) -> Schedule
    # raises ScheduleError on any unusable expression, including the
    # reserved interval form, whose message names it specifically
def parse_labels(labels, prefix) -> tuple[dict[int, Schedule], list[str]]
def fires_on(schedule: Schedule, on: date) -> bool
def plan_recurrence(cards, schedules, on, target_stack_id) -> list[RecurAction]
def execute_plan(actions, deck, dry_run=False) -> ExecutionSummary
```

`parse_labels` returns the parsed schedules **and** the warnings, rather than
logging directly, so it stays pure and testable.

## CLI

```
nextcloud-deck-recur.py --url URL -u USER -p PASS -b BOARD_ID
                        [--source-name Done] [--target-name Todo]
                        [--label-prefix "Repeat:"]
                        [--date YYYY-MM-DD] [--dry-run] [-v]
```

Every flag also reads a matching environment variable, following the sync's
`env()` helper.

- `--date` overrides the evaluation date. It exists so `Repeat: 25` can be
  tested without waiting until the 25th, and so a missed day can be replayed.
- `--dry-run` plans and logs, performing no mutation.

Exit codes match the sync: `0` success, `1` setup or connection failure,
`2` the run completed with per-card failures. As in the sync, `1` raises a
Discord error and `2` a Discord warning.

Logging uses the olen logger with app name `deck-recur` and icon `🔁`
(U+1F501, no variation selector).

## Testing

Parsing and trigger evaluation are pure, so they carry the weight:

- `parse_schedule`: each valid form; case and whitespace variants; the
  `14d` deferred-form message; invalid forms (`modnay`, `0`, `32`, empty,
  `mon,`, `mon,xyz`).
- `fires_on`: weekday matching; day-of-month exact; the clamp on 28 February,
  29 February in a leap year, and 30 April for `Repeat: 31`; a non-clamping
  day in a 31-day month.
- `parse_labels`: prefix matching and non-matching labels; warnings collected
  rather than raised.
- `plan_recurrence`: archived, trashed, unlabelled, and non-firing cards are
  all skipped; multi-label union fires when either schedule matches; one
  action per firing card.
- `execute_plan`: a fake client records `move_card` calls and asserts
  `clear_done=True` and `order=0`; `dry_run=True` records none; a per-card
  exception yields exit code 2 without aborting the remaining cards.

In `nextcloud-deck-client`, `test_parity.py` already enforces that the new
`clear_done` parameter lands on both clients with an identical signature. Add
payload tests asserting `done` is absent when `clear_done=True` and present
when `clear_done=False` with a non-null source `done`.

## Rollout

1. ncdeck 1.1.0: `clear_done`, tests, release to PyPI.
2. nextcloud-deck-cli: pin `nextcloud-deck-client>=1.1.0`, add the recurrence
   script, tests, README, and the `plan_archive` guard.
3. chezmoi: add the unit and timer, `chezmoi apply`, enable the timer.
4. Verify against the live board with
   `--date 2026-08-25 --dry-run`, which must plan a move for cards 323 and
   324 and nothing else.

## Deferred

**Interval schedules (`Repeat: 14d`).** "Every 14 days" is measured from
completion, which needs a per-card anchor. Neither available anchor is good:
an invisible description marker costs one card write and one spurious
activity entry per completed chore, and the Nextcloud activity log is free but
expires at `activity_expire_days` (30 by default) — precisely where long
intervals live. `lastModified` is unusable for the reason given above. The
grammar reserves the form and the parser reports it explicitly.
