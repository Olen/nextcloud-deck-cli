"""
IMAP ↔ Nextcloud Deck sync.

Pure helpers + planner live near the top of this file; IMAP/Deck IO and the
orchestration `run()` function live below.

Spec: docs/superpowers/specs/2026-05-11-imap-deck-sync-design.md
"""
from __future__ import annotations

import re
from typing import Optional


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
