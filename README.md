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
