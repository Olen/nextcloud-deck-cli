# nextcloud-deck-list

Simple cli tool for Nextcloud Deck

Built on the [nextcloud-deck-client](https://github.com/Olen/nextcloud-deck-client)
library, which handles the Nextcloud Deck API calls.

## Usage:
```
nextcloud-deck-list.py [-h] [--url URL] [-u USERNAME] [-p PASSWORD] [-b BOARD_ID] [--include-archived] [--json] [--color] [--pango] [--markdown] [--show-owner]
                             [--date-format {iso,local,relative}]

List Nextcloud Deck cards from a board, grouped by lists (stacks).


options:
  -h, --help            show this help message and exit
  --url URL             Base URL, e.g. https://cloud.example.com
  -u, --username USERNAME
                        Username
  -p, --password PASSWORD
                        App password
  -b, --board-id BOARD_ID
                        Board ID
  --include-archived    Include archived cards
  --json                Output grouped JSON dicts (always includes owner)
  --color               ANSI-colored terminal output with emojis
  --pango               Pango-markup text
  --markdown            Markdown-formatted output
  --show-owner          Show card owner (default off in non-JSON modes)
  --date-format {iso,local,relative}
                        How to display due dates (default: relative)
```

# nextcloud-deck-todo

Helper for TODO-lists 

Requires a board with 3 stacks:

* Todo
* Doing
* Done

The script allows you to easily add new items to the Todo-list, or move them between the stacks

## Usage:
```
usage: nextcloud-deck-todo.py [-h] [--url URL] [-u USERNAME] [-p PASSWORD] [-b BOARD_ID] [--todo-name TODO_NAME] [--doing-name DOING_NAME] [--done-name DONE_NAME]
                              (--add TITLE | --doing TITLE | --done TITLE | --do TITLE)

Nextcloud Deck 'todo' helper: add/move cards between stacks.

options:
  -h, --help            show this help message and exit
  --url URL             Base URL
  -u, --username USERNAME
  -p, --password PASSWORD
  -b, --board-id BOARD_ID
                        Board ID
  --todo-name TODO_NAME
                        Name of "Todo" stack
  --doing-name DOING_NAME
                        Name of "Doing" stack
  --done-name DONE_NAME
                        Name of "Done" stack
  --add TITLE           Add new card to "Todo" stack
  --doing TITLE         Move card to "Doing" stack
  --done TITLE          Move card to "Done" stack
  --do TITLE            Move card back to "Todo" stack
```

# nextcloud-deck-sync

Syncs starred email to a Nextcloud Deck board. Starring a message in any mail
client creates a card in the `Todo` stack. Moving that card to `Done` clears
the star on the message. Unstarring the message moves its card to `Done`.
Cards it creates carry a configurable label (default `Email`).

It watches one IMAP folder that aggregates the flagged messages you want
synced, selected with `--imap-folder` (default `_Virtual/Important`). A
Dovecot `virtual` mailbox is one way to build such a folder out of messages
flagged across other folders; any IMAP folder that surfaces the same set of
messages works.

Cards carrying the label that have sat in `Done` longer than
`--archive-done-after-days` (default 7) are archived. Time-in-Done is read
from the Nextcloud Activity log rather than the card's `lastModified`,
because `lastModified` does not mean what it looks like it means: Deck's web
UI still moves cards with the `reorder` endpoint, which rewrites
`lastModified` on **every** card in the target stack. One drag-and-drop into
`Done` therefore restamps everything already sitting in `Done`, so a card's
`lastModified` records when anything last landed in that stack, not when that
card did. (This client's `move_card()` no longer uses `reorder` — since
ncdeck 1.1.0 it PUTs the update route addressed at the target stack, see
[Deck#6830](https://github.com/nextcloud/deck/issues/6830) — but the UI does,
and the UI is how cards usually get there.)

Activity retention is admin-configurable via Nextcloud's
`activity_expire_days` setting and was 30 days on the instance this was built
against — size the threshold against your own instance's setting. Values of
25 or more are rejected at startup, and 0 or less disables the archiving
pass. Cards with no move-to-Done record in the activity window are left
alone.

Cards carrying a recurrence label (see `nextcloud-deck-recur` below) are never
archived, and are never moved back to `Done` by the unstarred-message pass —
either would break the recurrence cycle. Set `--recur-label-prefix` /
`RECUR_LABEL_PREFIX` to the same value as `nextcloud-deck-recur.py`'s
`--label-prefix`; an empty prefix is rejected at startup, since it would match
every label and disable both passes board-wide.

### done-tick sync

Deck's per-card done-tick and membership of the `Done` stack are kept in
agreement on every run:

- a card in `Done` with no done-tick gets one
- a card in `Todo` or `Doing` with a done-tick is moved into `Done`

Stack membership wins inside `Done`; the tick wins outside it. That is
loop-free — after one pass every card is either in `Done` and ticked, or
outside `Done` and unticked.

The consequence is that un-ticking a card while leaving it in `Done` does not
stick: the next run re-ticks it. To reopen a card, drag it out of `Done`.

Archived and trashed cards are left alone. Disable the pass with
`--no-done-sync` or `DONE_SYNC=0`.

Use `--dry-run` to see the full plan without making any IMAP or Deck changes
— the safe way to try it out.

**Private dependency:** `nextcloud-deck-sync.py` imports logging and
Discord/IRC alerting helpers from a private package (`olen`) at module load,
so this entry point cannot currently run outside its author's environment.
The underlying library, `deck_sync.py`, has no such dependency; removing
the `olen` dependency from the entry point is planned.

## Usage:
```
usage: nextcloud-deck-sync.py [-h] [--url URL] [-u USERNAME] [-p PASSWORD]
                              [-b BOARD_ID] [--todo-name TODO_NAME]
                              [--doing-name DOING_NAME]
                              [--done-name DONE_NAME]
                              [--email-label-name EMAIL_LABEL_NAME]
                              [--email-label-color EMAIL_LABEL_COLOR]
                              [--imap-host IMAP_HOST] [--imap-port IMAP_PORT]
                              [--imap-user IMAP_USER]
                              [--imap-password IMAP_PASSWORD]
                              [--imap-folder IMAP_FOLDER]
                              [--archive-done-after-days ARCHIVE_DONE_AFTER_DAYS]
                              [--recur-label-prefix RECUR_LABEL_PREFIX]
                              [--dry-run] [--no-done-sync] [-v]

Sync IMAP starred messages to/from a Nextcloud Deck Todo board.

options:
  -h, --help            show this help message and exit
  --url URL             Nextcloud base URL
  -u, --username USERNAME
  -p, --password PASSWORD
  -b, --board-id BOARD_ID
  --todo-name TODO_NAME
  --doing-name DOING_NAME
  --done-name DONE_NAME
  --email-label-name EMAIL_LABEL_NAME
                        Name of the Deck label applied to email-derived cards
  --email-label-color EMAIL_LABEL_COLOR
                        Hex color (no '#') for the Email label if it has to be
                        created
  --imap-host IMAP_HOST
  --imap-port IMAP_PORT
  --imap-user IMAP_USER
  --imap-password IMAP_PASSWORD
  --imap-folder IMAP_FOLDER
  --archive-done-after-days ARCHIVE_DONE_AFTER_DAYS
                        Archive Email-labelled cards that have been in Done
                        this many days. 0 or less disables the pass.
  --recur-label-prefix RECUR_LABEL_PREFIX
                        Title prefix marking a label as a recurrence schedule.
                        Cards carrying one are never archived and are never
                        moved back to Done by the unstarred-message pass. Must
                        match nextcloud-deck-recur.py's --label-prefix.
  --dry-run             Plan everything; perform no IMAP or Deck mutations.
  --no-done-sync        Disable the done-tick <-> Done stack sync pass.
  -v, --verbose
```

# nextcloud-deck-recur

Returns recurring cards from `Done` to `Todo` on the days their label names.

A recurrence label is any Deck label whose title starts with `Repeat:`. The
rest of the title is the schedule. Create the labels you need by hand in the
Deck UI; this script only reads them.

| Label title | Fires on |
|---|---|
| `Repeat: daily` | every day |
| `Repeat: mon` | every Monday |
| `Repeat: mon,thu` | every Monday and Thursday |
| `Repeat: 1` | the 1st of each month |
| `Repeat: 25` | the 25th of each month |

- Weekday names are `mon tue wed thu fri sat sun`, comma-separated.
- Day-of-month clamps to the end of short months, so `Repeat: 31` fires on
  28 February and on 30 April.
- The prefix and expression are matched case-insensitively.
- Several recurrence labels on one card union together.
- An unparseable expression is logged as a WARNING naming the label and the
  cards carrying it, and those cards are skipped, so a typo is visible rather
  than silently disabling a chore.
- Interval schedules (`Repeat: 14d`) are **not implemented**. The parser
  recognises the form and says so explicitly.

When a card is returned to `Todo` its Deck done-tick is cleared, so it
arrives as an open task.

There is no state file. The move out of `Done` is itself the record that the
card has recurred, which is why this must run **once a day** — at a shorter
interval, re-completing a card on its own trigger day would send it straight
back. Deployed as `deck-recur.timer`, daily at 04:17 — deliberately off the
`*:0/5` boundary `deck-sync.timer` fires on, so the two jobs do not snapshot
the same card at the same moment.

If the host is down for a whole trigger day, those cards wait for the next
occurrence. Replay a missed day with `--date`.

## Usage:

```
nextcloud-deck-recur.py --url URL -u USER -p PASS -b BOARD_ID
                        [--source-name Done] [--target-name Todo]
                        [--label-prefix "Repeat:"]
                        [--date YYYY-MM-DD] [--dry-run] [-v]
```

Every flag also reads a matching environment variable: `NEXTCLOUD_BASE_URL`,
`NEXTCLOUD_USERNAME`, `NEXTCLOUD_PASSWORD`, `NEXTCLOUD_BOARD_ID`,
`DONE_STACK_NAME`, `TODO_STACK_NAME`, `RECUR_LABEL_PREFIX`, `RECUR_DATE`.

`--date` evaluates a date other than today, for testing a schedule without
waiting for it and for replaying a missed day:

```bash
./deck-recur-wrapper.sh --date 2026-08-25 --dry-run -v
```

Exit codes: `0` clean, `1` setup or connection failure, `2` completed with
per-card failures.
