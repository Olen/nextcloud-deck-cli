# nextcloud-deck-cli

Simple cli tool for Nextcloud Deck

## Usage:
nextcloud-deck-cli.py [-h] [--url URL] [-u USERNAME] [-p PASSWORD] [-b BOARD_ID] [--include-archived] [--json] [--color] [--pango] [--markdown] [--show-owner]
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
