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

# nextcloud-deck-imap-sync

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
from the Nextcloud Activity log rather than the card's `lastModified`, since
Deck rewrites `lastModified` on every card in a stack whenever any card moves
into that stack. Nextcloud keeps 30 days of activity, so the threshold has to
stay well below that: values of 25 or more are rejected at startup, and 0 or
less disables the archiving pass. Cards with no move-to-Done record in the
activity window are left alone.

Use `--dry-run` to see the full plan without making any IMAP or Deck changes
— the safe way to try it out.

**Private dependency:** `nextcloud-deck-imap-sync.py` imports logging and
Discord/IRC alerting helpers from a private package (`olen`) at module load,
so this entry point cannot currently run outside its author's environment.
The underlying library, `imap_deck_sync.py`, has no such dependency; removing
the `olen` dependency from the entry point is planned.

## Usage:
```
usage: nextcloud-deck-imap-sync.py [-h] [--url URL] [-u USERNAME]
                                   [-p PASSWORD] [-b BOARD_ID]
                                   [--todo-name TODO_NAME]
                                   [--doing-name DOING_NAME]
                                   [--done-name DONE_NAME]
                                   [--email-label-name EMAIL_LABEL_NAME]
                                   [--email-label-color EMAIL_LABEL_COLOR]
                                   [--imap-host IMAP_HOST]
                                   [--imap-port IMAP_PORT]
                                   [--imap-user IMAP_USER]
                                   [--imap-password IMAP_PASSWORD]
                                   [--imap-folder IMAP_FOLDER]
                                   [--archive-done-after-days ARCHIVE_DONE_AFTER_DAYS]
                                   [--dry-run] [-v]

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
  --dry-run             Plan everything; perform no IMAP or Deck mutations.
  -v, --verbose
```
