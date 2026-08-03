# IMAP ↔ Nextcloud Deck sync — design

**Status:** Approved 2026-05-11
**Author:** olen (brainstormed with Claude)
**Implements:** `nextcloud-deck-imap-sync.py`

## Goal

Treat IMAP "starred" (`\Flagged`) messages as a todo inbox: starring a message in
any mail client adds a card to the user's Nextcloud Deck Todo list; unstarring
moves the corresponding card to the Done stack; manually moving a card to the
Done stack clears the star on the source message.

## Source of starred messages

The mail server (Dovecot, running on the same host) exposes a virtual mailbox
**`_Virtual/Important`** that aggregates every message across every folder with
`\Flagged` set:

```
namespace virtual {
  prefix = _Virtual/
  separator = /
  location = virtual:~/.dovecot/virtual
}
```

```
$ cat ~/.dovecot/virtual/Important/dovecot-virtual
*
-Archive
-Archive/*
-Sent
-Sent/*
-Spam
-Spam/*
-badmail
-badmail/*
  FLAGGED
```

The Dovecot `virtual` backend transparently propagates flag-clearing back to the
underlying physical message — clearing `\Flagged` on a UID accessed via
`_Virtual/Important` updates the real message in its real folder, and the
virtual view stops showing it on the next FETCH. This makes the virtual folder
a single source of truth for both reads (which messages are starred) and writes
(clear the star).

The folder name is configurable so the same tool can run against any
IMAP server that exposes an equivalent virtual flagged-folder.

## Target Deck board

Existing Todo board (default board id 4 — same as `todo-desktop.sh`), reusing
the existing `Todo` / `Doing` / `Done` stacks. Email-derived cards live
alongside cards added manually via `nextcloud-deck-todo.py`; the two are
distinguished by a marker in the card description (see "Card identity" below).

## Architecture

A single new script in this repo:

```
nextcloud-deck-cli/
├── nextcloud_deck_core.py        (existing, unused by other scripts — older local copy)
├── nextcloud-deck-todo.py        (existing, unchanged)
├── nextcloud-deck-list.py        (existing, unchanged)
└── nextcloud-deck-imap-sync.py   (NEW)
```

No changes to any existing file. The new script imports `DeckClient` from the
installed `olen_deck` package (same as `nextcloud-deck-todo.py`); that
package's `create_card(stack_id, title, description="", ...)` already accepts
a `description` argument, so we can write the marker without modifying any
shared library. (The repo's local `nextcloud_deck_core.py` is older / unused
— nothing imports it.)

The new script depends on:

- `olen_deck.DeckClient` (already installed)
- `olen` logger + `RemoteLogger` for Discord/IRC alerts (per `/home/olen/CLAUDE.md`)
- `imap-tools` (new dependency, `pip install --user imap-tools`) — chosen over
  raw `imaplib` because it parses `From`, `Subject`, and `Message-ID` directly
  with proper RFC 2047 decoding, saving ~150 lines of boilerplate

## Email label

Every card managed by this sync is also tagged with a Deck label named
**`Email`** (default colour grey). This makes it trivial to filter for
"all email-originated tasks I've ever processed" in the Deck UI, and visually
distinguishes managed cards from manually-added ones at a glance.

Behaviour:

- On each run, the sync looks up the `Email` label on the board. If it does
  not exist, the sync creates it (`DeckClient.create_label(board_id, title="Email", color="808080")`).
- Newly created cards get the label applied immediately after creation.
- Existing managed cards (any card carrying the imap-sync marker) that lack
  the label get retro-tagged on the next run. This self-heals if the sync
  was upgraded from a pre-label version.
- Cards keep the label regardless of which stack they live in (the label
  reflects origin, not status — Done cards stay tagged).
- The label name is fixed to `Email`; the sync does not look up other labels
  on the board.

## Card identity (linking a Deck card back to an IMAP message)

Each managed card carries a marker somewhere in its description:

```
<!-- imap-sync: message-id=<CA+abc123@mail.example.com> -->
```

The script writes it on the first line of an otherwise-empty description on
create, but the matcher is not line-anchored — users may freely edit the
description above or below the marker without breaking the link.

- HTML-comment form so it renders invisibly in Deck's markdown view.
- The sync only ever touches cards whose description matches the marker regex
  `<!-- imap-sync: message-id=(.+?) -->`. Cards without the marker are
  "manual" (created by `nextcloud-deck-todo.py` or by hand) and are invisible
  to the sync — they will never be moved or deleted.
