# Card Recurrence and done-tick Sync — Design

**Date:** 2026-08-04
**Status:** Approved

Two related conveniences for the `Personal` Deck board, plus the one library
change they share.

- **Feature A — Recurrence.** A label expresses a repeating schedule; a daily
  job returns matching cards from `Done` to `Todo`.
- **Feature B — done-tick sync.** Deck's per-card `done` field and membership
  of the `Done` stack are kept in agreement.
- **Prerequisite — ncdeck 1.1.0.** A single-field card update that does not
  blank the rest of the card.

Both are conveniences. Every card can still be dragged by hand, and neither
feature is load-bearing for anything.

---

# Prerequisite: ncdeck 1.1.0

Deck's card update endpoint treats every omitted field as a default rather
than leaving it alone: a partial payload silently blanks `description` and
`duedate`. `move_card` already works around this by re-sending the card's
full state. Both new features need to change exactly one field, so that
workaround has to become reusable rather than being copied.

Extract the payload builder and expose a general single-field update:

```python
def _card_payload(card: Card, **overrides) -> dict
    # Full state from card.source, with overrides applied.
    # Sole owner of Deck's blanking and `done` quirks.

def patch_card(self, card: Card, board_id: int | None = None, **changes) -> Card
    # PUT the card's own stack with the full payload plus `changes`.

def move_card(self, card, target_stack_id, order=0,
              clear_done=False, board_id=None) -> Card
    # As today, but built on _card_payload; clear_done omits `done`.
```

Notes:

- `clear_done=False` is the default, so `imap_deck_sync`'s existing move
  behaviour is unchanged.
- Clearing is expressed by *omitting* `done`, because Deck's
  `CardService::update()` collapses an omitted `done` and an explicitly-null
  `done` to `setDone(null)`. This is the one case where the quirk helps.
- `board_id` stays last on both methods, per the parity convention.
  `tests/test_parity.py` compares public signatures across `DeckClient` and
  `AsyncDeckClient` and fails on a one-sided change.
- Both methods land on both clients.

---

# Feature A: Label-Driven Recurrence

## Goal

A card sitting in `Done` returns to `Todo` automatically on its next
scheduled day. The deliverable is the grammar and the engine; labels are
created by hand in the Deck UI as they are needed, and the script never
creates one.

`Timeliste` and `Visma Expenses` are due on the 25th of every month. Both
already carry the label `Repeat: 25`.

## Non-Goals

- Creating labels. The board is the configuration; the script only reads it.
- Interval schedules measured from completion. See "Deferred".
- Changing how cards reach `Done`. Completion stays a manual action.

## The Label Grammar

A recurrence label is any Deck label whose title starts with a configurable
prefix, default `Repeat:`. The remainder is the schedule expression.

