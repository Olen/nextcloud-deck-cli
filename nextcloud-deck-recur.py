#!/usr/bin/env python3
"""
CLI entrypoint for label-driven Deck card recurrence.

Reads config from CLI flags or matching env vars, then delegates to
`deck_recur.run()`.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Make the sibling module importable when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from deck_recur import DEFAULT_LABEL_PREFIX, Config, run  # noqa: E402
from olen.config import APP_CONFIG
from olen.const import ATTR_APP, ATTR_LOG
from olen.log import get_logger
from olen.remote_log import RemoteLogger


def env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def main() -> int:
    p = argparse.ArgumentParser(
        description="Return recurring Deck cards from Done to Todo."
    )
    p.add_argument("--url", default=env("NEXTCLOUD_BASE_URL"), help="Nextcloud base URL")
    p.add_argument("-u", "--username", default=env("NEXTCLOUD_USERNAME"))
    p.add_argument("-p", "--password", default=env("NEXTCLOUD_PASSWORD"))
    p.add_argument("-b", "--board-id", type=int,
                   default=int(env("NEXTCLOUD_BOARD_ID", "0") or 0))
    p.add_argument("--source-name", default=env("DONE_STACK_NAME", "Done"),
                   help="Stack recurring cards are taken from")
    p.add_argument("--target-name", default=env("TODO_STACK_NAME", "Todo"),
                   help="Stack recurring cards are returned to")
    p.add_argument("--label-prefix",
                   default=env("RECUR_LABEL_PREFIX", DEFAULT_LABEL_PREFIX),
                   help="Title prefix marking a label as a recurrence schedule")
    p.add_argument("--date", default=env("RECUR_DATE"),
                   help="Evaluate this date (YYYY-MM-DD) instead of today. "
                        "For testing, and for replaying a missed day.")
    p.add_argument("--dry-run", action="store_true",
                   help="Plan everything; perform no Deck mutations.")
    p.add_argument("-v", "--verbose", action="store_true")

    args = p.parse_args()

    APP_CONFIG.set(ATTR_APP, "name", "deck-recur")
    APP_CONFIG.set(ATTR_APP, "icon", "🔁")
    APP_CONFIG.set(ATTR_LOG, "log_path", os.path.expanduser("~/bin/logs/"))

    app_log = get_logger()
    app_log.silent = True
    app_log.set_level(app_log.DEBUG if args.verbose else app_log.INFO)
    app_log.start_logger(APP_CONFIG)

    # Bridge the stdlib `logging` calls from deck_recur into a useful console
    # format. The olen logger handles its own file output.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    remote_logger = RemoteLogger(APP_CONFIG)

    missing = [
        name for name, ok in (
            ("--url", bool(args.url)),
            ("--username", bool(args.username)),
            ("--password", bool(args.password)),
            ("--board-id", bool(args.board_id)),
        ) if not ok
    ]
    if missing:
        print(f"Missing required argument(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    on_date = None
    if args.date:
        try:
            on_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"--date must be YYYY-MM-DD, got {args.date!r}", file=sys.stderr)
            return 2

    cfg = Config(
        nc_url=args.url,
        nc_username=args.username,
        nc_password=args.password,
        nc_board_id=args.board_id,
        source_stack_name=args.source_name,
        target_stack_name=args.target_name,
        label_prefix=args.label_prefix,
        on_date=on_date,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )
    rc = run(cfg)
    if rc != 0:
        try:
            if rc == 2:
                # Per-card failures during an otherwise-completed run.
                remote_logger.discord.warning(
                    title="deck-recur completed with per-card failures",
                    message=f"Exit code {rc}. See logs at ~/bin/logs/deck-recur.log",
                    app="deck-recur",
                    icon="🔁",
                )
            else:
                remote_logger.discord.error(
                    title="deck-recur failed",
                    message=f"Exit code {rc}. See logs at ~/bin/logs/deck-recur.log",
                    app="deck-recur",
                    icon="🔁",
                )
        except Exception as e:
            print(f"Failed to send Discord alert: {e}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