- Message-ID is the RFC 5322 value verbatim (with angle brackets). Globally
  unique, stable across folder moves, survives manual title edits.
- The card title is set on create to `"{from_name or from_addr}: {subject}"`,
  truncated to 200 chars. The title is never rewritten on later runs — if the
  user edits it, the edit sticks; matching is on the marker, not the title.

## Reconciliation algorithm

The script does one full sync per invocation. Each run is idempotent: any
missed run is caught up by the next.

```
1. IMAP login, SELECT "_Virtual/Important", FETCH headers for all messages.
   Build: starred = { message_id → (uid, from_name, from_addr, subject) }

2. deck.fetch_stacks() — locate Todo, Doing, Done stacks by configured name.
   Look up the "Email" label on the board, creating it if absent.
   Scan EVERY stack on the board (not just the three named ones) for cards
   carrying the imap-sync marker; parse the Message-ID out of each, and
   capture each card's current label IDs.
   Build: managed = { message_id → (card, current_stack, current_label_ids) }
   Cards without the marker are dropped from `managed` entirely.

   Rationale: scanning every stack means a user who moves an email-card to a
   custom stack (e.g. "Later", "Waiting") is still seen by the sync — so we
   won't re-create a duplicate card in Todo on the next run. Passes A and B
   only *act* on cards currently in Done / Todo / Doing respectively; cards
   tracked in other stacks are intentional no-ops.

3. Pass A — Deck → IMAP. Clear flags for cards user moved to Done:
     for msgid, (card, stack) in managed.items():
       if stack is done and msgid in starred:
         imap.uid_store(starred[msgid].uid, '-FLAGS', '\\Flagged')
         del starred[msgid]   # message no longer in the virtual view

4. Pass B — IMAP → Deck. Move cards to Done for messages user unstarred:
     for msgid, (card, stack) in managed.items():
       if stack in (todo, doing) and msgid not in starred:
         deck.move_card(card, done.id)

5. Pass C — IMAP → Deck. Create cards for newly-starred messages:
     for msgid, msg in starred.items():
       if msgid not in managed:
         title = f"{msg.from_name or msg.from_addr}: {msg.subject}"[:200]
         body  = f"<!-- imap-sync: message-id={msgid} -->\n"
         new_card = deck.create_card(todo.id, title, description=body)
         deck.assign_label(todo.id, new_card.id, email_label.id)

6. Pass D — Deck → Deck. Retro-tag managed cards missing the Email label:
     for msgid, mc in managed.items():
       if email_label.id not in mc.current_label_ids:
         deck.assign_label(mc.stack_id, mc.card.id, email_label.id)
```

**Pass ordering is load-bearing.** Pass A must run before Pass C: if the user
moved a card to Done within the same tick, the message is still in the virtual
folder until we clear its flag in A; without A first, C would see it as a
"newly starred" message and immediately re-create the card.

Pass B is independent of A and C and could run in any order, but doing it
between A and C keeps the flow easy to read: "first reconcile Done → IMAP,
then Active → Done, then create new cards."

## Configuration

CLI flags (with env-var fallbacks):

| Flag | Env | Default |
|---|---|---|
| `--url` | `NEXTCLOUD_BASE_URL` | — |
| `-u/--username` | `NEXTCLOUD_USERNAME` | — |
| `-p/--password` | `NEXTCLOUD_PASSWORD` | — |
| `-b/--board-id` | `NEXTCLOUD_BOARD_ID` | — |
| `--todo-name` | `TODO_STACK_NAME` | `Todo` |
| `--doing-name` | `DOING_STACK_NAME` | `Doing` |
| `--done-name` | `DONE_STACK_NAME` | `Done` |
| `--imap-host` | `IMAP_HOST` | `localhost` |
| `--imap-port` | `IMAP_PORT` | `993` |
| `--imap-user` | `IMAP_USER` | — |
| `--imap-password` | `IMAP_PASSWORD` | — |
| `--imap-folder` | `IMAP_FOLDER` | `_Virtual/Important` |
| `--dry-run` | — | off |
| `--verbose` | — | off |

The deployment wrapper (chezmoi-managed) resolves both passwords from
1Password via `op`, matching the pattern in `todo-desktop.sh`.

## Deployment

