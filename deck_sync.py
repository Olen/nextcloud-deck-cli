"""
IMAP ↔ Nextcloud Deck sync.

Pure helpers + planner live near the top of this file; IMAP/Deck IO and the
orchestration `run()` function live below.

Spec: docs/superpowers/specs/2026-05-11-imap-deck-sync-design.md
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional

from deck_recur import DEFAULT_LABEL_PREFIX


# Nextcloud's activity_expire_days is 30 on this deployment. A threshold at or
# near that value means records expire before a card can qualify, so archiving
# would silently stop. Refuse thresholds that leave no headroom.
ARCHIVE_MAX_DAYS_LIMIT = 25

MARKER_RE = re.compile(r"<!--\s*imap-sync:\s*message-id=(\S.*?)\s*-->", re.DOTALL)


def validate_archive_days(days: int) -> str | None:
    """Return an error message if the threshold is unusable, else None."""
    if days >= ARCHIVE_MAX_DAYS_LIMIT:
        return (
            f"--archive-done-after-days must be below {ARCHIVE_MAX_DAYS_LIMIT} "
            f"(got {days}). Nextcloud keeps only 30 days of activity, so a "
            f"larger window would silently archive nothing."
        )
    return None


def validate_recur_label_prefix(prefix: str) -> str | None:
    """Return an error message if the recurrence prefix is unusable, else None."""
    if not (prefix or "").strip():
        return (
            "--recur-label-prefix must not be empty. An empty prefix matches "
            "every label title, which would exclude every card from the archive "
            "pass and from the move-to-Done pass."
        )
    return None


def _normalize_recur_prefix(prefix: Optional[str]) -> str:
    """Stripped, lower-cased recurrence prefix for title comparisons."""
    return (prefix or "").strip().lower()


def _has_recur_label(card, recur_prefix: str) -> bool:
    """
    True when `card` carries a label whose title starts with `recur_prefix`.

    `recur_prefix` must already be normalised by `_normalize_recur_prefix()`.

    An empty prefix matches *nothing* here. `"anything".startswith("")` is
    True, so the naive reading would treat every card as recurring and
    silently exclude the whole board from the passes that use this guard.
    A misconfigured prefix is rejected at startup by
    `validate_recur_label_prefix()`; this is the second line of defence, and
    it fails towards the ordinary email flow rather than towards paralysis.
    """
    if not recur_prefix:
        return False
    return any(
        ((getattr(label, "title", "") or "").strip().lower()).startswith(recur_prefix)
        for label in (getattr(card, "labels", None) or [])
    )


def build_marker(message_id: str) -> str:
    """Return the HTML-comment marker for a Message-ID."""
    return f"<!-- imap-sync: message-id={message_id} -->"


def parse_marker(description: Optional[str]) -> Optional[str]:
    """
    Extract the Message-ID from a card description, or return None if there
    is no marker.
    """
    if not description:
        return None
    m = MARKER_RE.search(description)
    return m.group(1) if m else None


TITLE_MAX_LEN = 200


def format_card_title(name: Optional[str], addr: str, subject: Optional[str]) -> str:
    """
    Build the Deck card title from an email's From and Subject.

    - Prefer the display name; fall back to the bare address.
    - Collapse runs of whitespace (incl. newlines/tabs) in the subject.
    - Truncate the final title to TITLE_MAX_LEN chars.
    """
    sender = (name or "").strip() or addr
    subj = " ".join((subject or "").split())
    title = f"{sender}: {subj}"
    return title[:TITLE_MAX_LEN]


@dataclass(frozen=True)
class StarredMessage:
    """One starred message as seen in the IMAP virtual folder."""
    message_id: str
    uid: str
    from_name: Optional[str]
    from_addr: str
    subject: Optional[str]


@dataclass(frozen=True)
class ManagedCard:
    """A Deck card carrying our imap-sync marker."""
    message_id: str
    card: Any           # ncdeck.Card — kept opaque so the planner is library-agnostic
    stack_id: int
    label_ids: frozenset[int] = frozenset()


@dataclass(frozen=True)
class StackIds:
    """Resolved IDs of the three named stacks for this run."""
    todo: int
    doing: int
    done: int


@dataclass(frozen=True)
class UnstarAction:
    """Clear the \\Flagged flag on an IMAP message."""
    uid: str
    message_id: str


@dataclass(frozen=True)
class MoveToDoneAction:
    """Move a Deck card to the Done stack."""
    card: Any           # ncdeck.Card
    target_stack_id: int


@dataclass(frozen=True)
class CreateCardAction:
    """Create a new Deck card in the Todo stack. Labels are applied after creation."""
    stack_id: int
    title: str
    description: str
    message_id: str
    label_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class AssignLabelAction:
    """Attach an existing Deck label to an existing card."""
    stack_id: int
    card_id: int
    label_id: int


@dataclass(frozen=True)
class ArchiveAction:
    """Archive a Deck card that has aged out of the Done stack."""
    stack_id: int
    card_id: int
    card_title: str
    done_at: int


@dataclass(frozen=True)
class TickDoneAction:
    """Set Deck's done-tick on a card that is sitting in the Done stack."""
    card: Any           # ncdeck.Card
    card_title: str
    done_at: str        # ISO 8601 with offset, e.g. 2026-08-04T15:30:00+00:00


