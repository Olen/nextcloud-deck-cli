"""
IMAP ↔ Nextcloud Deck sync.

Pure helpers + planner live near the top of this file; IMAP/Deck IO and the
orchestration `run()` function live below.

Spec: docs/superpowers/specs/2026-05-11-imap-deck-sync-design.md
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional


MARKER_RE = re.compile(r"<!--\s*imap-sync:\s*message-id=(\S.*?)\s*-->", re.DOTALL)


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
    card: Any           # olen_deck.Card — kept opaque so the planner is library-agnostic
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
    card: Any           # olen_deck.Card
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


Action = UnstarAction | MoveToDoneAction | CreateCardAction | AssignLabelAction

log = logging.getLogger(__name__)


def make_plan(
    starred: dict[str, StarredMessage],
    managed: dict[str, ManagedCard],
    stack_ids: StackIds,
    email_label_id: int,
) -> list[Action]:
    """
    Pure reconciliation function. Inputs are the current state; output is a
    list of mutations to apply.

    Ordering invariant: UnstarActions must precede any CreateCardActions for
    the same Message-ID. We achieve this by tracking message_ids that have
    been handled in Pass A and skipping them in Pass C.

    Pass D: retro-tags any managed card missing the Email label, regardless of
    which stack the card lives in.

    See spec section "Reconciliation algorithm" for the full rationale.
    """
    actions: list[Action] = []
    handled: set[str] = set()

    # Pass A: cards in Done where the message is still starred → unstar.
    for msgid, mc in managed.items():
        if mc.stack_id == stack_ids.done and msgid in starred:
            actions.append(UnstarAction(uid=starred[msgid].uid, message_id=msgid))
            handled.add(msgid)

    # Pass B: cards in Todo/Doing where the message is no longer starred → move to Done.
    for msgid, mc in managed.items():
        if mc.stack_id in (stack_ids.todo, stack_ids.doing) and msgid not in starred:
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
