#!/usr/bin/env python3

import argparse
import json
import os
import requests  # only for catching RequestException
import sys
import html
import math
from datetime import datetime, timezone
from dateutil import parser as dateparser  # built-in on most distros
from typing import List, Optional

from olen_deck import DeckClient, Stack, Card, Label
"""
List Nextcloud Deck cards from a board, grouped by lists (stacks).

Uses DeckClient from nextcloud_deck_core.py

Output modes:
  --json       : grouped JSON dicts
  --color      : ANSI-colored terminal output with emojis + label colors
  --pango      : Pango-markup text
  --markdown   : Markdown + HTML spans for label colors
"""




def env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


# ---------- Date formatting ----------

def format_duedate(dt: Optional[datetime], style: str = "iso") -> str:
    if not dt:
        return ""
    now = datetime.now(timezone.utc)
    if style == "iso":
        return dt.isoformat()
    elif style == "local":
        try:
            return dt.astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:
            return dt.strftime("%Y-%m-%d %H:%M")
    elif style == "relative":
        diff = (dt - now).total_seconds()
        days = diff / 86400
        if abs(days) < 1:
            hours = diff / 3600
            if hours > 0:
                return f"in {int(hours)} hour(s)"
            else:
                return f"{int(abs(hours))}hour(s) ago"
        if days > 365:
            return f"in {math.ceil(days / 365)} year(s)"
        elif days > 30:
            return f"in {math.ceil(days / 30)} month(s)"
        elif days > 0:
            return f"in {math.ceil(days)} days"
        elif days < -365:
            return f"{abs(math.ceil(days / 365))} year(s) ago"
        elif days < -30:
            return f"{abs(math.ceil(days / 30))} month(s) ago"
        else:
            return f"{abs(math.floor(days))} days ago"
    return dt.isoformat()





# ---------- ANSI / color helpers ----------

class Ansi:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"


def hex_to_rgb(s: str) -> tuple[int, int, int]:
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def ansi_label(label: Label) -> str:
    title = label.title or ""
    hexcol = label.color
    if not hexcol:
        return title
    r, g, b = hex_to_rgb(hexcol)
    return f"\033[38;2;{r};{g};{b}m{title}{Ansi.RESET}"


# ---------- Output formatters ----------
E_STACK, E_CARD, E_LABEL, E_OWNER, E_ASSIGNEES, E_DUE, E_ARCH, E_TODO, E_DONE = (
    "🗂", "📝", "🏷", "👤", "👥", "📅", "📦", "✔ ", "✅"
)

def card_icon(stack: Stack) -> str:
    if stack.title.lower() == "todo" or stack.title.lower() == "to do":
        return E_TODO
    elif stack.title.lower() == "done":
        return E_DONE
    return E_CARD

def card_markdown(stack: Stack) -> str:
    if stack.title.lower() == "todo" or stack.title.lower() == "to do":
        return "[x]"
    elif stack.title.lower() == "done" or stack.title.lower() == "doing":
        return "[ ]"
    return ""


def colorize_output(stacks: List[Stack], show_owner: bool, datefmt: str) -> str:
    lines: list[str] = []
    for s in stacks:
        title = s.title or f"List {s.id}"
        lines.append(f"\n{Ansi.BOLD}{Ansi.BLUE}{E_STACK}  {title}{Ansi.RESET}")
        if not s.cards:
            lines.append(f"{Ansi.DIM}(no cards){Ansi.RESET}")
            continue
        for c in s.cards:
            line = f"- {Ansi.BOLD}{card_icon(s)} {c.title or '(untitled)'}{Ansi.RESET}"
            if c.labels:
                rendered = ", ".join(ansi_label(lb) for lb in c.labels)
                line += f"  {E_LABEL}  {rendered}"
            if show_owner and c.owner:
                line += f"  {Ansi.CYAN}{E_OWNER} {c.owner}{Ansi.RESET}"
            if c.assignees:
                line += (
                    f"  {Ansi.CYAN}{E_ASSIGNEES} {', '.join(c.assignees)}{Ansi.RESET}"
                )
            if c.duedate:
                line += (
                    f"  {Ansi.YELLOW}{E_DUE} "
                    f"{format_duedate(c.duedate, datefmt)}{Ansi.RESET}"
                )
            if c.archived:
                line += f"  {Ansi.DIM}{E_ARCH} archived{Ansi.RESET}"
            lines.append(line)
    return "\n".join(lines).lstrip("\n")


def markdown_output(stacks: List[Stack], show_owner: bool, datefmt: str) -> str:
    lines: list[str] = []
    for s in stacks:
        title = s.title or f"List {s.id}"
        lines.append(f"## {title}")
        if not s.cards:
            lines.append("_(no cards)_")
            continue
        for c in s.cards:
            line = f"- {card_markdown(s)} **{c.title or '(untitled)'}**"
            meta: list[str] = []
            if c.labels:
                pieces = []
                for lb in c.labels:
                    t = lb.title or ""
                    col = lb.color
                    if col:
                        pieces.append(
                            f"<span style=\"color:#{col}\">{t}</span>"
                        )
                    else:
                        pieces.append(t)
                meta.append("labels: " + ", ".join(pieces))
            if show_owner and c.owner:
                meta.append(f"owner: {c.owner}")
            if c.assignees:
                meta.append("assignees: " + ", ".join(c.assignees))
            if c.duedate:
                meta.append("due: " + format_duedate(c.duedate, datefmt))
            if c.archived:
                meta.append("archived")
            if meta:
                line += f" — _{'; '.join(meta)}_"
            lines.append(line)
        lines.append("")
    return "\n".join(lines).strip()


