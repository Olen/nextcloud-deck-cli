#!/usr/bin/env python3

import argparse
import os
import requests  # just for catching RequestException
import sys

from ncdeck import DeckClient, find_card_by_title, find_stack_by_name
"""
Tiny Nextcloud Deck CLI "todo" helper.

Operations:

  todo --add   "Task title"
    -> create a new card in the "Todo" stack

  todo --doing "Task title"
    -> move card with that title to the "Doing" stack

  todo --done  "Task title"
    -> move card with that title to the "Done" stack

  todo --do    "Task title"
    -> move card with that title back to the "Todo" stack

Stack names are configurable via:
  TODO_STACK_NAME, DOING_STACK_NAME, DONE_STACK_NAME
or via flags:
  --todo-name, --doing-name, --done-name
"""



def env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    CYAN = "\033[36m"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Nextcloud Deck 'todo' helper: add/move cards between stacks."
    )
    parser.add_argument("--url", default=env("NEXTCLOUD_BASE_URL"), help="Base URL")
    parser.add_argument("-u", "--username", default=env("NEXTCLOUD_USERNAME"))
    parser.add_argument("-p", "--password", default=env("NEXTCLOUD_PASSWORD"))
    parser.add_argument(
        "-b",
        "--board-id",
        type=int,
        default=int(env("NEXTCLOUD_BOARD_ID", "0") or 0),
        help="Board ID",
    )

    # Stack names
    parser.add_argument(
        "--todo-name", default=env("TODO_STACK_NAME", "Todo"), help='Name of "Todo" stack'
    )
    parser.add_argument(
        "--doing-name", default=env("DOING_STACK_NAME", "Doing"), help='Name of "Doing" stack'
    )
    parser.add_argument(
        "--done-name", default=env("DONE_STACK_NAME", "Done"), help='Name of "Done" stack'
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add", metavar="TITLE", help='Add new card to "Todo" stack')
    group.add_argument("--doing", metavar="TITLE", help='Move card to "Doing" stack')
    group.add_argument("--done", metavar="TITLE", help='Move card to "Done" stack')
    group.add_argument(
        "--do",
        metavar="TITLE",
        dest="todo_move",
        help='Move card back to "Todo" stack',
    )

    args = parser.parse_args()
    missing = [
        name
        for name, ok in [
            ("base URL", bool(args.url)),
            ("username", bool(args.username)),
            ("app password", bool(args.password)),
            ("board id", bool(args.board_id)),
        ]
        if not ok
    ]
    if missing:
        print(f"{C.RED}Missing: {', '.join(missing)}{C.RESET}", file=sys.stderr)
        parser.print_help(sys.stderr)
        sys.exit(2)

    client = DeckClient(args.url, args.username, args.password, args.board_id)

    # First fetch stacks (with cards) so we can resolve names
    try:
        stacks = client.get_stacks(include_archived=True)
    except requests.RequestException as e:
        print(f"{C.RED}Error fetching stacks: {e}{C.RESET}", file=sys.stderr)
        sys.exit(1)

    todo_stack = find_stack_by_name(stacks, args.todo_name)
    doing_stack = find_stack_by_name(stacks, args.doing_name)
    done_stack = find_stack_by_name(stacks, args.done_name)

    if not todo_stack:
        print(
            f"{C.RED}Could not find 'Todo' stack named '{args.todo_name}'.{C.RESET}",
            file=sys.stderr,
        )
        sys.exit(1)
    if not doing_stack:
        print(
            f"{C.RED}Could not find 'Doing' stack named '{args.doing_name}'.{C.RESET}",
            file=sys.stderr,
        )
        sys.exit(1)
    if not done_stack:
        print(
            f"{C.RED}Could not find 'Done' stack named '{args.done_name}'.{C.RESET}",
            file=sys.stderr,
        )
        sys.exit(1)

    # -------- Add --------
    if args.add is not None:
        title = args.add
        try:
            card = client.create_card(todo_stack.id, title)
        except requests.RequestException as e:
            print(f"{C.RED}Failed to create card: {e}{C.RESET}", file=sys.stderr)
            sys.exit(1)
        print(
            f"{C.GREEN}➕ Created card #{card.id} in '{todo_stack.title}': "
            f"{card.title}{C.RESET}"
        )
        return

    # -------- Move --------
    # Refresh stacks to ensure we see the newly created card too
    try:
        stacks = client.get_stacks(include_archived=True)
    except requests.RequestException as e:
        print(f"{C.RED}Error fetching stacks: {e}{C.RESET}", file=sys.stderr)
        sys.exit(1)

    if args.doing is not None:
        title = args.doing
        target_stack = doing_stack
    elif args.done is not None:
        title = args.done
        target_stack = done_stack
    else:  # args.todo_move
        title = args.todo_move
        target_stack = todo_stack

    found = find_card_by_title(stacks, title)
    if not found:
        print(f"{C.RED}No card found with title: {title}{C.RESET}\n", file=sys.stderr)
        sys.exit(1)

    current_stack, card = found
    if current_stack.id == target_stack.id:
        print(
            f"{C.YELLOW}Card '{card.title}' is already in "
            f"'{target_stack.title}'.{C.RESET}\n"
        )
        return

    try:
        updated = client.move_card(card, target_stack.id)
    except requests.RequestException as e:
        print(f"{C.RED}Failed to move card: {e}{C.RESET}\n", file=sys.stderr)
        sys.exit(1)

    print(
        f"{C.CYAN}➡  Moved card #{updated.id} '{updated.title}' "
        f"from '{current_stack.title}' to '{target_stack.title}'.{C.RESET}\n"
    )


if __name__ == "__main__":
    main()