```
label-title := prefix WS* expr
expr        := "daily"
             | weekday ("," WS* weekday)*
             | day-of-month
             | interval          ; recognised but rejected, see "Deferred"
weekday     := "mon" | "tue" | "wed" | "thu" | "fri" | "sat" | "sun"
day-of-month:= integer 1..31
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
  28 February, on 29 February in a leap year, and on 30 April. Formally: a
  day-of-month `n` fires on date `D` when `D.day == n`, or when
  `n > days_in_month(D)` and `D.day == days_in_month(D)`.
- **An unparseable expression is loud.** `Repeat: modnay` emits a WARNING
  naming the label title and every card carrying it, and those cards are
  skipped. Silent failure is the dominant risk here: a typo would otherwise
  disable a chore permanently with no signal.
- **`Nd` gets its own message.** `Repeat: 14d` warns `interval schedules like
  '14d' are not implemented yet`, distinct from the generic parse error.

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
a catch-up window would solve a problem that does not exist here. Accepted
consequence: if the host is down for a whole trigger day, those cards wait
for the next occurrence. `OnFailure=notify-failure@%n.service` reports a
failed run, and `--date YYYY-MM-DD` replays a missed day in one command.

### Why daily, and why not inside imap-deck-sync

At a 5-minute cadence "today is the 25th" is true 288 times, so avoiding a
re-move after re-completion would require per-card bookkeeping. A daily run
makes schedule granularity (days) equal run granularity (days), and the rule
becomes naturally edge-triggered. Recurrence is also unrelated to IMAP.

## Card Eligibility

A card is acted on when all of these hold:

1. It is in the source stack (default `Done`).
2. `archived` is falsy. `include_archived=True` is ncdeck's default, so this
   must be filtered explicitly. A deliberately archived chore must not
   resurrect.
3. `deletedAt` is 0. Trashed cards stay in the trash.
4. It carries at least one recurrence label that parses.
5. At least one of those schedules fires on the evaluation date.

## The Move

`move_card(card, todo_stack_id, order=0, clear_done=True)`.

`order=0` puts the card at the top of `Todo`, where it reads as newly due.
When several cards fire on the same day they are moved in board order, each
to `order=0`, so they end up at the top in reverse. Their relative order
among themselves does not matter and is not specified further.

Clearing the tick is required, not cosmetic: both `Repeat: 25` cards
currently carry `done='2026-08-03T17:08:13+00:00'`, and moving them while
still ticked would deliver them pre-completed.

## Failure Modes

- **A named stack does not exist.** If `--source-name` or `--target-name`
  matches no stack, the run logs an error and exits `1` without touching any
  card. A typo in a stack name must not be a silent no-op.
- **The board has no recurrence labels.** Logged at INFO, exit `0`. This is
  the expected state of an unconfigured board.
- **A single card fails to move.** Remaining actions still run; exit `2`.

## Components

| File | Responsibility |
|---|---|
| `deck_recur.py` | `Schedule` model, label parsing, trigger evaluation, plan building (pure), plan execution |
| `nextcloud-deck-recur.py` | CLI entrypoint: argparse, olen logging, Discord alerting, exit codes |
| `deck-recur-wrapper.sh` | `HOME` resolution, 1Password lookups, invocation |
| `tests/test_deck_recur.py` | Table-driven tests over the pure functions; fake client for execution |
| chezmoi `dot_config/systemd/user/deck-recur.{service,timer}` | Daily unit |

This mirrors the existing split of `imap_deck_sync.py` (logic),
`nextcloud-deck-imap-sync.py` (CLI), and `imap-deck-sync-wrapper.sh`
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

`parse_labels` returns the parsed schedules **and** the warnings rather than
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

- `--date` overrides the evaluation date, so `Repeat: 25` can be tested
  without waiting until the 25th and a missed day can be replayed.
- `--dry-run` plans and logs, performing no mutation.

Exit codes match the sync: `0` success, `1` setup or connection failure, `2`
completed with per-card failures. `1` raises a Discord error, `2` a Discord
warning.

Logging uses the olen logger, app name `deck-recur`, icon `🔁` (U+1F501, no
variation selector).

---

# Feature B: done-tick Sync

## Goal

Keep Deck's per-card `done` field and membership of the `Done` stack in
agreement, in both directions.

## The Rule

> **Stack membership wins inside `Done`; the tick wins outside it.**
>
> - A card in `Done` with `done` unset → set `done` to now.
> - A card in `Todo` or `Doing` with `done` set → move it to `Done`.

This cannot oscillate. After one pass every card is either in `Done` and
ticked, or outside `Done` and unticked; neither clause then matches.

The one thing it makes inexpressible is un-ticking a card while leaving it in
`Done` — the next run re-ticks it. Un-ticking is expressed by dragging the
card out of `Done`.

Only the configured `Todo` and `Doing` stacks are scanned for the second
clause, not "every stack that is not `Done`", so adding a stack to the board
later does not silently enrol it.

Archived and trashed cards are skipped in both directions, for the same
reason as in Feature A.

## Interaction With the Other Passes

- **Recurrence.** Recurrence moves a card to `Todo` *and* clears `done`, so
  the done pass then sees an unticked card outside `Done` and does nothing.
  This holds in either run order, so the daily and 5-minute jobs need no
  coordination.
- **Archive pass.** `latest_done_at` identifies a move by the presence of
  `stackBefore` in the activity entry's rich parameters. A done-tick produces
  no `stackBefore`, so ticking cannot corrupt the archive anchor. This was
  verified against the live activity feed.
- **Ordering within `run()`.** The done pass runs after the IMAP passes and
  before the archive pass, on the same stack snapshot. Cards it moves into
  `Done` are therefore not archive candidates in the same run, which is
  correct: their move-to-`Done` time is now.

## Cost

Each tick writes one card and produces one activity entry. `latest_done_at`
ignores those entries, but they do consume the paging budget of
`fetch_deck_activity` (20 pages of 200). Steady state is one entry per card
entering `Done`, which is negligible. The first run is larger: nine of the
eleven cards currently in `Done` are unticked and will be ticked at once.

## Placement

A third pass inside `imap_deck_sync.py`, beside the archive pass, driven by
the existing 5-minute timer.

It needs a prompt cadence — ticking a card should move it within minutes, not
at tomorrow's daily run — and the sync already fetches the board and stacks,
holds the credentials, owns a timer, and reports to Discord. Adding a pass
costs no new API calls and no new infrastructure, and keeping it in one
process removes any interleaving with the IMAP passes.

The acknowledged cost is that `imap_deck_sync.py` gains a third pass and its
name describes it less well. Extracting a shared `deck_passes.py` was
considered and rejected for now: it would refactor working, reviewed code
before adding the feature. If a fourth pass appears, do the extraction then.

## Interfaces

```python
@dataclass(frozen=True)
class TickDoneAction:      # card in Done, done unset
    card_id: int
    card_title: str

@dataclass(frozen=True)
class MoveToDoneByTickAction:   # card in Todo/Doing, done set
    card_id: int
    card_title: str
    source_stack_id: int

