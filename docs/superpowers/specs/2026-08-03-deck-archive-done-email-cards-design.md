# Auto-archive aged Email cards from Done — design

**Status:** Approved 2026-08-03
**Author:** olen (brainstormed with Claude)
**Extends:** `imap_deck_sync.py`, `nextcloud-deck-imap-sync.py`
**Depends on:** `olen-deck` >= 0.3.0
**Prior art:** `docs/superpowers/specs/2026-05-11-imap-deck-sync-design.md`

## Goal

Keep the Deck `Done` stack from growing without bound. Cards that came from
starred email and have sat in `Done` for longer than a configurable period
(default one week) are archived automatically. Cards the user created by hand
are never touched.

Archiving uses Deck's own archive feature, which is reversible from the board's
archive view and via `PUT .../cards/{cardId}/unarchive`.

## Background: why the obvious approach does not work

The naive implementation reads `card.lastModified` and archives anything older
than the threshold. **`lastModified` is unusable here.**

Observed live on board 4 on 2026-07-29:

- 14:31 — the sync logged `Moving card #319 to stack #9` (one card into `Done`)
- immediately afterwards **all 28 cards in `Done`** reported
  `lastModified = 14:31`, including cards created in 2022 and 2024 that the sync
  has never touched
- the 14:36 / 14:41 / 14:46 / 14:50 runs performed no actions, and the timestamp
  stayed at 14:31

Cause: `olen_deck.move_card()` implements the workaround for Deck issue #6830 by
`PUT`ing to `/apps/deck/cards/{id}/reorder`. Deck renumbers the order of every
card in the target stack, bumping `lastModified` on all of them.

Consequence: every new arrival in `Done` resets the clock for everything already
there. A one-week rule would archive nothing while mail keeps flowing, then
archive the entire backlog at once after a single quiet week.

`card.done` is `None` on every card — Deck's own done-tracking is not in use, so
it is not an alternative either.

## Source of truth: the Nextcloud Activity log

`GET /ocs/v2.php/apps/activity/api/v2/activity/deck` returns Deck activity for
the authenticated user, newest first, including move events:

```json
{
  "object_type": "deck_card",
  "object_id": 295,
  "datetime": "2026-06-30T08:55:54+00:00",
  "subject": "You have moved the card ... from list Todo to Done",
  "subject_rich": ["...", {
      "board": {"id": "4",  "name": "Personal"},
      "stack": {"id": "9",  "name": "Done"},
      "stackBefore": {"id": "22", "name": "Todo"}
  }]
}
```

Verified properties:

- **Moves made by the sync are recorded identically to manual moves.** Card #319,
  moved by the service at 12:31 UTC, produced an entry with `stack.id = 9`.
- **One bulk call covers every card.** There is no per-card fan-out. The
  per-card form (`.../activity/filter?object_type=deck_card&object_id=N`) also
  works but is not needed.
- **`object_type=deck_board` is not valid** — it returns 304. There is no
  board-scoped query; filtering by board happens client-side.
- **The feed spans all boards.** Recipe-board cards appear in the same response,
  so `board.id` must be filtered explicitly.
- **Retention is 30 days** (`occ config:system:get activity_expire_days` = 30),
  confirmed by the oldest available entry being 29 days old. Of 17 Email-labelled
  cards currently in `Done`, only 8 have a usable record.

### Handling repeated moves

A card may be moved back and forth between `Todo` and `Done`. Because the feed
is newest-first, taking the **first** matching entry per card yields the latest
move into `Done`, which is the correct one.

Combined with the requirement that the card is *currently* in `Done` (from the
stacks query), the two sources cross-check each other: a stale activity entry
cannot archive a card that has since been dragged back out.

### How far back to page

Paging back only to the threshold is not sufficient. A card with no entry inside
that window is ambiguous — either it moved to `Done` before the window (eligible)
or it has no record at all (must be ignored). Distinguishing the two requires
seeing the whole retention window.

Because retention is a hard 30 days, "fetch everything available" is finite and
well defined. On the current board that is 32 entries in a single request.
Pagination via `since=<activity_id>` is bounded at 20 pages x 200 entries purely
as a runaway guard.

## Decisions

| Question | Decision |
|---|---|
| Which cards are eligible | Cards in `Done` carrying the **Email label**. The sync marker is not required, so card #259 (label, no marker) is included. |
| Card with no "moved to Done" record | **Ignored.** The user archives those manually. |
| Threshold | `--archive-done-after-days`, env `ARCHIVE_DONE_AFTER_DAYS`, default **7**. |
| `<= 0` | Pass disabled. |
| `>= 25` | Hard error at startup — with 30-day retention the records would expire before a card could qualify, and archiving would silently stop working. |
| Archive vs delete | Archive. Reversible. |

## Architecture

Approach: a **separate pure planner** whose actions join the existing plan.
`make_plan` is left untouched — it is a pure IMAP<->Deck reconciliation over two
dicts, and archiving is a time-based cleanup with no IMAP involvement. Adding a
third input dict and a clock to it would blur its single purpose and disturb the
existing test suite.

```
run(config)
  |- DeckClient(...)
  |- fetch_stacks(include_archived=False)      # archived cards drop out here
  |- resolve Todo / Doing / Done stack ids
  |- find_or_create Email label                -> email_label_id
  |- fetch_managed(stacks)                     -> {msgid: ManagedCard}
  |- IMAP fetch_starred(...)                   -> {msgid: StarredMessage}
  |
  |- NEW  get_deck_activity()                  -> raw entries (newest-first)
  |- NEW  latest_done_at(entries, board, done) -> {card_id: unix_ts}   [pure]
  |
  |- actions  = make_plan(...)                                         [unchanged]
  |- actions += plan_archive(done_cards, done_at, email_label_id,
  |                          now, max_age_days)                        [pure, NEW]
  |- execute_plan(actions, mailbox, deck, dry_run)   # one new branch
```