@dataclass(frozen=True)
class MoveToDoneByTickAction:
    """Move a card into Done because its done-tick was set by hand."""
    card: Any           # ncdeck.Card
    card_title: str
    target_stack_id: int


Action = (
    UnstarAction | MoveToDoneAction | CreateCardAction
    | AssignLabelAction | ArchiveAction
    | TickDoneAction | MoveToDoneByTickAction
)

log = logging.getLogger(__name__)


def make_plan(
    starred: dict[str, StarredMessage],
    managed: dict[str, ManagedCard],
    stack_ids: StackIds,
    email_label_id: int,
    recur_label_prefix: str = DEFAULT_LABEL_PREFIX,
) -> list[Action]:
    """
    Pure reconciliation function. Inputs are the current state; output is a
    list of mutations to apply.

    Ordering invariant: UnstarActions must precede any CreateCardActions for
    the same Message-ID. We achieve this by tracking message_ids that have
    been handled in Pass A and skipping them in Pass C.

    Pass B exempts cards carrying a recurrence label (title starting with
    `recur_label_prefix`), the same exemption `plan_archive` applies.

    Pass D: retro-tags any managed card missing the Email label, regardless of
    which stack the card lives in.

    See spec section "Reconciliation algorithm" for the full rationale.
    """
    actions: list[Action] = []
    handled: set[str] = set()
    recur_prefix = _normalize_recur_prefix(recur_label_prefix)

    # Pass A: cards in Done where the message is still starred → unstar.
    for msgid, mc in managed.items():
        if mc.stack_id == stack_ids.done and msgid in starred:
            actions.append(UnstarAction(uid=starred[msgid].uid, message_id=msgid))
            handled.add(msgid)

    # Pass B: cards in Todo/Doing where the message is no longer starred → move to Done.
    #
    # Recurring cards are exempt. A card born from a starred email keeps its
    # imap-sync marker, so it stays in `managed` forever; once the user adds a
    # recurrence label and completes it, Pass A unstars the message for good.
    # Without this exemption, the recurrence job's move back into Todo would be
    # undone by the next 5-minute sync — the chore would flash into view and
    # vanish. plan_archive exempts the same cards for the same reason.
    for msgid, mc in managed.items():
        if mc.stack_id not in (stack_ids.todo, stack_ids.doing) or msgid in starred:
            continue
        if _has_recur_label(mc.card, recur_prefix):
            continue
        actions.append(MoveToDoneAction(card=mc.card, target_stack_id=stack_ids.done))

    # Pass C: messages newly starred (no matching managed card) → create in Todo.
    # Cards in custom stacks (e.g. "Later") DO appear in `managed` so we skip them here.
    for msgid, msg in starred.items():
        if msgid in handled:
            continue
        if msgid in managed:
            continue
        actions.append(
            CreateCardAction(
                stack_id=stack_ids.todo,
                title=format_card_title(msg.from_name, msg.from_addr, msg.subject),
                description=f"{build_marker(msgid)}\n",
                message_id=msgid,
                label_ids=(email_label_id,),
            )
        )

    # Pass D: retro-tag managed cards that don't yet carry the Email label.
    # Operates on ALL managed cards regardless of stack — origin labels persist
    # even when a card is in Done or a custom stack.
    for msgid, mc in managed.items():
        if email_label_id not in mc.label_ids:
            actions.append(
                AssignLabelAction(
                    stack_id=mc.stack_id,
                    card_id=mc.card.id,
                    label_id=email_label_id,
                )
            )

    return actions


