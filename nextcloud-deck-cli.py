#!/usr/bin/env python3
"""
List Nextcloud Deck cards from a specific board, grouped by lists (stacks).

Direct app route, API v1.1 only:
  {BASE_URL}/index.php/apps/deck/api/v1.1/boards/{board_id}/stacks

Output modes:
  --json       : grouped JSON dicts (includes owner)
  --color      : ANSI-colored terminal output with emojis
  --pango      : Pango-markup text (for GTK labels)
  --markdown   : Markdown-formatted output
Precedence if multiple flags given: json > markdown > pango > color > default.

Env (or flags):
  NEXTCLOUD_BASE_URL, NEXTCLOUD_USERNAME, NEXTCLOUD_PASSWORD, NEXTCLOUD_BOARD_ID
  NEXTCLOUD_INCLUDE_ARCHIVED=1  # include archived cards
"""

import argparse
import json
import os
import sys
import html
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
from dateutil import parser as dateparser  # built-in on most distros
import requests
import math

API_BASE_SUFFIX = "/index.php/apps/deck/api/v1.1"
HEADERS = {
    "OCS-APIRequest": "true",
    "Accept": "application/json",
}

# ---------- Utility ----------

def env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v if v is not None and v != "" else default

def stacks_url(base: str, board_id: int) -> str:
    return f"{base.rstrip('/')}{API_BASE_SUFFIX}/boards/{board_id}/stacks"

def get_json(r: requests.Response) -> Any:
    r.raise_for_status()
    return r.json()

def fetch_stacks(session: requests.Session, base_url: str, board_id: int) -> List[Dict[str, Any]]:
    r = session.get(stacks_url(base_url, board_id), timeout=30)
    data = get_json(r)
    if isinstance(data, dict) and "ocs" in data and "data" in data["ocs"]:
        return data["ocs"]["data"]  # fallback if server wraps response
    return data  # expected: list of stacks; each stack includes "cards"

def fmt_user(u: Dict[str, Any] | None) -> str:
    if not u:
        return ""
    return u.get("displayname") or u.get("primaryKey") or ""

def build_grouped_model(stacks: List[Dict[str, Any]], include_archived: bool) -> List[Dict[str, Any]]:
    grouped: List[Dict[str, Any]] = []
    for s in sorted(stacks, key=lambda x: x.get("order", 0)):
        cards = (s.get("cards") or [])
        if not include_archived:
            cards = [c for c in cards if not c.get("archived", False)]
        grouped.append({
            "stack": {
                "id": s.get("id"),
                "title": s.get("title"),
                "order": s.get("order"),
            },
            "cards": [
                {
                    "id": c.get("id"),
                    "title": c.get("title"),
                    "order": c.get("order"),
                    "archived": c.get("archived", False),
                    "duedate": parse_duedate(c.get("duedate")),
                    "owner": fmt_user(c.get("owner")) or None,
                    "assignees": [fmt_user(u) for u in (c.get("assignedUsers") or []) if fmt_user(u)],
                    "labels": [
                        {
                            "title": lb.get("title", ""),
                            "color": normalize_hex_color(lb.get("color")),
                        }
                        for lb in (c.get("labels") or [])
                        if lb.get("title")
                    ],
                }
                for c in sorted(cards, key=lambda x: x.get("order", 0))
            ]
        })
    return grouped

def parse_duedate(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = dateparser.parse(raw)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

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



# ---------- Output Formatters ----------

class Ansi:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"

def normalize_hex_color(c: Optional[str]) -> Optional[str]:
    """
    Normalize Deck's label hex color string to 6-char lowercase RGB.
    Handles:
      - "31CC7C" / "92e314"
      - with leading '#' (stripped)
      - 8-char ARGB/RGBA -> drop the first 2 bytes (alpha)
    """
    if not c:
        return None
    s = c.strip().lstrip("#")
    if len(s) == 3:  # short rgb, e.g. "abc"
        s = "".join(ch * 2 for ch in s)
    elif len(s) == 8:  # argb or rgba, drop alpha
        s = s[2:]
    if len(s) != 6:
        return None
    try:
        int(s, 16)
    except ValueError:
        return None
    return s.lower()

def hex_to_rgb(s: str) -> tuple[int, int, int]:
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)

def ansi_label(label: Dict[str, Any]) -> str:
    """Render a label title with 24-bit ANSI color if available."""
    title = label.get("title") or ""
    hexcol = label.get("color")
    if not hexcol:
        return title
    r, g, b = hex_to_rgb(hexcol)
    return f"\033[38;2;{r};{g};{b}m{title}{Ansi.RESET}"