This inherits `--dry-run`, per-action failure counting, and the `rc=2` Discord
warning path for free.

## Components

### `olen_deck` (shared package, additive)

Both consumers (`nextcloud-deck-cli`, SmartList) are unaffected by additive
methods.

```python
def archive_card(self, stack_id, card_id, board_id=None) -> Card
    # PUT /api/v1.0/boards/{boardId}/stacks/{stackId}/cards/{cardId}/archive
    # route confirmed present in Deck 1.17.3 (appinfo/routes.php:108)

def get_deck_activity(self, limit=200, since=None) -> list[dict]
    # GET /ocs/v2.php/apps/activity/api/v2/activity/deck
    # returns [] on HTTP 304; paginates via `since`
```

`get_deck_activity` belongs in `olen_deck` rather than a second HTTP stack in
the CLI because it reuses the client's authenticated session and base URL.

### `imap_deck_sync.py`

```python
@dataclass
class ArchiveAction:
    stack_id: int
    card_id: int
    card_title: str
    done_at: int

def latest_done_at(entries, board_id, done_stack_id) -> dict[int, int]
def plan_archive(done_cards, done_at, email_label_id, now, max_age_days) -> list[ArchiveAction]
```

`latest_done_at` iterates newest-first and keeps the first entry per card where
`board.id == board_id`, `stack.id == done_stack_id`, and the event is a move.

`plan_archive` eligibility, in order:

1. card is in the `Done` stack
2. card carries `email_label_id`
3. `card.id in done_at` — otherwise skip silently
4. `now - done_at[card.id] >= max_age_days * 86400`

`now` is a parameter and is never read from the clock inside the planner, which
keeps the time-based rule testable without freezing time globally.

## Error handling

| Situation | Behaviour |
|---|---|
| Activity API returns **304** | Not an error — this endpoint signals "no data" with a status code. Return `[]`. `done_at` empty -> nothing eligible -> pass no-ops. Parsing the empty body as JSON crashes; must be handled explicitly. |
| Activity fetch raises | Log ERROR, skip the archive pass, set `rc=2`. The main sync still completes. Routed to the existing Discord warning; deliberately not silent. |
| `archive_card()` fails on one card | Counted as a per-action failure; remaining actions still run. Matches existing `execute_plan` behaviour. |
| Card has no `done_at` | Skipped silently, by decision. |
| Card moved to `Done` during this run | Its activity entry postdates the feed already fetched, so it has no `done_at` and is skipped. It archives on a later run. No ordering hazard. |
| Card moved by a different Nextcloud user | The feed is per-user, so no record -> skipped. |
| Deck API entirely down | `fetch_stacks` fails first -> `rc=1` -> Discord error, unchanged. |

### Timezone

Activity timestamps are offset-aware UTC (`...+00:00`). They must be parsed as
aware datetimes and compared as epoch integers. Naive parsing compared against a
local-time `now` silently shifts everything by the CEST offset.

## Behaviour change worth naming

`fetch_stacks(include_archived=False)` means an archived card leaves `managed`.
If the underlying message is starred again later, Pass C creates a **fresh**
card rather than resurrecting the archived one. This is intended.

## Testing

Existing `tests/test_imap_deck_sync.py` is untouched; `make_plan` does not change.

**`latest_done_at`** — filters other boards out; filters non-`Done` stacks out;
newest-first wins for a card moved Todo->Done->Todo->Done; ignores non-move
events (tag added, description updated — both present in the real feed);
malformed entries missing `subject_rich` / `stack` / `board` are skipped rather
than fatal; empty input; UTC-to-epoch conversion is offset-correct.

**`plan_archive`** (injected `now`) — not in `Done` -> skip; no Email label ->
skip; absent from `done_at` -> skip; below threshold -> skip; exactly at the
boundary -> archive; above -> archive; a mixed set yields only the right actions.

**Config validation** — `<= 0` disables; `>= 25` raises at startup.

**`execute_plan` archive branch** — `--dry-run` performs zero API calls; the real
path calls `archive_card` with the correct `(stack_id, card_id, board_id)`; a
raised exception is counted while remaining actions still run.

**`olen_deck`** — `archive_card` issues `PUT` to the v1.0 route;
`get_deck_activity` returns `[]` on 304 and paginates via `since`.

The repository has no CI configuration, so a locally passing `pytest` is the gate.

## Rollout

1. `olen_deck`: add both methods plus tests, bump `0.2.1 -> 0.3.0`, run
   `./update.sh` to build, upload to the Gitea PyPI, and reinstall.
2. `nextcloud-deck-cli`: implement and test; pin the new version in
   `requirements.txt`.
3. Dry run: `./imap-deck-sync-wrapper.sh --dry-run -v` and confirm the reported
   set (approximately 8 cards at time of writing).
4. No wrapper or systemd change is required — the default is 7 and the wrapper
   already forwards `"$@"`.
5. Observe one timer cycle.

## Out of scope

- Archiving cards in stacks other than `Done`.
- Any change to `make_plan` or to the IMAP side of the sync.
- Backfilling the 9 cards whose activity records have already expired; those are
  archived manually.
- Raising `activity_expire_days` above 30.