def plan_done_sync(done_cards, open_cards, done_stack_id) -> list
```

`plan_done_sync` is pure. Execution extends `execute_plan` and
`ExecutionSummary` with the two new counters, following the existing
`ArchiveAction` pattern:

- `TickDoneAction` -> `patch_card(card, done=<now>)`. The card does not move,
  so only the field changes.
- `MoveToDoneByTickAction` -> `move_card(card, done_stack_id, order=0)` with
  `clear_done` left at its default, so the tick that triggered the move is
  preserved.

The tick value is an ISO 8601 timestamp with offset, matching the format Deck
returns (`2026-08-03T17:08:13+00:00`), generated in UTC.

## Configuration

The pass is on by default and disabled with `--no-done-sync`
(`store_false`), or by setting `DONE_SYNC=0` in the environment, so it can be
turned off without editing the unit. `--dry-run` covers it like every other
pass.

---

# Why Not lastModified

Neither feature can anchor anything on `lastModified`. Nine of the eleven
cards in `Done`, including cards created in 2022, share
`lastModified = 2026-08-04 16:11:11` to the second. The activity log names
the cause at that timestamp: a single card was dragged into `Done` via the
web UI.

The ncdeck 1.0.0 fix stopped *ncdeck's* `move_card` from calling `/reorder`.
The Deck **web UI** still uses `/reorder` for drag-and-drop, and Deck's
`CardService::reorder()` restamps every card in the stack. Since completing a
chore is a UI drag, `lastModified` on a `Done` card records when anything
last landed in that stack, not when that card was completed.

---

# Testing

## ncdeck 1.1.0

- `_card_payload`: full state round-trips; overrides win; `done` present when
  the source has it and `clear_done` is false; absent when `clear_done` is
  true.
- `patch_card`: changing one field leaves `description` and `duedate` intact
  in the sent payload; the request targets the card's current stack.
- `test_parity.py` already enforces that `patch_card` and the new `clear_done`
  parameter land identically on both clients.

## Feature A

Parsing and trigger evaluation are pure, so they carry the weight:

- `parse_schedule`: every valid form; case and whitespace variants; the `14d`
  deferred message; invalid forms (`modnay`, `0`, `32`, empty, `mon,`,
  `mon,xyz`).
- `fires_on`: weekday matching; day-of-month exact; the clamp on 28 February,
  29 February in a leap year, and 30 April for `Repeat: 31`; a non-clamping
  day in a 31-day month.
- `parse_labels`: prefix matching and non-matching labels; warnings collected
  rather than raised.
- `plan_recurrence`: archived, trashed, unlabelled, and non-firing cards all
  skipped; multi-label union fires when either schedule matches; one action
  per firing card.
- `execute_plan`: a fake client records `move_card` calls and asserts
  `clear_done=True` and `order=0`; `dry_run=True` records none; a per-card
  exception is counted in the summary without aborting the rest, which is
  what the CLI turns into exit code 2.

## Feature B

- `plan_done_sync`: a ticked card in `Todo` yields a move; an unticked card
  in `Done` yields a tick; an already-consistent card in either position
  yields nothing; archived and trashed cards yield nothing in both
  directions; a card in a stack that is neither `Todo`, `Doing`, nor `Done`
  is ignored.
- Convergence: applying the plan to a fake board and re-planning yields an
  empty second plan.
- `run()` wiring: the pass is skipped when disabled, and performs no
  mutations under `--dry-run`.

---

# Rollout

1. **ncdeck 1.1.0** — `_card_payload`, `patch_card`, `move_card(clear_done=)`,
   tests, release to PyPI.
2. **nextcloud-deck-cli** — pin `nextcloud-deck-client>=1.1.0`; add Feature A
   (script, CLI, wrapper, tests); add Feature B as a third pass in
   `imap_deck_sync.py` with tests; add the recurrence guard to
   `plan_archive`; document both in `README.md`.
3. **chezmoi** — add `deck-recur.service` and `deck-recur.timer`,
   `chezmoi apply`, enable the timer.
4. **Verify against the live board.** `nextcloud-deck-recur.py --date
   2026-08-25 --dry-run` must plan a move for cards 323 and 324 and nothing
   else. The done pass under `--dry-run` must plan ticks for the nine
   unticked `Done` cards and no moves.

## Collision Guard

`imap_deck_sync.plan_archive` archives `Email`-labelled cards after 7 days in
`Done`. A card carrying both `Email` and a recurrence label would be archived
before it could ever recur, so `plan_archive` gains a guard excluding cards
that carry a recurrence label. Recurrence wins.

---

# Deferred

**Interval schedules (`Repeat: 14d`).** "Every 14 days" is measured from
completion, which needs a per-card anchor. Neither available anchor is good:
an invisible description marker costs one card write and one spurious
activity entry per completed chore, and the Nextcloud activity log is free
but expires at `activity_expire_days` (30 by default) — precisely where long
intervals live. `lastModified` is unusable for the reason given above. The
grammar reserves the form and the parser reports it explicitly.