def colorize_output(grouped: List[Dict[str, Any]], show_owner: bool, datefmt: str) -> str:
    E_STACK, E_CARD, E_LABEL, E_OWNER, E_ASSIGNEES, E_DUE, E_ARCH, E_TODO, E_DONE = "🗂", "📝", "🏷", "👤", "👥", "📅", "📦", "✔ ", "✅"
    lines: List[str] = []
    for block in grouped:
        st = block["stack"]
        lines.append(f"\n{Ansi.BOLD}{Ansi.BLUE}{E_STACK} {st.get('title') or f'List {st.get('id')}'}{Ansi.RESET}")
        cards = block["cards"]
        if not cards:
            lines.append(f"{Ansi.DIM}(no cards){Ansi.RESET}")
            continue
        for c in cards:
            if st.get("title").lower() == "todo" or st.get("title").lower() == "to do":
                icon = E_TODO
            elif st.get("title").lower() == "done":
                icon = E_DONE
            else:
                icon = E_CARD
            line = f"- {Ansi.BOLD}{icon} {c['title'] or '(untitled)'}{Ansi.RESET}"
            if c.get("labels"):
                rendered_labels = ", ".join(ansi_label(lb) for lb in c["labels"])
                line += f"  {E_LABEL}  {rendered_labels}"
            if show_owner and c.get("owner"):
                line += f"  {Ansi.CYAN}{E_OWNER} {c['owner']}{Ansi.RESET}"
            if c.get("assignees"):
                line += f"  {Ansi.CYAN}{E_ASSIGNEES} {', '.join(c['assignees'])}{Ansi.RESET}"
            if c.get("duedate"):
                line += f"  {Ansi.YELLOW}{E_DUE} {format_duedate(c['duedate'], datefmt)}{Ansi.RESET}"
            if c.get("archived"):
                line += f"  {Ansi.DIM}{E_ARCH} archived{Ansi.RESET}"
            lines.append(line)
    return "\n".join(lines).lstrip("\n")

def markdown_output(grouped: List[Dict[str, Any]], show_owner: bool, datefmt: str) -> str:
    lines: List[str] = []
    for block in grouped:
        st = block["stack"]
        lines.append(f"## {st.get('title') or f'List {st.get('id')}'}")
        cards = block["cards"]
        if not cards:
            lines.append("_(no cards)_")
            continue
        for c in cards:
            line = f"- **{c['title'] or '(untitled)'}**"
            meta = []
            if c.get("labels"):
                label_chunks = []
                for lb in c["labels"]:
                    title = lb.get("title") or ""
                    color = lb.get("color")
                    if color:
                        label_chunks.append(f"<span style=\"color:#{color}\">{title}</span>")
                    else:
                        label_chunks.append(title)
                meta.append(f"labels: {', '.join(label_chunks)}")
            if show_owner and c.get("owner"):
                meta.append(f"owner: {c['owner']}")
            if c.get("assignees"):
                meta.append(f"assignees: {', '.join(c['assignees'])}")
            if c.get("duedate"):
                meta.append(f"due: {format_duedate(c['duedate'], datefmt)}")
            if c.get("archived"):
                meta.append("archived")
            if meta:
                line += f" — _{'; '.join(meta)}_"
            lines.append(line)
        lines.append("")
    return "\n".join(lines).strip()

def pango_escape(text: str) -> str:
    return html.escape(text, quote=True)

def pango_label(label: Dict[str, Any]) -> str:
    title = pango_escape(label.get("title") or "")
    color = label.get("color")
    if color:
        return f"<span foreground='#{color}'>{title}</span>"
    else:
        return title

def pango_output(grouped: List[Dict[str, Any]], show_owner: bool, datefmt: str) -> str:
    lines: List[str] = []
    for block in grouped:
        st = block["stack"]
        title = pango_escape(st.get("title") or f"List {st.get('id')}")
        lines.append(f"<b><u>{title}</u></b>")
        cards = block["cards"]
        if not cards:
            lines.append("<span foreground='#6e7781'>(no cards)</span>")
            continue
        for c in cards:
            t = pango_escape(c["title"] or "(untitled)")
            if st.get("title").lower() == "todo" or st.get("title").lower() == "to do":
                line = f"✔  {t}"
            elif st.get("title").lower() == "done":
                line = f"✅ <span foreground='#888888'>{t}</span>"
            else:
                line = f"📝 {t}"
            meta = []
            if c.get("labels"):
                meta.append("🏷 " + ", ".join(pango_label(lb) for lb in c["labels"]))
            if show_owner and c.get("owner"):
                meta.append(f"<span foreground='#58a6ff'>👤 {pango_escape(c['owner'])}</span>")
            if c.get("assignees"):
                meta.append(f"<span foreground='#58a6ff'>👥 {pango_escape(', '.join(c['assignees']))}</span>")
            if c.get("duedate"):
                meta.append(f"<span foreground='#d29922'>📅 {pango_escape(format_duedate(c['duedate'], datefmt))}</span>")
            if c.get("archived"):
                meta.append("<span foreground='#6e7781'>📦 archived</span>")
            if meta:
                line += "  " + "  ".join(meta)
            lines.append(line)
        lines.append(" ")
    return "\n".join(lines)

