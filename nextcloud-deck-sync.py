#!/usr/bin/env python3
"""
CLI entrypoint for the IMAP → Nextcloud Deck sync.

Reads config from CLI flags or matching env vars, then delegates to
`deck_sync.run()`.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Make the sibling module importable when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from deck_sync import (  # noqa: E402
    DEFAULT_LABEL_PREFIX,
    Config,
    run,
    validate_archive_days,
    validate_recur_label_prefix,
)
from olen.config import APP_CONFIG
from olen.const import ATTR_APP, ATTR_LOG
from olen.log import get_logger
from olen.remote_log import RemoteLogger


def env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def main() -> int:
    p = argparse.ArgumentParser(
        description="Sync IMAP starred messages to/from a Nextcloud Deck Todo board."
    )
    # Nextcloud
    p.add_argument("--url", default=env("NEXTCLOUD_BASE_URL"), help="Nextcloud base URL")
    p.add_argument("-u", "--username", default=env("NEXTCLOUD_USERNAME"))
    p.add_argument("-p", "--password", default=env("NEXTCLOUD_PASSWORD"))
    p.add_argument("-b", "--board-id", type=int,
                   default=int(env("NEXTCLOUD_BOARD_ID", "0") or 0))
    p.add_argument("--todo-name", default=env("TODO_STACK_NAME", "Todo"))
    p.add_argument("--doing-name", default=env("DOING_STACK_NAME", "Doing"))
    p.add_argument("--done-name", default=env("DONE_STACK_NAME", "Done"))
    p.add_argument("--email-label-name", default=env("EMAIL_LABEL_NAME", "Email"),
                   help="Name of the Deck label applied to email-derived cards")
    p.add_argument("--email-label-color", default=env("EMAIL_LABEL_COLOR", "808080"),
                   help="Hex color (no '#') for the Email label if it has to be created")

    # IMAP
    p.add_argument("--imap-host", default=env("IMAP_HOST", "localhost"))
    p.add_argument("--imap-port", type=int, default=int(env("IMAP_PORT", "993")))
    p.add_argument("--imap-user", default=env("IMAP_USER"))
    p.add_argument("--imap-password", default=env("IMAP_PASSWORD"))
    p.add_argument("--imap-folder", default=env("IMAP_FOLDER", "_Virtual/Important"))
    p.add_argument("--archive-done-after-days", type=int,
                   default=int(env("ARCHIVE_DONE_AFTER_DAYS", "7")),
                   help="Archive Email-labelled cards that have been in Done this "
                        "many days. 0 or less disables the pass.")
    p.add_argument("--recur-label-prefix",
                   default=env("RECUR_LABEL_PREFIX", DEFAULT_LABEL_PREFIX),
                   help="Title prefix marking a label as a recurrence schedule. "
                        "Cards carrying one are never archived and are never "
                        "moved back to Done by the unstarred-message pass. Must "
                        "match nextcloud-deck-recur.py's --label-prefix.")

    # Modes
    p.add_argument("--dry-run", action="store_true",
                   help="Plan everything; perform no IMAP or Deck mutations.")
    p.add_argument("--no-done-sync", action="store_false", dest="done_sync",
                   default=env("DONE_SYNC", "1").lower()
                   not in ("0", "false", "no", "off"),
                   help="Disable the done-tick <-> Done stack sync pass.")
    p.add_argument("-v", "--verbose", action="store_true")

    args = p.parse_args()

    APP_CONFIG.set(ATTR_APP, "name", "deck-sync")
    APP_CONFIG.set(ATTR_APP, "icon", "⭐")
    APP_CONFIG.set(ATTR_LOG, "log_path", os.path.expanduser("~/bin/logs/"))

    app_log = get_logger()
    app_log.silent = True
    app_log.set_level(app_log.DEBUG if args.verbose else app_log.INFO)
    app_log.start_logger(APP_CONFIG)

    # Bridge the stdlib `logging` calls from deck_sync into a useful
    # console format. The olen logger handles its own file output.
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
            ("--imap-user", bool(args.imap_user)),
            ("--imap-password", bool(args.imap_password)),
        ) if not ok
    ]
    if missing:
        print(f"Missing required argument(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    err = validate_archive_days(args.archive_done_after_days)
    if err:
        print(err, file=sys.stderr)
        return 2

    err = validate_recur_label_prefix(args.recur_label_prefix)
    if err:
        print(err, file=sys.stderr)
        return 2

    cfg = Config(
        nc_url=args.url,
        nc_username=args.username,
        nc_password=args.password,
        nc_board_id=args.board_id,
        todo_stack_name=args.todo_name,
        doing_stack_name=args.doing_name,
        done_stack_name=args.done_name,
        email_label_name=args.email_label_name,
        email_label_color=args.email_label_color,
        imap_host=args.imap_host,
        imap_port=args.imap_port,
        imap_user=args.imap_user,
        imap_password=args.imap_password,
        imap_folder=args.imap_folder,
        archive_done_after_days=args.archive_done_after_days,
        recur_label_prefix=args.recur_label_prefix,
        done_sync=args.done_sync,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )
    rc = run(cfg)
    if rc != 0:
        try:
            if rc == 2:
                # Per-card failures during an otherwise-completed run — warning, not error.
                remote_logger.discord.warning(
                    title="deck-sync completed with per-card failures",
                    message=f"Exit code {rc}. See logs at ~/bin/logs/deck-sync.log",
                    app="deck-sync",
                    icon="⭐",
                )
            else:
                # rc=1: setup/connection failure — escalate.
                remote_logger.discord.error(
                    title="deck-sync failed",
                    message=f"Exit code {rc}. See logs at ~/bin/logs/deck-sync.log",
                    app="deck-sync",
                    icon="⭐",
                )
        except Exception as e:
            print(f"Failed to send Discord alert: {e}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