A user-scope systemd `.timer` + `.service` pair, chezmoi-managed in
`etc/systemd/user/` (or the per-user equivalent). The service runs every
5 minutes:

```
[Unit]
Description=Sync IMAP starred messages to Nextcloud Deck

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
```

The `.service` invokes a small wrapper shell script that:
1. Resolves `HOME` if unset (per `/home/olen/CLAUDE.md` systemd conventions)
2. Fetches both passwords via `op`
3. Execs `nextcloud-deck-imap-sync.py` with the right flags

## Error handling and logging

- Use the `olen` library logger (`from olen.log import get_logger`) with:
  - app name: `imap-deck-sync`
  - icon: `⭐`
  - log path: `$HOME/bin/logs/`
- Run-level failures (IMAP login refused, board not found, Deck unreachable):
  log at ERROR, send one `error_discord` alert, exit non-zero.
- Per-card failures (one card's move fails, one star clear fails): log at
  WARN, continue with the remaining cards, summarise at the end. Send one
  `warning_discord` per run *only if* at least one per-card failure occurred —
  not one per failure.
- `--dry-run`: run the full FETCH and reconciliation maths, log every planned
  action, but skip all mutations (`uid_store`, `create_card`, `move_card`).
  Used for the first live test against the real board.

## Behaviour decisions (reference table)

| Case | Behaviour |
|---|---|
| Card in `Doing`, message unstarred | Move to `Done`. Todo and Doing are treated as equivalent "active" stacks. |
| Card in `Done`, message still starred | Unstar the message. |
| Card in `Done`, message already unstarred | No-op. |
| Card in `Todo`/`Doing`, message still starred | No-op. |
| Card without `imap-sync` marker | Untouched in any stack. |
| Same Message-ID appears twice in IMAP | Use first; log a WARN. Shouldn't happen normally. |
| Message-ID header missing from email | Skip the message with a WARN log; cannot link without it. |
| Card has marker but message no longer in IMAP at all | Treat same as "unstarred" → move to Done. (`del`'d, archived, or unstarred — all collapse to the same outcome.) |
| Card with marker in a non-standard stack (e.g. "Later") | Left in place. Not re-created. Not moved. The marker is still recognised so the sync won't duplicate the card. |
| Managed card without the `Email` label | Tagged with the `Email` label on next run (Pass D). |
| `Email` label missing from the board | Created on first run (grey, `#808080`). |
| Card moves Todo → Done | Keeps the `Email` label. Label reflects origin, not status. |
| Card title too long for Deck | Truncated at 200 chars on creation. Never re-truncated later. |
| Subject contains newlines / weird chars | Whitespace-collapsed to single spaces on creation. |
| Stack with configured name not found | Hard error, exit non-zero, Discord alert. |

## Testing

- **Unit tests** for the reconciliation function. Pure function: takes
  `(starred_dict, managed_dict, stack_ids)` and returns a list of planned
  actions (`UnstarAction`, `MoveToDoneAction`, `CreateCardAction`). Then
  drive the IMAP and Deck clients off that plan in a thin `execute(plan)`
  step. This split lets us assert on the plan without mocking IMAP or HTTP.
- **`--dry-run` against the real services** for the first smoke test:
  verifies header parsing, marker parsing, and that the plan looks right
  without touching anything.
- **Live test** with a single throwaway starred message. Verify create →
  move-to-Done unstars; star-then-move-to-Done unstars; manually-added card
  in Todo is left alone.
- No automated integration tests — production dependencies (live IMAP, live
  Deck) aren't easily containerised here, and the algorithm's idempotency
  makes "ran twice, second run is no-op" the practical regression check.

## Out of scope

- Real-time sync via IMAP IDLE. The poll-every-5-min model is enough; we can
  add IDLE later if latency bites.
- Syncing card *content* edits back to email (e.g. card description → email
  metadata). One-way semantic mapping only.
- Multi-account support in a single process. Run a second timer for a second
  account if needed.
- Migration of existing manual cards into managed ones. New cards from now on
  only.


> **Amended 2026-08-03:** the virtual store moved from `~/Maildir/virtual`
> to `~/.dovecot/virtual` (it sat inside the inbox namespace's `LAYOUT=fs`
> mail_location and was being enumerated as phantom mailboxes). Symlink
> aliases `Archive`/`Sent`/`Spam`/`badmail` are excluded so the same storage
> is not read more than once. Folder names are unchanged.