def plain_output(grouped: List[Dict[str, Any]], show_owner: bool, datefmt: str) -> str:
    lines: List[str] = []
    for block in grouped:
        st = block["stack"]
        lines.append(f"\n=== {st.get('title') or f'List {st.get('id')}'} ===")
        cards = block["cards"]
        if not cards:
            lines.append("(no cards)")
            continue
        for c in cards:
            line = f"- {c['title'] or '(untitled)'}"
            bits = []
            if c.get("labels"):
                bits.append(f"labels: {', '.join(c['labels'])}")
            if show_owner and c.get("owner"):
                bits.append(f"owner: {c['owner']}")
            if c.get("assignees"):
                bits.append(f"assignees: {', '.join(c['assignees'])}")
            if c.get("duedate"):
                bits.append(f"due: {format_duedate(c['duedate'], datefmt)}")
            if c.get("archived"):
                bits.append("archived")
            if bits:
                line += "  [" + "; ".join(bits) + "]"
            lines.append(line)
    return "\n".join(lines).lstrip("\n")

# ---------- Main ----------

def main():
    parser = argparse.ArgumentParser(description="List Nextcloud Deck cards from a board, grouped by lists (stacks).")
    parser.add_argument("--url", default=env("NEXTCLOUD_BASE_URL"), help="Base URL, e.g. https://cloud.example.com")
    parser.add_argument("-u", "--username", default=env("NEXTCLOUD_USERNAME"), help="Username")
    parser.add_argument("-p", "--password", default=env("NEXTCLOUD_PASSWORD"), help="App password")
    parser.add_argument("-b", "--board-id", type=int, default=int(env("NEXTCLOUD_BOARD_ID", "0") or 0), help="Board ID")
    parser.add_argument("--include-archived", action="store_true",
                        default=env("NEXTCLOUD_INCLUDE_ARCHIVED") == "1",
                        help="Include archived cards")

    # Output modes
    parser.add_argument("--json", action="store_true", help="Output grouped JSON dicts (always includes owner)")
    parser.add_argument("--color", action="store_true", help="ANSI-colored terminal output with emojis")
    parser.add_argument("--pango", action="store_true", help="Pango-markup text")
    parser.add_argument("--markdown", action="store_true", help="Markdown-formatted output")

    # Display options
    parser.add_argument("--show-owner", action="store_true", help="Show card owner (default off in non-JSON modes)")

    parser.add_argument("--date-format",choices=["iso","local","relative"],default="relative",
                        help="How to display due dates (default: relative)")


    args = parser.parse_args()
    missing = [name for name, ok in [
        ("base URL", bool(args.url)),
        ("username", bool(args.username)),
        ("app password", bool(args.password)),
        ("board id", bool(args.board_id)),
    ] if not ok]
    if missing:
        print("Missing: " + ", ".join(missing), file=sys.stderr)
        parser.print_help(sys.stderr)
        sys.exit(2)

    session = requests.Session()
    session.auth = (args.username, args.password)
    session.headers.update(HEADERS)

    try:
        stacks = fetch_stacks(session, args.url, args.board_id)
    except requests.RequestException as e:
        print(f"Error fetching stacks for board {args.board_id}: {e}", file=sys.stderr)
        sys.exit(1)

    grouped = build_grouped_model(stacks, include_archived=args.include_archived)

    # Output precedence: json > markdown > pango > color > plain
    if args.json:
        # datetime objects need to be ISO stringified
        def default(o): return o.isoformat() if isinstance(o,datetime) else o
        print(json.dumps({
            "board_id": args.board_id,
            "api_base": args.url.rstrip("/") + API_BASE_SUFFIX,
            "stacks": grouped,
        }, indent=2, ensure_ascii=False, default=default))
        return

    if args.markdown:
        print(markdown_output(grouped, args.show_owner, args.date_format))
        return

    if args.pango:
        print(pango_output(grouped, args.show_owner, args.date_format))
        return

    if args.color:
        print(colorize_output(grouped, args.show_owner, args.date_format))
        return

    print(plain_output(grouped, args.show_owner, args.date_format))

if __name__ == "__main__":
    main()