def _parse_activity_datetime(value) -> Optional[int]:
    """ISO-8601 (usually offset-aware UTC) -> epoch seconds, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def latest_done_at(entries, board_id: int, done_stack_id: int) -> dict[int, int]:
    """
    Map card id -> epoch seconds of the card's most recent move into the Done
    stack, from Nextcloud activity entries.

    `entries` must be newest-first (as the API returns them), so the first
    match for a card wins. That is what makes a card moved Todo->Done->Todo->Done
    resolve to its latest arrival.

    A move is identified by the presence of `stackBefore` in the rich-subject
    parameters. Never string-match `subject`: "moved" also matches "removed",
    and the text is localised.
    """
    out: dict[int, int] = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("object_type") != "deck_card":
            continue

        rich = entry.get("subject_rich") or []
        params = rich[1] if len(rich) > 1 and isinstance(rich[1], dict) else {}
        if "stackBefore" not in params:
            continue

        board = params.get("board") or {}
        stack = params.get("stack") or {}
        if str(board.get("id")) != str(board_id):
            continue
        if str(stack.get("id")) != str(done_stack_id):
            continue

        card_id = entry.get("object_id")
        if card_id is None:
            continue
        try:
            card_id = int(card_id)
        except (TypeError, ValueError):
            continue
        if card_id in out:
            continue

        ts = _parse_activity_datetime(entry.get("datetime"))
        if ts is None:
            continue
        out[card_id] = ts

    return out


def plan_archive(done_cards, done_at: dict[int, int], email_label_id: int,
                 now: int, max_age_days: int,
                 recur_label_prefix: str = DEFAULT_LABEL_PREFIX) -> list[ArchiveAction]:
    """
    Pure planner for the archive pass.

    A card is archived when all of the following hold:
      1. it is in the Done stack (the caller passes only those)
      2. it carries `email_label_id`
      3. `done_at` has a record for it — otherwise skip silently, the user
         archives those by hand
      4. it has been in Done for at least `max_age_days`
      5. it carries no recurrence label — a card that recurs must not be
         archived out from under the recurrence job

    `max_age_days <= 0` disables the pass entirely.
    """
    if max_age_days <= 0:
        return []

    cutoff_seconds = max_age_days * 86400
    recur_prefix = _normalize_recur_prefix(recur_label_prefix)
    actions: list[ArchiveAction] = []

    for card in done_cards or []:
        if _has_recur_label(card, recur_prefix):
            continue

        label_ids = {
            getattr(label, "id", None)
            for label in (getattr(card, "labels", None) or [])
        }
        if email_label_id not in label_ids:
            continue

        done_ts = done_at.get(card.id)
        if done_ts is None:
            continue

        if now - done_ts < cutoff_seconds:
            continue

        actions.append(
            ArchiveAction(
                stack_id=card.stack_id,
                card_id=card.id,
                card_title=getattr(card, "title", "") or "",
                done_at=done_ts,
            )
        )

    return actions


def plan_done_sync(done_cards, open_cards, done_stack_id: int,
                   now_iso: str, skip_card_ids=()) -> list:
    """
    Pure planner keeping Deck's per-card `done` field and membership of the
    Done stack in agreement.

    Stack membership wins inside Done; the tick wins outside it:
      - a card in Done with no `done` value is ticked
      - a card in Todo or Doing with a `done` value is moved into Done

    This cannot oscillate. After one pass every card is either in Done and
    ticked, or outside Done and unticked, and neither clause then matches.

    The cost is that un-ticking a card while leaving it in Done is not
    expressible: the next run re-ticks it. Un-ticking means dragging the card
    out of Done.

    Archived and trashed cards are skipped in both directions. Deck's update
    endpoint hardcodes `deletedAt` to 0, so touching a trashed card would
    restore it from the trash.

    `skip_card_ids` are cards another pass in the same plan already moves into
    Done — an unstarred, hand-ticked card is both a Pass B `MoveToDoneAction`
    and a `MoveToDoneByTickAction`, which would be two identical PUTs and two
    activity entries for one logical move. The caller owns that set because
    only it sees the whole plan.
    """
    actions: list = []
    skip = set(skip_card_ids or ())

    for card in done_cards or []:
        if getattr(card, "archived", False) or getattr(card, "deleted_at", 0):
            continue
        if getattr(card, "id", None) in skip:
            continue
        if getattr(card, "done", None) is None:
            actions.append(
                TickDoneAction(
                    card=card,
                    card_title=getattr(card, "title", "") or "",
                    done_at=now_iso,
                )
            )

    for card in open_cards or []:
        if getattr(card, "archived", False) or getattr(card, "deleted_at", 0):
            continue
        if getattr(card, "id", None) in skip:
            continue
        if getattr(card, "done", None) is not None:
            actions.append(
                MoveToDoneByTickAction(
                    card=card,
                    card_title=getattr(card, "title", "") or "",
                    target_stack_id=done_stack_id,
                )
            )

    return actions


def _flagged_flag():
    # Import inside the function so this module stays importable without imap_tools
    # (e.g. in environments that only run the pure-logic tests).
    from imap_tools import MailMessageFlags
    return MailMessageFlags.FLAGGED


@dataclass
class ExecutionSummary:
    """Counts of actions actually applied during execute_plan."""
    created: int = 0
    moved: int = 0
    unstarred: int = 0
    labels_assigned: int = 0
    archived: int = 0
    ticked: int = 0
    moved_by_tick: int = 0
    failures: int = 0


def execute_plan(plan, mailbox, deck, dry_run: bool = False) -> ExecutionSummary:
    """
    Apply a plan from make_plan() against the IMAP mailbox and Deck client.

    Per-action failures are logged and counted in `summary.failures`; the rest
    of the plan still runs (idempotency means the next run will pick up the
    pieces).

    For CreateCardAction, the card is created first, then each requested label
    is applied with a separate `assign_label` call. A failure during the label
    step counts as a failure (one per failed label) without rolling back the
    card creation.

    `mailbox` must duck-type as imap_tools.MailBox (we call
    `.flag(uids, {FLAGGED}, False)`).
    `deck` must duck-type as ncdeck.DeckClient (we call `.create_card(...)`,
    `.move_card(...)`, `.assign_label(...)`, `.archive_card(...)`, and
    `.patch_card(...)`).
    """
    summary = ExecutionSummary()

    for action in plan:
        try:
            if isinstance(action, UnstarAction):
                if dry_run:
                    log.info("[dry-run] would clear \\Flagged on uid=%s (message_id=%s)",
                             action.uid, action.message_id)
                else:
                    log.info("Clearing \\Flagged on uid=%s (message_id=%s)",
                             action.uid, action.message_id)
                    mailbox.flag(action.uid, {_flagged_flag()}, False)
                summary.unstarred += 1

            elif isinstance(action, MoveToDoneAction):
                if dry_run:
                    log.info("[dry-run] would move card #%s to stack #%s",
                             action.card.id, action.target_stack_id)
                else:
                    log.info("Moving card #%s to stack #%s",
                             action.card.id, action.target_stack_id)
                    deck.move_card(action.card, action.target_stack_id)
                summary.moved += 1

            elif isinstance(action, CreateCardAction):
                if dry_run:
                    log.info("[dry-run] would create card in stack #%s: %r (labels=%s)",
                             action.stack_id, action.title, action.label_ids)
                    summary.created += 1
                    # Count label assignments that WOULD happen for dry-run accounting.
                    summary.labels_assigned += len(action.label_ids)
                else:
                    log.info("Creating card in stack #%s: %r", action.stack_id, action.title)
                    new_card = deck.create_card(
                        stack_id=action.stack_id,
                        title=action.title,
                        description=action.description,
                    )
                    summary.created += 1
                    # Apply each label. Per-label failures are recorded but don't undo the create.
                    for label_id in action.label_ids:
                        try:
                            deck.assign_label(
                                stack_id=action.stack_id,
                                card_id=new_card.id,
                                label_id=label_id,
                            )
                            summary.labels_assigned += 1
                        except Exception as e:
                            log.warning(
                                "Failed to assign label %s to new card #%s: %s",
                                label_id, new_card.id, e,
                            )
                            summary.failures += 1

            elif isinstance(action, AssignLabelAction):
                if dry_run:
                    log.info("[dry-run] would assign label %s to card #%s in stack #%s",
                             action.label_id, action.card_id, action.stack_id)
                else:
                    log.info("Assigning label %s to card #%s in stack #%s",
                             action.label_id, action.card_id, action.stack_id)
                    deck.assign_label(
                        stack_id=action.stack_id,
                        card_id=action.card_id,
                        label_id=action.label_id,
                    )
                summary.labels_assigned += 1

            elif isinstance(action, ArchiveAction):
                if dry_run:
                    log.info("[dry-run] would archive card #%s %r (in Done since %s)",
                             action.card_id, action.card_title, action.done_at)
                else:
                    log.info("Archiving card #%s %r (in Done since %s)",
                             action.card_id, action.card_title, action.done_at)
                    deck.archive_card(stack_id=action.stack_id, card_id=action.card_id)
                summary.archived += 1

            elif isinstance(action, TickDoneAction):
                if dry_run:
                    log.info("[dry-run] would tick card #%s %r as done at %s",
                             action.card.id, action.card_title, action.done_at)
                else:
                    log.info("Ticking card #%s %r as done at %s",
                             action.card.id, action.card_title, action.done_at)
                    deck.patch_card(action.card, done=action.done_at)
                summary.ticked += 1

            elif isinstance(action, MoveToDoneByTickAction):
                if dry_run:
                    log.info("[dry-run] would move ticked card #%s %r to stack #%s",
                             action.card.id, action.card_title, action.target_stack_id)
                else:
                    log.info("Moving ticked card #%s %r to stack #%s",
                             action.card.id, action.card_title, action.target_stack_id)
                    deck.move_card(action.card, action.target_stack_id, order=0)
                summary.moved_by_tick += 1

            else:
                log.warning("Unknown action type %r — skipping", type(action).__name__)
                summary.failures += 1

        except Exception as e:
            log.warning("Action %r failed: %s", action, e)
            summary.failures += 1

    return summary


def fetch_deck_activity(deck, page_size: int = 200, max_pages: int = 20) -> list[dict]:
    """
    Fetch the whole available Deck activity window, newest first.

    Paging back only as far as the archive threshold is not enough: a card with
    no entry inside that window is ambiguous (moved long ago vs. no record at
    all), and those two cases must behave differently. Retention is finite
    (30 days), so fetching everything available is bounded in practice;
    `max_pages` is only a runaway guard.
    """
    entries: list[dict] = []
    since = None

    for _ in range(max_pages):
        batch = deck.get_deck_activity(limit=page_size, since=since)
        if not batch:
            break
        entries.extend(batch)
        if len(batch) < page_size:
            break
        since = batch[-1].get("activity_id")
        if since is None:
            break

    return entries


def fetch_managed(stacks) -> dict[str, ManagedCard]:
    """
    Walk every stack on the board, parse each card's description, and return
    a {message_id: ManagedCard} mapping for cards that carry our marker.

    Cards without the marker are silently skipped. Cards whose Message-ID has
    already been seen log a WARN and are skipped (the first sighting wins).

    Also captures each card's current Deck label IDs so the planner can decide
    whether to retro-tag with the Email label.
    """
    managed: dict[str, ManagedCard] = {}
    for stack in stacks:
        for card in (getattr(stack, "cards", None) or []):
            msgid = parse_marker(getattr(card, "description", None))
            if not msgid:
                continue
            if msgid in managed:
                log.warning(
                    "Duplicate imap-sync marker for Message-ID %s "
                    "(card #%s in stack #%s vs. card #%s in stack #%s); keeping first",
                    msgid, managed[msgid].card.id, managed[msgid].stack_id,
                    card.id, stack.id,
                )
                continue
            label_ids = frozenset(
                lb.id for lb in (getattr(card, "labels", None) or [])
                if getattr(lb, "id", None) is not None
            )
            managed[msgid] = ManagedCard(
                message_id=msgid,
                card=card,
                stack_id=stack.id,
                label_ids=label_ids,
            )
    return managed


def _first_message_id(headers: dict) -> Optional[str]:
    """Return the first Message-ID header, or None if missing/empty."""
    values = headers.get("message-id") if headers else None
    if not values:
        return None
    first = values[0] if isinstance(values, (list, tuple)) else values
    return first or None


def fetch_starred(messages_iter) -> dict[str, StarredMessage]:
    """
    Build {message_id: StarredMessage} from an iterable of imap_tools MailMessage
    objects (or any duck-typed equivalent providing .uid, .from_values, .subject,
    .headers).

    The iterable is the result of `mailbox.fetch(...)`; this function does NOT
    open or close the connection.
    """
    starred: dict[str, StarredMessage] = {}
    for m in messages_iter:
        msgid = _first_message_id(getattr(m, "headers", None) or {})
        if not msgid:
            log.warning(
                "IMAP message uid=%s has no Message-ID header; skipping",
                getattr(m, "uid", "?"),
            )
            continue
        if msgid in starred:
            log.warning(
                "Duplicate Message-ID %s in IMAP folder (uid %s vs %s); keeping first",
                msgid, starred[msgid].uid, getattr(m, "uid", "?"),
            )
            continue
        fv = getattr(m, "from_values", None) or SimpleNamespace(name="", email="")
        starred[msgid] = StarredMessage(
            message_id=msgid,
            uid=str(getattr(m, "uid", "")),
            from_name=getattr(fv, "name", "") or None,
            from_addr=getattr(fv, "email", "") or "",
            subject=getattr(m, "subject", None),
        )
    return starred


@dataclass
class Config:
    nc_url: str
    nc_username: str
    nc_password: str
    nc_board_id: int
    todo_stack_name: str
    doing_stack_name: str
    done_stack_name: str
    email_label_name: str
    email_label_color: str
    imap_host: str
    imap_port: int
    imap_user: str
    imap_password: str
    imap_folder: str
    archive_done_after_days: int = 7
    # Must match nextcloud-deck-recur.py's --label-prefix: the archive pass and
    # Pass B use it to recognise (and leave alone) cards the recurrence job owns.
    recur_label_prefix: str = DEFAULT_LABEL_PREFIX
    done_sync: bool = True
    dry_run: bool = False
    verbose: bool = False


def _find_stack(stacks, name: str):
    target = (name or "").strip().lower()
    for s in stacks:
        if (getattr(s, "title", "") or "").strip().lower() == target:
            return s
    return None


def _find_or_create_email_label(deck, board_id: int, name: str, color: str):
    """
    Look up a label by exact title on the given board. If not present, create
    it. Returns the label (object exposing `.id`).
    """
    labels = deck.get_board_labels(board_id=board_id)
    for lb in labels:
        if (getattr(lb, "title", "") or "").strip() == name:
            return lb
    log.info("Creating Deck label %r (color #%s) on board %s", name, color, board_id)
    created = deck.create_label(board_id=board_id, title=name, color=color)
    if created is None:
        raise RuntimeError(f"create_label returned None for label {name!r}")
    return created


def run(config: Config) -> int:
    """
    Orchestrate one full sync. Returns a process exit code:
      0 — clean run
      1 — setup/connection error (Deck or IMAP couldn't be queried)
      2 — sync completed but at least one per-action failure occurred
    """
    from imap_tools import MailBox
    from ncdeck import DeckClient

    deck = DeckClient(
        config.nc_url,
        config.nc_username,
        config.nc_password,
        config.nc_board_id,
    )

    try:
        stacks = deck.get_stacks(include_archived=False)
    except Exception as e:
        log.error("Failed to fetch Deck stacks: %s", e)
        return 1

    todo = _find_stack(stacks, config.todo_stack_name)
    doing = _find_stack(stacks, config.doing_stack_name)
    done = _find_stack(stacks, config.done_stack_name)

    missing = [
        n for n, s in (
            (config.todo_stack_name, todo),
            (config.doing_stack_name, doing),
            (config.done_stack_name, done),
        ) if s is None
    ]
    if missing:
        log.error("Could not find required stack(s): %s on board %s",
                  ", ".join(missing), config.nc_board_id)
        return 1

    try:
        email_label = _find_or_create_email_label(
            deck, config.nc_board_id,
            name=config.email_label_name,
            color=config.email_label_color,
        )
    except Exception as e:
        log.error("Failed to look up or create %r label: %s",
                  config.email_label_name, e)
        return 1
    log.info("Using Email label id=%s on board %s", email_label.id, config.nc_board_id)

    stack_ids = StackIds(todo=todo.id, doing=doing.id, done=done.id)
    managed = fetch_managed(stacks)
    log.info("Found %d managed card(s) on board %s", len(managed), config.nc_board_id)

    archive_actions: list[ArchiveAction] = []
    activity_failures = 0
    if config.archive_done_after_days > 0:
        try:
            activity = fetch_deck_activity(deck)
        except Exception as e:
            log.error("Failed to fetch Deck activity; skipping archive pass: %s", e)
            activity_failures = 1
        else:
            done_cards = getattr(done, "cards", None) or []
            done_at = latest_done_at(activity, config.nc_board_id, done.id)
            archive_actions = plan_archive(
                done_cards=done_cards,
                done_at=done_at,
                email_label_id=email_label.id,
                now=int(time.time()),
                max_age_days=config.archive_done_after_days,
                recur_label_prefix=config.recur_label_prefix,
            )
            log.info(
                "Archive pass: %d of %d card(s) in %s eligible "
                "(%d activity entries, %d with a move-to-Done record)",
                len(archive_actions), len(done_cards), config.done_stack_name,
                len(activity), len(done_at),
            )

    try:
        with MailBox(config.imap_host, port=config.imap_port).login(
            config.imap_user, config.imap_password, initial_folder=config.imap_folder
        ) as mailbox:
            starred = fetch_starred(mailbox.fetch(mark_seen=False, bulk=True))
            log.info("Found %d starred message(s) in %s", len(starred), config.imap_folder)

            imap_actions = make_plan(
                starred=starred,
                managed=managed,
                stack_ids=stack_ids,
                email_label_id=email_label.id,
                recur_label_prefix=config.recur_label_prefix,
            )

            # Planned after make_plan so it can see which cards Pass B already
            # moves into Done, and skip them (one move, not two).
            done_sync_actions: list = []
            if config.done_sync:
                open_cards = (
                    (getattr(todo, "cards", None) or [])
                    + (getattr(doing, "cards", None) or [])
                )
                moving_to_done = {
                    getattr(a.card, "id", None) for a in imap_actions
                    if isinstance(a, MoveToDoneAction)
                }
                done_sync_actions = plan_done_sync(
                    done_cards=getattr(done, "cards", None) or [],
                    open_cards=open_cards,
                    done_stack_id=done.id,
                    now_iso=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    skip_card_ids=moving_to_done,
                )
                ticks = sum(1 for a in done_sync_actions if isinstance(a, TickDoneAction))
                log.info(
                    "Done-tick pass: %d card(s) in %s to tick, %d ticked card(s) to move in",
                    ticks, config.done_stack_name, len(done_sync_actions) - ticks,
                )

            plan = imap_actions + done_sync_actions + archive_actions
            log.info("Plan: %d action(s)%s",
                     len(plan), " (dry-run)" if config.dry_run else "")

            summary = execute_plan(
                plan=plan, mailbox=mailbox, deck=deck, dry_run=config.dry_run
            )
    except Exception as e:
        log.error("IMAP/sync failed: %s", e)
        return 1

    summary.failures += activity_failures

    log.info(
        "Done: created=%d moved=%d unstarred=%d labels_assigned=%d "
        "archived=%d ticked=%d moved_by_tick=%d failures=%d%s",
        summary.created, summary.moved, summary.unstarred,
        summary.labels_assigned, summary.archived, summary.ticked,
        summary.moved_by_tick, summary.failures,
        " (dry-run)" if config.dry_run else "",
    )
    return 0 if summary.failures == 0 else 2