def pango_escape(text: str) -> str:
    return html.escape(text, quote=True)


def pango_label(lb: Label) -> str:
    t = pango_escape(lb.title or "")
    if lb.color:
        return f"<span foreground='#{lb.color}'>{t}</span>"
    return t


def pango_output(stacks: List[Stack], show_owner: bool, datefmt: str) -> str:
    lines: list[str] = []
    for s in stacks:
        title = pango_escape(s.title or f"List {s.id}")
        lines.append(f"<b><span foreground='#1f6feb'>🗂 {title}</span></b>")
        if not s.cards:
            lines.append("<span foreground='#6e7781'>(no cards)</span>")
            continue
        for c in s.cards:
            t = pango_escape(c.title or "(untitled)")
            line = f"• <b>{card_icon(s)} {t}</b>"
            meta: list[str] = []
            if c.labels:
                meta.append("🏷 " + ", ".join(pango_label(lb) for lb in c.labels))
            if show_owner and c.owner:
                meta.append(
                    f"<span foreground='#58a6ff'>👤 {pango_escape(c.owner)}</span>"
                )
            if c.assignees:
                meta.append(
                    f"<span foreground='#58a6ff'>👥 "
                    f"{pango_escape(', '.join(c.assignees))}</span>"
                )
            if c.duedate:
                meta.append(
                    f"<span foreground='#d29922'>📅 "
                    f"{pango_escape(format_duedate(c.duedate, datefmt))}</span>"
                )
            if c.archived:
                meta.append("<span foreground='#6e7781'>📦 archived</span>")
            if meta:
                line += "  " + "  ".join(meta)
            lines.append(line)
    return "\n".join(lines)


def plain_output(stacks: List[Stack], show_owner: bool, datefmt: str) -> str:
    lines: list[str] = []
    for s in stacks:
        title = s.title or f"List {s.id}"
        lines.append(f"\n=== {title} ===")
        if not s.cards:
            lines.append("(no cards)")
            continue
        for c in s.cards:
            line = f"- {c.title or '(untitled)'}"
            bits: list[str] = []
            if c.labels:
                bits.append("labels: " + ", ".join(lb.title for lb in c.labels))
            if show_owner and c.owner:
                bits.append(f"owner: {c.owner}")
            if c.assignees:
                bits.append("assignees: " + ", ".join(c.assignees))
            if c.duedate:
                bits.append("due: " + format_duedate(c.duedate, datefmt))
            if c.archived:
                bits.append("archived")
            if bits:
                line += "  [" + "; ".join(bits) + "]"
            lines.append(line)
    return "\n".join(lines).lstrip("\n")


# ---------- Main ----------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="List Nextcloud Deck cards from a board, grouped by lists (stacks)."
    )
    parser.add_argument("--url", default=env("NEXTCLOUD_BASE_URL"), help="Base URL")
    parser.add_argument("-u", "--username", default=env("NEXTCLOUD_USERNAME"))
    parser.add_argument("-p", "--password", default=env("NEXTCLOUD_PASSWORD"))
    parser.add_argument(
        "-b",
        "--board-id",
        type=int,
        default=int(env("NEXTCLOUD_BOARD_ID", "0") or 0),
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        default=env("NEXTCLOUD_INCLUDE_ARCHIVED") == "1",
        help="Include archived cards",
    )

    # Output modes
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--json", action="store_true")
    group.add_argument("--color", action="store_true")
    group.add_argument("--pango", action="store_true")
    group.add_argument("--markdown", action="store_true")
    group.add_argument("--plain", action="store_true", default=True)

    # Display options
    parser.add_argument(
        "--show-owner",
        action="store_true",
        help="Show card owner (default off in non-JSON modes)",
    )
    parser.add_argument(
        "--date-format",
        choices=["iso", "local", "relative"],
        default="relative",
        help="How to display due dates",
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
        print("Missing: " + ", ".join(missing), file=sys.stderr)
        parser.print_help(sys.stderr)
        sys.exit(2)

    client = DeckClient(args.url, args.username, args.password, args.board_id)

    try:
        stacks = client.fetch_stacks(include_archived=args.include_archived)
    except requests.RequestException as e:
        print(f"Error fetching stacks: {e}", file=sys.stderr)
        sys.exit(1)

    # Output precedence: json > markdown > pango > color > plain
    if args.json:
        def default(o):
            from datetime import datetime
            if isinstance(o, datetime):
                return o.isoformat()
            return o

        grouped = [
            {
                "stack": {
                    "id": s.id,
                    "title": s.title,
                    "order": s.order,
                },
                "cards": [client.to_dict(c) for c in s.cards],
            }
            for s in stacks
        ]
        print(
            json.dumps(
                {
                    "board_id": args.board_id,
                    "api_base": client.api_base,
                    "stacks": grouped,
                },
                indent=2,
                ensure_ascii=False,
                default=default,
            )
        )
        return

    if args.markdown:
        print(markdown_output(stacks, args.show_owner, args.date_format))
        return

    if args.pango:
        print(pango_output(stacks, args.show_owner, args.date_format))
        return

    if args.color:
        print(colorize_output(stacks, args.show_owner, args.date_format))
        return

    print(plain_output(stacks, args.show_owner, args.date_format))


if __name__ == "__main__":
    main()

