# IMAP → Nextcloud Deck Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `nextcloud-deck-imap-sync.py` — a periodic-poll tool that mirrors IMAP `\Flagged` messages (visible in the Dovecot virtual folder `_Virtual/Important`) to a Nextcloud Deck Todo board, and propagates "card → Done" back to the IMAP star.

**Architecture:** Pure-function reconciliation core (`make_plan`) takes "set of starred messages" + "set of managed cards", returns a list of action dataclasses; a thin `execute_plan` step drives IMAP and Deck IO. This keeps the interesting logic unit-testable without mocks. Two files: an importable module `imap_deck_sync.py` for the logic, and a thin CLI wrapper script `nextcloud-deck-imap-sync.py` matching the existing project naming.

**Tech Stack:** Python 3, `olen_deck.DeckClient` (already installed), `imap-tools` (new dep), `pytest` for tests, `olen.log` + `olen.remote_log.RemoteLogger` for logging/alerts.

**Spec:** `docs/superpowers/specs/2026-05-11-imap-deck-sync-design.md`

---

## File Plan

```
nextcloud-deck-cli/
├── nextcloud_deck_core.py           (unchanged — older local copy, unused)
├── nextcloud-deck-todo.py           (unchanged)
├── nextcloud-deck-list.py           (unchanged)
├── nextcloud-deck-cli.py            (unchanged)
├── nextcloud-deck-imap-sync.py      (NEW — CLI entrypoint)
├── imap_deck_sync.py                (NEW — importable module, all logic)
├── imap-deck-sync-wrapper.sh        (NEW — fetches op secrets, invokes the script)
├── systemd/                         (NEW dir for deployment artefacts)
│   ├── imap-deck-sync.service       (NEW — user systemd unit)
│   └── imap-deck-sync.timer         (NEW — user systemd timer)
├── tests/
│   ├── __init__.py                  (NEW — empty marker)
│   └── test_imap_deck_sync.py       (NEW — tests for pure functions)
└── docs/superpowers/specs/2026-05-11-imap-deck-sync-design.md   (existing spec)
```

**Module responsibilities:**

- `imap_deck_sync.py` — all importable logic:
  - Dataclasses: `StarredMessage`, `ManagedCard`, `UnstarAction`, `MoveToDoneAction`, `CreateCardAction`
  - Pure helpers: `parse_marker(description) -> Optional[str]`, `format_card_title(name, addr, subject) -> str`, `build_marker(message_id) -> str`
  - Pure planner: `make_plan(starred, managed, stack_ids) -> list[Action]`
  - IO functions: `fetch_starred(mailbox) -> dict[str, StarredMessage]`, `fetch_managed(stacks) -> dict[str, ManagedCard]`, `execute_plan(plan, mailbox, deck, dry_run, log) -> Summary`
  - Orchestrator: `run(config) -> int` — returns exit code

- `nextcloud-deck-imap-sync.py` — thin CLI:
  - Argparse + env-var fallback
  - `sys.path` insertion so `imap_deck_sync` is importable next to this script
  - Calls `imap_deck_sync.run(config)`

---

## Task 0: Setup — dependencies, test scaffold, deps file

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `requirements.txt`

- [ ] **Step 0.1: Install runtime dependency `imap-tools` (per user, no venv)**

Run:
```bash
pip install --user imap-tools
```

Expected: install completes, `python3 -c "import imap_tools; print(imap_tools.__version__)"` prints a version.

- [ ] **Step 0.2: Install test dependency `pytest`**

Run:
```bash
pip install --user pytest
```

Expected: `python3 -m pytest --version` prints a version like `pytest 8.x.y`.

- [ ] **Step 0.3: Create `requirements.txt` documenting runtime deps**

Create `requirements.txt`:
```
imap-tools>=1.0
# olen_deck is installed from the private Olen PyPI index
# olen is installed from the private Olen PyPI index
```

- [ ] **Step 0.4: Create empty test-package marker**

Create `tests/__init__.py` with no content (empty file).

- [ ] **Step 0.5: Create `tests/conftest.py` so pytest finds the project root on sys.path**

Create `tests/conftest.py`:
```python
"""
Make the project root importable so tests can `import imap_deck_sync`
without installing the project.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

- [ ] **Step 0.6: Sanity-check pytest discovers the tests directory**

Run:
```bash
cd /home/olen/prog/nextcloud-deck-cli && python3 -m pytest tests/ -v
```

Expected: `no tests ran` (no failures). Confirms discovery + conftest works.

- [ ] **Step 0.7: Commit**

Run:
```bash
git add requirements.txt tests/__init__.py tests/conftest.py
git commit -m "chore: scaffold tests dir and pin imap-tools runtime dep"
```

---

## Task 1: Marker parsing (`parse_marker`, `build_marker`)

The marker is `<!-- imap-sync: message-id=<msgid> -->`. We need to write it on create and find it later regardless of where the user has edited the description.

**Files:**
- Create: `imap_deck_sync.py`
- Test: `tests/test_imap_deck_sync.py`

- [ ] **Step 1.1: Write failing tests for `parse_marker` and `build_marker`**

Create `tests/test_imap_deck_sync.py`:
```python
from imap_deck_sync import parse_marker, build_marker


class TestBuildMarker:
    def test_wraps_message_id_in_html_comment(self):
        assert build_marker("<abc@example.com>") == "<!-- imap-sync: message-id=<abc@example.com> -->"

    def test_no_extra_whitespace(self):
        marker = build_marker("<x@y>")
        assert "  " not in marker


class TestParseMarker:
    def test_marker_alone(self):
        assert parse_marker("<!-- imap-sync: message-id=<abc@example.com> -->") == "<abc@example.com>"

    def test_marker_with_trailing_text(self):
        desc = "<!-- imap-sync: message-id=<abc@example.com> -->\nUser added this note later."
        assert parse_marker(desc) == "<abc@example.com>"

    def test_marker_not_on_first_line(self):
        desc = "User wrote some prose here.\n\n<!-- imap-sync: message-id=<abc@example.com> -->\nMore notes."
        assert parse_marker(desc) == "<abc@example.com>"

    def test_no_marker_returns_none(self):
        assert parse_marker("Just some user notes, no marker.") is None

    def test_empty_description(self):
        assert parse_marker("") is None

    def test_none_description(self):
        assert parse_marker(None) is None

    def test_message_id_with_plus_and_special_chars(self):
        # Real Message-IDs have +, /, =, and angle brackets.
        msgid = "<CA+abc=def/ghi@mail.example.com>"
        desc = f"<!-- imap-sync: message-id={msgid} -->"
        assert parse_marker(desc) == msgid

    def test_only_first_marker_returned_if_duplicated(self):
        # Shouldn't happen, but be deterministic if it does.
        desc = (
            "<!-- imap-sync: message-id=<first@x> -->\n"
            "<!-- imap-sync: message-id=<second@x> -->"
        )
        assert parse_marker(desc) == "<first@x>"
```

- [ ] **Step 1.2: Run tests, confirm they all fail with ImportError**

Run:
```bash
cd /home/olen/prog/nextcloud-deck-cli && python3 -m pytest tests/test_imap_deck_sync.py -v
```

Expected: collection error or all-fail with `ModuleNotFoundError: No module named 'imap_deck_sync'`.

- [ ] **Step 1.3: Create `imap_deck_sync.py` with minimal implementation**

Create `imap_deck_sync.py`:
```python
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
```

- [ ] **Step 1.4: Run tests, confirm they pass**

Run:
```bash
cd /home/olen/prog/nextcloud-deck-cli && python3 -m pytest tests/test_imap_deck_sync.py -v
```

Expected: 8 passed.

- [ ] **Step 1.5: Commit**

Run:
```bash
git add imap_deck_sync.py tests/test_imap_deck_sync.py
git commit -m "feat(sync): parse/build imap-sync description markers"
```

---

## Task 2: Title formatting (`format_card_title`)

Format `"{name or addr}: {subject}"`, collapsing whitespace and truncating to 200 chars.

**Files:**
- Modify: `imap_deck_sync.py`
- Test: `tests/test_imap_deck_sync.py`

- [ ] **Step 2.1: Add failing tests**

Append to `tests/test_imap_deck_sync.py`:
```python
from imap_deck_sync import format_card_title


class TestFormatCardTitle:
    def test_uses_name_when_present(self):
        assert format_card_title("Alice", "alice@example.com", "Hi") == "Alice: Hi"

    def test_falls_back_to_email_when_name_empty(self):
        assert format_card_title("", "alice@example.com", "Hi") == "alice@example.com: Hi"

    def test_falls_back_to_email_when_name_none(self):
        assert format_card_title(None, "alice@example.com", "Hi") == "alice@example.com: Hi"

    def test_collapses_internal_whitespace(self):
        assert format_card_title("Alice", "a@x", "Hi   there\n\tworld") == "Alice: Hi there world"

    def test_strips_leading_trailing_whitespace_in_subject(self):
        assert format_card_title("Alice", "a@x", "   spaced   ") == "Alice: spaced"

    def test_truncates_at_200_chars(self):
        long_subject = "x" * 500
        title = format_card_title("Alice", "a@x", long_subject)
        assert len(title) == 200
        assert title.startswith("Alice: ")

    def test_handles_empty_subject(self):
        assert format_card_title("Alice", "a@x", "") == "Alice: "

    def test_handles_no_subject_None(self):
        assert format_card_title("Alice", "a@x", None) == "Alice: "
```

- [ ] **Step 2.2: Run tests, confirm they fail**

Run:
```bash
cd /home/olen/prog/nextcloud-deck-cli && python3 -m pytest tests/test_imap_deck_sync.py::TestFormatCardTitle -v
```

Expected: 8 fail with `ImportError: cannot import name 'format_card_title'`.

- [ ] **Step 2.3: Implement `format_card_title`**

Append to `imap_deck_sync.py` (after `parse_marker`):
```python
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
```

- [ ] **Step 2.4: Run tests, confirm all pass**

Run:
```bash
cd /home/olen/prog/nextcloud-deck-cli && python3 -m pytest tests/test_imap_deck_sync.py -v
```

Expected: 16 passed.

- [ ] **Step 2.5: Commit**

Run:
```bash
git add imap_deck_sync.py tests/test_imap_deck_sync.py
git commit -m "feat(sync): format_card_title with name/addr fallback and 200-char truncation"
```

---

## Task 3: Dataclasses + `make_plan` pure reconciliation function

This is the heart of the spec — Section "Reconciliation algorithm". The whole point of the architecture is that this function is pure (no IO) and exhaustively unit-tested.

**Files:**
- Modify: `imap_deck_sync.py`
- Test: `tests/test_imap_deck_sync.py`

- [ ] **Step 3.1: Add failing tests for the planner**

Append to `tests/test_imap_deck_sync.py`:
```python
from imap_deck_sync import (
    StarredMessage,
    ManagedCard,
    UnstarAction,
    MoveToDoneAction,
    CreateCardAction,
    StackIds,
    make_plan,
)


def _stack_ids():
    return StackIds(todo=1, doing=2, done=3)


def _starred(msgid, uid="1", name="Alice", addr="alice@example.com", subject="Hi"):
    return StarredMessage(message_id=msgid, uid=uid, from_name=name, from_addr=addr, subject=subject)


def _managed(msgid, stack_id, card_id=42):
    # Card is opaque to the planner — we only need an identity for downstream actions.
    # We pass a SimpleNamespace so production code can attach a real Card here.
    from types import SimpleNamespace
    card = SimpleNamespace(id=card_id, title="(irrelevant)")
    return ManagedCard(message_id=msgid, card=card, stack_id=stack_id)


class TestMakePlan:
    def test_empty_inputs_yield_empty_plan(self):
        assert make_plan(starred={}, managed={}, stack_ids=_stack_ids()) == []

    def test_new_starred_message_creates_card_in_todo(self):
        starred = {"<a@x>": _starred("<a@x>")}
        plan = make_plan(starred=starred, managed={}, stack_ids=_stack_ids())
        assert plan == [
            CreateCardAction(
                stack_id=1,
                title="Alice: Hi",
                description="<!-- imap-sync: message-id=<a@x> -->\n",
                message_id="<a@x>",
            )
        ]

    def test_managed_card_in_todo_for_still_starred_message_is_noop(self):
        starred = {"<a@x>": _starred("<a@x>")}
        managed = {"<a@x>": _managed("<a@x>", stack_id=1)}
        assert make_plan(starred=starred, managed=managed, stack_ids=_stack_ids()) == []

    def test_managed_card_in_doing_for_unstarred_message_moves_to_done(self):
        managed = {"<a@x>": _managed("<a@x>", stack_id=2)}
        plan = make_plan(starred={}, managed=managed, stack_ids=_stack_ids())
        assert len(plan) == 1
        assert isinstance(plan[0], MoveToDoneAction)
        assert plan[0].card is managed["<a@x>"].card
        assert plan[0].target_stack_id == 3

    def test_managed_card_in_todo_for_unstarred_message_moves_to_done(self):
        managed = {"<a@x>": _managed("<a@x>", stack_id=1)}
        plan = make_plan(starred={}, managed=managed, stack_ids=_stack_ids())
        assert isinstance(plan[0], MoveToDoneAction)

    def test_managed_card_in_done_with_still_starred_message_clears_flag(self):
        starred = {"<a@x>": _starred("<a@x>", uid="77")}
        managed = {"<a@x>": _managed("<a@x>", stack_id=3)}
        plan = make_plan(starred=starred, managed=managed, stack_ids=_stack_ids())
        assert plan == [UnstarAction(uid="77", message_id="<a@x>")]

    def test_managed_card_in_done_with_unstarred_message_is_noop(self):
        managed = {"<a@x>": _managed("<a@x>", stack_id=3)}
        assert make_plan(starred={}, managed=managed, stack_ids=_stack_ids()) == []

    def test_managed_card_in_custom_stack_is_left_alone(self):
        # User moved an email-card to a "Later" stack (id=99). Sync recognises
        # the marker so it must NOT create a duplicate even though the message
        # is still in the starred set.
        starred = {"<a@x>": _starred("<a@x>")}
        managed = {"<a@x>": _managed("<a@x>", stack_id=99)}
        plan = make_plan(starred=starred, managed=managed, stack_ids=_stack_ids())
        assert plan == []

    def test_pass_a_clears_flag_then_pass_c_does_not_recreate(self):
        # Card in Done + message still starred → unstar; no Create on same run.
        starred = {"<a@x>": _starred("<a@x>", uid="9")}
        managed = {"<a@x>": _managed("<a@x>", stack_id=3)}
        plan = make_plan(starred=starred, managed=managed, stack_ids=_stack_ids())
        assert plan == [UnstarAction(uid="9", message_id="<a@x>")]
        # No CreateCardAction
        assert not any(isinstance(a, CreateCardAction) for a in plan)

    def test_full_mixed_scenario(self):
        starred = {
            "<keep@x>": _starred("<keep@x>", subject="keep me"),
            "<unstar-me@x>": _starred("<unstar-me@x>", uid="55", subject="user dragged to Done"),
            "<new@x>": _starred("<new@x>", name="Bob", addr="bob@x", subject="brand new"),
        }
        managed = {
            "<keep@x>": _managed("<keep@x>", stack_id=1),           # active, still starred — no-op
            "<unstar-me@x>": _managed("<unstar-me@x>", stack_id=3), # in Done, still starred — clear flag
            "<gone@x>": _managed("<gone@x>", stack_id=2),           # was doing, no longer starred — to Done
        }
        plan = make_plan(starred=starred, managed=managed, stack_ids=_stack_ids())

        # Convert to set-of-types for order-tolerant assertion of categories
        kinds = [type(a).__name__ for a in plan]
        assert sorted(kinds) == sorted(["UnstarAction", "MoveToDoneAction", "CreateCardAction"])

        # Find each by type and verify details
        unstar = next(a for a in plan if isinstance(a, UnstarAction))
        move = next(a for a in plan if isinstance(a, MoveToDoneAction))
        create = next(a for a in plan if isinstance(a, CreateCardAction))

        assert unstar == UnstarAction(uid="55", message_id="<unstar-me@x>")
        assert move.target_stack_id == 3
        assert move.card is managed["<gone@x>"].card
        assert create.message_id == "<new@x>"
        assert create.title == "Bob: brand new"
        assert create.description == "<!-- imap-sync: message-id=<new@x> -->\n"
        assert create.stack_id == 1

    def test_unstar_pass_runs_before_create_pass(self):
        # If a message is in Done AND somehow still in starred, we must not
        # *also* create a new card for it. (Same as test above but verifies
        # ordering invariant.)
        starred = {"<x@x>": _starred("<x@x>", uid="11")}
        managed = {"<x@x>": _managed("<x@x>", stack_id=3)}
        plan = make_plan(starred=starred, managed=managed, stack_ids=_stack_ids())
        assert not any(isinstance(a, CreateCardAction) for a in plan)
```

- [ ] **Step 3.2: Run tests, confirm they fail (missing names)**

Run:
```bash
cd /home/olen/prog/nextcloud-deck-cli && python3 -m pytest tests/test_imap_deck_sync.py::TestMakePlan -v
```

Expected: collection error on `ImportError` for `StarredMessage`, `make_plan`, etc.

- [ ] **Step 3.3: Implement the dataclasses + `make_plan`**

Append to `imap_deck_sync.py` (after `format_card_title`):
```python
from dataclasses import dataclass
from typing import Any


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
    """Create a new Deck card in the Todo stack."""
    stack_id: int
    title: str
    description: str
    message_id: str


Action = Any  # union of the three Action dataclasses above


def make_plan(
    starred: dict[str, StarredMessage],
    managed: dict[str, ManagedCard],
    stack_ids: StackIds,
) -> list[Action]:
    """
    Pure reconciliation function. Inputs are the current state; output is a
    list of mutations to apply.

    Ordering invariant: UnstarActions must precede any CreateCardActions for
    the same Message-ID. We achieve this by tracking message_ids that have
    been handled in Pass A and skipping them in Pass C.

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
            )
        )

    return actions
```

- [ ] **Step 3.4: Run tests, confirm all pass**

Run:
```bash
cd /home/olen/prog/nextcloud-deck-cli && python3 -m pytest tests/test_imap_deck_sync.py -v
```

Expected: 27 passed (16 from earlier + 11 new `TestMakePlan` cases).

- [ ] **Step 3.5: Commit**

Run:
```bash
git add imap_deck_sync.py tests/test_imap_deck_sync.py
git commit -m "feat(sync): pure make_plan reconciler with action dataclasses"
```

---

## Task 4: `fetch_managed` — parse marker cards out of Deck stacks

Walks every stack on the board, picks out cards carrying the imap-sync marker, builds `dict[message_id, ManagedCard]`.

**Files:**
- Modify: `imap_deck_sync.py`
- Test: `tests/test_imap_deck_sync.py`

- [ ] **Step 4.1: Add failing tests**

Append to `tests/test_imap_deck_sync.py`:
```python
from imap_deck_sync import fetch_managed


class _FakeCard:
    """Minimal stand-in for olen_deck.Card."""
    def __init__(self, id, description="", stack_id=None):
        self.id = id
        self.description = description
        self.stack_id = stack_id


class _FakeStack:
    def __init__(self, id, cards):
        self.id = id
        self.cards = cards


class TestFetchManaged:
    def test_empty_board_returns_empty_dict(self):
        assert fetch_managed(stacks=[]) == {}

    def test_skips_cards_without_marker(self):
        stacks = [_FakeStack(id=1, cards=[
            _FakeCard(id=10, description="just a manual card", stack_id=1),
            _FakeCard(id=11, description="", stack_id=1),
        ])]
        assert fetch_managed(stacks=stacks) == {}

    def test_picks_up_managed_card(self):
        stacks = [_FakeStack(id=1, cards=[
            _FakeCard(id=10, description="<!-- imap-sync: message-id=<a@x> -->", stack_id=1),
        ])]
        managed = fetch_managed(stacks=stacks)
        assert set(managed.keys()) == {"<a@x>"}
        assert managed["<a@x>"].stack_id == 1
        assert managed["<a@x>"].card.id == 10
        assert managed["<a@x>"].message_id == "<a@x>"

    def test_scans_all_stacks_not_just_three(self):
        # Cards in any stack, including custom ones, must be discovered.
        stacks = [
            _FakeStack(id=1, cards=[_FakeCard(id=10, description="<!-- imap-sync: message-id=<a@x> -->", stack_id=1)]),
            _FakeStack(id=2, cards=[_FakeCard(id=20, description="<!-- imap-sync: message-id=<b@x> -->", stack_id=2)]),
            _FakeStack(id=99, cards=[_FakeCard(id=30, description="<!-- imap-sync: message-id=<c@x> -->", stack_id=99)]),
        ]
        managed = fetch_managed(stacks=stacks)
        assert set(managed.keys()) == {"<a@x>", "<b@x>", "<c@x>"}
        assert managed["<c@x>"].stack_id == 99

    def test_duplicate_message_id_keeps_first_and_warns(self, caplog):
        import logging
        stacks = [
            _FakeStack(id=1, cards=[_FakeCard(id=10, description="<!-- imap-sync: message-id=<dup@x> -->", stack_id=1)]),
            _FakeStack(id=2, cards=[_FakeCard(id=20, description="<!-- imap-sync: message-id=<dup@x> -->", stack_id=2)]),
        ]
        with caplog.at_level(logging.WARNING):
            managed = fetch_managed(stacks=stacks)
        assert managed["<dup@x>"].card.id == 10
        assert any("duplicate" in rec.message.lower() for rec in caplog.records)
```

- [ ] **Step 4.2: Run, confirm new tests fail**

Run:
```bash
cd /home/olen/prog/nextcloud-deck-cli && python3 -m pytest tests/test_imap_deck_sync.py::TestFetchManaged -v
```

Expected: ImportError on `fetch_managed`.

- [ ] **Step 4.3: Implement `fetch_managed`**

Append to `imap_deck_sync.py`:
```python
import logging

log = logging.getLogger(__name__)


def fetch_managed(stacks) -> dict[str, ManagedCard]:
    """
    Walk every stack on the board, parse each card's description, and return
    a {message_id: ManagedCard} mapping for cards that carry our marker.

    Cards without the marker are silently skipped. Cards whose Message-ID has
    already been seen log a WARN and are skipped (the first sighting wins).
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
            managed[msgid] = ManagedCard(
                message_id=msgid,
                card=card,
                stack_id=stack.id,
            )
    return managed
```

- [ ] **Step 4.4: Run tests, confirm all pass**

Run:
```bash
cd /home/olen/prog/nextcloud-deck-cli && python3 -m pytest tests/test_imap_deck_sync.py -v
```

Expected: 32 passed.

- [ ] **Step 4.5: Commit**

Run:
```bash
git add imap_deck_sync.py tests/test_imap_deck_sync.py
git commit -m "feat(sync): fetch_managed scans all stacks for imap-sync marker cards"
```

---

## Task 5: `fetch_starred` — pull starred messages from IMAP

Thin wrapper over `imap_tools.MailBox.fetch()`. The function takes a "messages iterator" (so tests can pass a list of fakes) and returns the dict. The real IMAP connection is opened at call sites.

**Files:**
- Modify: `imap_deck_sync.py`
- Test: `tests/test_imap_deck_sync.py`

- [ ] **Step 5.1: Add failing tests**

Append to `tests/test_imap_deck_sync.py`:
```python
from types import SimpleNamespace
from imap_deck_sync import fetch_starred


def _imap_msg(uid, message_id, from_name="Alice", from_addr="a@x", subject="Hi"):
    """Mimics an imap_tools MailMessage closely enough for fetch_starred."""
    return SimpleNamespace(
        uid=uid,
        from_values=SimpleNamespace(name=from_name, email=from_addr),
        subject=subject,
        headers={"message-id": (message_id,)},
    )


class TestFetchStarred:
    def test_empty_inbox_returns_empty_dict(self):
        assert fetch_starred(messages_iter=iter([])) == {}

    def test_returns_message_id_keyed_dict(self):
        msgs = [_imap_msg(uid="1", message_id="<a@x>")]
        result = fetch_starred(messages_iter=iter(msgs))
        assert set(result.keys()) == {"<a@x>"}
        sm = result["<a@x>"]
        assert sm.uid == "1"
        assert sm.from_name == "Alice"
        assert sm.from_addr == "a@x"
        assert sm.subject == "Hi"

    def test_skips_messages_missing_message_id_header(self, caplog):
        import logging
        msgs = [
            SimpleNamespace(
                uid="2",
                from_values=SimpleNamespace(name="", email="a@x"),
                subject="no id",
                headers={},   # no message-id
            ),
        ]
        with caplog.at_level(logging.WARNING):
            result = fetch_starred(messages_iter=iter(msgs))
        assert result == {}
        assert any("message-id" in rec.message.lower() for rec in caplog.records)

    def test_first_message_wins_on_duplicate_message_id(self, caplog):
        import logging
        msgs = [
            _imap_msg(uid="1", message_id="<dup@x>", subject="first"),
            _imap_msg(uid="2", message_id="<dup@x>", subject="second"),
        ]
        with caplog.at_level(logging.WARNING):
            result = fetch_starred(messages_iter=iter(msgs))
        assert result["<dup@x>"].subject == "first"
        assert any("duplicate" in rec.message.lower() for rec in caplog.records)

    def test_message_id_is_taken_verbatim_with_angle_brackets(self):
        msgs = [_imap_msg(uid="1", message_id="<CA+xyz@example.com>")]
        result = fetch_starred(messages_iter=iter(msgs))
        assert "<CA+xyz@example.com>" in result
```

- [ ] **Step 5.2: Run, confirm tests fail**

Run:
```bash
cd /home/olen/prog/nextcloud-deck-cli && python3 -m pytest tests/test_imap_deck_sync.py::TestFetchStarred -v
```

Expected: ImportError on `fetch_starred`.

- [ ] **Step 5.3: Implement `fetch_starred`**

Append to `imap_deck_sync.py`:
```python
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
```

Also add the import at the top of the file (after the existing imports):
```python
from types import SimpleNamespace
```

- [ ] **Step 5.4: Run tests, confirm all pass**

Run:
```bash
cd /home/olen/prog/nextcloud-deck-cli && python3 -m pytest tests/test_imap_deck_sync.py -v
```

Expected: 37 passed.

- [ ] **Step 5.5: Commit**

Run:
```bash
git add imap_deck_sync.py tests/test_imap_deck_sync.py
git commit -m "feat(sync): fetch_starred builds Message-ID→StarredMessage map"
```

---

## Task 6: `execute_plan` — apply actions to IMAP and Deck

Takes the action list from `make_plan`, dispatches each action to the right client. Supports `dry_run` (log only, no IO). Returns a summary tuple of counts.

The IMAP and Deck clients are passed in as duck-typed dependencies so tests can drive them with fakes.

**Files:**
- Modify: `imap_deck_sync.py`
- Test: `tests/test_imap_deck_sync.py`

- [ ] **Step 6.1: Add failing tests**

Append to `tests/test_imap_deck_sync.py`:
```python
from imap_deck_sync import execute_plan, ExecutionSummary


class _FakeMailbox:
    """imap_tools MailBox stand-in supporting just flag(uids, flag, value=False)."""
    def __init__(self, fail_on=()):
        self.flag_calls = []
        self.fail_on = set(fail_on)

    def flag(self, uids, flag_set, value):
        # imap_tools flag() takes (uids: str|iterable, flag_set, value)
        uid_list = [uids] if isinstance(uids, str) else list(uids)
        for uid in uid_list:
            if uid in self.fail_on:
                raise RuntimeError(f"simulated IMAP failure on uid {uid}")
        self.flag_calls.append((uid_list, flag_set, value))


class _FakeDeck:
    def __init__(self, fail_creates=False, fail_moves_for_card_ids=()):
        self.created = []
        self.moved = []
        self.fail_creates = fail_creates
        self.fail_moves_for_card_ids = set(fail_moves_for_card_ids)

    def create_card(self, stack_id, title, description="", **kwargs):
        if self.fail_creates:
            raise RuntimeError("simulated create failure")
        card = SimpleNamespace(id=len(self.created) + 1000, title=title, stack_id=stack_id, description=description)
        self.created.append({"stack_id": stack_id, "title": title, "description": description})
        return card

    def move_card(self, card, target_stack_id):
        if card.id in self.fail_moves_for_card_ids:
            raise RuntimeError(f"simulated move failure for card {card.id}")
        self.moved.append({"card_id": card.id, "target": target_stack_id})
        card.stack_id = target_stack_id
        return card


class TestExecutePlan:
    def test_empty_plan_is_noop(self):
        mailbox, deck = _FakeMailbox(), _FakeDeck()
        summary = execute_plan(plan=[], mailbox=mailbox, deck=deck, dry_run=False)
        assert summary == ExecutionSummary(created=0, moved=0, unstarred=0, failures=0)
        assert mailbox.flag_calls == []
        assert deck.created == []
        assert deck.moved == []

    def test_creates_card(self):
        mailbox, deck = _FakeMailbox(), _FakeDeck()
        plan = [CreateCardAction(stack_id=1, title="t", description="d", message_id="<a@x>")]
        summary = execute_plan(plan=plan, mailbox=mailbox, deck=deck, dry_run=False)
        assert summary.created == 1
        assert deck.created == [{"stack_id": 1, "title": "t", "description": "d"}]

    def test_moves_card(self):
        mailbox, deck = _FakeMailbox(), _FakeDeck()
        card = SimpleNamespace(id=42, title="t", stack_id=2)
        plan = [MoveToDoneAction(card=card, target_stack_id=3)]
        summary = execute_plan(plan=plan, mailbox=mailbox, deck=deck, dry_run=False)
        assert summary.moved == 1
        assert deck.moved == [{"card_id": 42, "target": 3}]

    def test_unstars_message(self):
        from imap_tools import MailMessageFlags  # confirms the dep is present
        mailbox, deck = _FakeMailbox(), _FakeDeck()
        plan = [UnstarAction(uid="55", message_id="<a@x>")]
        summary = execute_plan(plan=plan, mailbox=mailbox, deck=deck, dry_run=False)
        assert summary.unstarred == 1
        assert len(mailbox.flag_calls) == 1
        uids, flag_set, value = mailbox.flag_calls[0]
        assert uids == ["55"]
        assert MailMessageFlags.FLAGGED in flag_set
        assert value is False

    def test_dry_run_performs_no_mutations(self):
        mailbox, deck = _FakeMailbox(), _FakeDeck()
        card = SimpleNamespace(id=42, title="t", stack_id=2)
        plan = [
            CreateCardAction(stack_id=1, title="t", description="d", message_id="<a@x>"),
            MoveToDoneAction(card=card, target_stack_id=3),
            UnstarAction(uid="55", message_id="<a@x>"),
        ]
        summary = execute_plan(plan=plan, mailbox=mailbox, deck=deck, dry_run=True)
        # Counts reflect what would happen
        assert summary == ExecutionSummary(created=1, moved=1, unstarred=1, failures=0)
        # But no IO was performed
        assert deck.created == [] and deck.moved == [] and mailbox.flag_calls == []

    def test_per_action_failures_increment_failure_counter_and_continue(self, caplog):
        import logging
        mailbox = _FakeMailbox(fail_on=["bad-uid"])
        deck = _FakeDeck(fail_moves_for_card_ids={42})
        card_good = SimpleNamespace(id=43, title="t", stack_id=2)
        card_bad = SimpleNamespace(id=42, title="t", stack_id=2)
        plan = [
            UnstarAction(uid="bad-uid", message_id="<a@x>"),
            MoveToDoneAction(card=card_bad, target_stack_id=3),     # fails
            MoveToDoneAction(card=card_good, target_stack_id=3),    # succeeds
            CreateCardAction(stack_id=1, title="t", description="d", message_id="<b@x>"),
        ]
        with caplog.at_level(logging.WARNING):
            summary = execute_plan(plan=plan, mailbox=mailbox, deck=deck, dry_run=False)
        assert summary.failures == 2
        assert summary.created == 1
        assert summary.moved == 1
        assert summary.unstarred == 0
        # All four were attempted; the failure didn't abort the run
        assert deck.created and deck.moved
```

- [ ] **Step 6.2: Run, confirm tests fail**

Run:
```bash
cd /home/olen/prog/nextcloud-deck-cli && python3 -m pytest tests/test_imap_deck_sync.py::TestExecutePlan -v
```

Expected: ImportError on `execute_plan` / `ExecutionSummary`.

- [ ] **Step 6.3: Implement `execute_plan` and `ExecutionSummary`**

Append to `imap_deck_sync.py`:
```python
from dataclasses import dataclass as _dataclass

# We import inside the function so the rest of the module stays importable
# when imap_tools isn't installed (e.g. during unit tests on a clean checkout).
def _flagged_flag():
    from imap_tools import MailMessageFlags
    return MailMessageFlags.FLAGGED


@dataclass
class ExecutionSummary:
    """Counts of actions actually applied during execute_plan."""
    created: int = 0
    moved: int = 0
    unstarred: int = 0
    failures: int = 0


def execute_plan(plan, mailbox, deck, dry_run: bool = False) -> ExecutionSummary:
    """
    Apply a plan from make_plan() against the IMAP mailbox and Deck client.

    Per-action failures are logged and counted in `summary.failures`; the rest
    of the plan still runs (idempotency means the next run will pick up the
    pieces).

    `mailbox` must duck-type as imap_tools.MailBox (we call `.flag(uids, {FLAGGED}, False)`).
    `deck` must duck-type as olen_deck.DeckClient (we call `.create_card(...)` and `.move_card(...)`).
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
                    log.info("[dry-run] would create card in stack #%s: %r",
                             action.stack_id, action.title)
                else:
                    log.info("Creating card in stack #%s: %r", action.stack_id, action.title)
                    deck.create_card(
                        stack_id=action.stack_id,
                        title=action.title,
                        description=action.description,
                    )
                summary.created += 1

            else:
                log.warning("Unknown action type %r — skipping", type(action).__name__)
                summary.failures += 1

        except Exception as e:
            log.warning("Action %r failed: %s", action, e)
            summary.failures += 1

    return summary
```

- [ ] **Step 6.4: Run tests, confirm all pass**

Run:
```bash
cd /home/olen/prog/nextcloud-deck-cli && python3 -m pytest tests/test_imap_deck_sync.py -v
```

Expected: 43 passed.

- [ ] **Step 6.5: Commit**

Run:
```bash
git add imap_deck_sync.py tests/test_imap_deck_sync.py
git commit -m "feat(sync): execute_plan applies actions with dry-run + per-action failure isolation"
```

---

## Task 7: `run()` orchestrator + CLI entrypoint script

`run()` opens an IMAP MailBox, opens a DeckClient, calls `fetch_starred` → `fetch_managed` → `make_plan` → `execute_plan`, and prints a one-line summary. The CLI script does argparse with env fallbacks then calls `run()`.

**Files:**
- Modify: `imap_deck_sync.py`
- Create: `nextcloud-deck-imap-sync.py`

- [ ] **Step 7.1: Add `run()` and `Config` to `imap_deck_sync.py`**

Append to `imap_deck_sync.py`:
```python
@dataclass
class Config:
    nc_url: str
    nc_username: str
    nc_password: str
    nc_board_id: int
    todo_stack_name: str
    doing_stack_name: str
    done_stack_name: str
    imap_host: str
    imap_port: int
    imap_user: str
    imap_password: str
    imap_folder: str
    dry_run: bool = False
    verbose: bool = False


def _find_stack(stacks, name: str):
    target = (name or "").strip().lower()
    for s in stacks:
        if (getattr(s, "title", "") or "").strip().lower() == target:
            return s
    return None


def run(config: Config) -> int:
    """
    Orchestrate one full sync. Returns a process exit code (0 on success).
    """
    from imap_tools import MailBox
    from olen_deck import DeckClient

    deck = DeckClient(
        config.nc_url,
        config.nc_username,
        config.nc_password,
        config.nc_board_id,
    )

    try:
        stacks = deck.fetch_stacks(include_archived=False)
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

    stack_ids = StackIds(todo=todo.id, doing=doing.id, done=done.id)
    managed = fetch_managed(stacks)
    log.info("Found %d managed card(s) on board %s", len(managed), config.nc_board_id)

    try:
        with MailBox(config.imap_host, port=config.imap_port).login(
            config.imap_user, config.imap_password, initial_folder=config.imap_folder
        ) as mailbox:
            starred = fetch_starred(mailbox.fetch(mark_seen=False, bulk=True))
            log.info("Found %d starred message(s) in %s", len(starred), config.imap_folder)

            plan = make_plan(starred=starred, managed=managed, stack_ids=stack_ids)
            log.info("Plan: %d action(s)%s",
                     len(plan), " (dry-run)" if config.dry_run else "")

            summary = execute_plan(
                plan=plan, mailbox=mailbox, deck=deck, dry_run=config.dry_run
            )
    except Exception as e:
        log.error("IMAP/sync failed: %s", e)
        return 1

    log.info(
        "Done: created=%d moved=%d unstarred=%d failures=%d%s",
        summary.created, summary.moved, summary.unstarred, summary.failures,
        " (dry-run)" if config.dry_run else "",
    )
    return 0 if summary.failures == 0 else 2
```

- [ ] **Step 7.2: Create the CLI wrapper script**

Create `nextcloud-deck-imap-sync.py` and make it executable:
```python
#!/usr/bin/env python3
"""
CLI entrypoint for the IMAP → Nextcloud Deck sync.

Reads config from CLI flags or matching env vars, then delegates to
`imap_deck_sync.run()`.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Make the sibling module importable when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from imap_deck_sync import Config, run  # noqa: E402


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

    # IMAP
    p.add_argument("--imap-host", default=env("IMAP_HOST", "localhost"))
    p.add_argument("--imap-port", type=int, default=int(env("IMAP_PORT", "993")))
    p.add_argument("--imap-user", default=env("IMAP_USER"))
    p.add_argument("--imap-password", default=env("IMAP_PASSWORD"))
    p.add_argument("--imap-folder", default=env("IMAP_FOLDER", "_Virtual/Important"))

    # Modes
    p.add_argument("--dry-run", action="store_true",
                   help="Plan everything; perform no IMAP or Deck mutations.")
    p.add_argument("-v", "--verbose", action="store_true")

    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

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

    cfg = Config(
        nc_url=args.url,
        nc_username=args.username,
        nc_password=args.password,
        nc_board_id=args.board_id,
        todo_stack_name=args.todo_name,
        doing_stack_name=args.doing_name,
        done_stack_name=args.done_name,
        imap_host=args.imap_host,
        imap_port=args.imap_port,
        imap_user=args.imap_user,
        imap_password=args.imap_password,
        imap_folder=args.imap_folder,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())
```

Make it executable:
```bash
chmod +x /home/olen/prog/nextcloud-deck-cli/nextcloud-deck-imap-sync.py
```

- [ ] **Step 7.3: Smoke-test the CLI parses --help**

Run:
```bash
cd /home/olen/prog/nextcloud-deck-cli && ./nextcloud-deck-imap-sync.py --help
```

Expected: usage text printed, exit 0. (No real sync attempted.)

- [ ] **Step 7.4: Run all unit tests again to be sure nothing regressed**

Run:
```bash
cd /home/olen/prog/nextcloud-deck-cli && python3 -m pytest tests/ -v
```

Expected: 43 passed.

- [ ] **Step 7.5: Commit**

Run:
```bash
git add imap_deck_sync.py nextcloud-deck-imap-sync.py
git commit -m "feat(sync): run() orchestrator and CLI entrypoint"
```

---

## Task 8: Wire `olen` library logging + Discord alerts on run failure

Replace the bare `logging.basicConfig` in the CLI with the project's standard `olen.log.get_logger` setup, and send a `error_discord` alert when `run()` returns non-zero.

**Files:**
- Modify: `nextcloud-deck-imap-sync.py`

- [ ] **Step 8.1: Confirm the olen library shape**

Run:
```bash
python3 -c "
from olen.config import APP_CONFIG
from olen.const import ATTR_APP, ATTR_LOG
from olen.log import get_logger
from olen.remote_log import RemoteLogger
print('olen API available')"
```

Expected: prints `olen API available` and exits 0. If this fails, install or refresh the `olen` package per `/home/olen/CLAUDE.md`.

- [ ] **Step 8.2: Replace the logging block in the CLI**

In `nextcloud-deck-imap-sync.py`, replace:
```python
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
```

with:
```python
    from olen.config import APP_CONFIG
    from olen.const import ATTR_APP, ATTR_LOG
    from olen.log import get_logger
    from olen.remote_log import RemoteLogger

    APP_CONFIG.set(ATTR_APP, "name", "imap-deck-sync")
    APP_CONFIG.set(ATTR_APP, "icon", "⭐")
    APP_CONFIG.set(ATTR_LOG, "log_path", os.path.expanduser("~/bin/logs/"))

    app_log = get_logger()
    app_log.silent = True
    app_log.set_level(app_log.DEBUG if args.verbose else app_log.INFO)
    app_log.start_logger(APP_CONFIG)

    # Bridge the stdlib `logging` calls from imap_deck_sync into the olen logger
    # by configuring the stdlib root logger to the same level. This keeps the
    # module-level `log = logging.getLogger(__name__)` working.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    remote_logger = RemoteLogger(APP_CONFIG)
```

Then change the `return run(cfg)` line near the end of `main()` to:
```python
    rc = run(cfg)
    if rc != 0:
        try:
            remote_logger.discord.error(
                title="imap-deck-sync failed",
                message=f"Exit code {rc}. See logs at ~/bin/logs/imap-deck-sync.log",
                app="imap-deck-sync",
                icon="⭐",
            )
        except Exception as e:
            print(f"Failed to send Discord alert: {e}", file=sys.stderr)
    return rc
```

- [ ] **Step 8.3: Smoke test that --help still works**

Run:
```bash
cd /home/olen/prog/nextcloud-deck-cli && ./nextcloud-deck-imap-sync.py --help
```

Expected: usage printed, exit 0.

- [ ] **Step 8.4: Make sure unit tests still pass**

Run:
```bash
cd /home/olen/prog/nextcloud-deck-cli && python3 -m pytest tests/ -v
```

Expected: 43 passed. (The CLI changes don't affect imap_deck_sync.py.)

- [ ] **Step 8.5: Commit**

Run:
```bash
git add nextcloud-deck-imap-sync.py
git commit -m "feat(sync): wire olen logger + Discord error alert"
```

---

## Task 9: Deployment artefacts — wrapper script + systemd user units

These are *files in the repo*. Moving them into chezmoi-managed paths is a follow-up the user does outside this plan.

**Files:**
- Create: `imap-deck-sync-wrapper.sh`
- Create: `systemd/imap-deck-sync.service`
- Create: `systemd/imap-deck-sync.timer`

- [ ] **Step 9.1: Create the wrapper script**

Create `imap-deck-sync-wrapper.sh`:
```bash
#!/bin/bash
# Resolve HOME if invoked from a context where it's unset (systemd cron-like).
if [ -z "$HOME" ]; then
    HOME=$(getent passwd "$(id -u)" | cut -d: -f6)
fi
PATH=$HOME/.local/bin/:$PATH

set -euo pipefail

# 1Password item names — adjust to match what you have in your vault.
NC_OP_ITEM="${NC_OP_ITEM:-nextcloud-olen-deck-app-password}"
IMAP_OP_ITEM="${IMAP_OP_ITEM:-apollo-imap-olen}"

NEXTCLOUD_PASSWORD="$(op --item="$NC_OP_ITEM" --field=password)"
IMAP_PASSWORD="$(op --item="$IMAP_OP_ITEM" --field=password)"

exec /home/olen/prog/nextcloud-deck-cli/nextcloud-deck-imap-sync.py \
    --url "${NEXTCLOUD_BASE_URL:-https://cloud.olen.net/}" \
    --username "${NEXTCLOUD_USERNAME:-olen}" \
    --password "$NEXTCLOUD_PASSWORD" \
    --board-id "${NEXTCLOUD_BOARD_ID:-4}" \
    --imap-host "${IMAP_HOST:-localhost}" \
    --imap-port "${IMAP_PORT:-993}" \
    --imap-user "${IMAP_USER:-olen}" \
    --imap-password "$IMAP_PASSWORD" \
    --imap-folder "${IMAP_FOLDER:-_Virtual/Important}" \
    "$@"
```

Make it executable:
```bash
chmod +x /home/olen/prog/nextcloud-deck-cli/imap-deck-sync-wrapper.sh
```

- [ ] **Step 9.2: Create the systemd unit files**

Create `systemd/imap-deck-sync.service`:
```ini
# Managed by chezmoi - do not edit directly
[Unit]
Description=Sync IMAP starred messages to/from Nextcloud Deck
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/home/olen/prog/nextcloud-deck-cli/imap-deck-sync-wrapper.sh
# StandardOutput / StandardError go to journal; the olen logger also writes to ~/bin/logs/.

[Install]
WantedBy=default.target
```

Create `systemd/imap-deck-sync.timer`:
```ini
# Managed by chezmoi - do not edit directly
[Unit]
Description=Run imap-deck-sync every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Unit=imap-deck-sync.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 9.3: Commit**

Run:
```bash
git add imap-deck-sync-wrapper.sh systemd/imap-deck-sync.service systemd/imap-deck-sync.timer
git commit -m "chore(sync): deployment artefacts (wrapper + systemd user units)"
```

---

## Task 10: Live smoke test (dry-run, then real)

This is the manual checklist the implementer (or olen) walks through to validate end-to-end. No code changes — but the plan would be incomplete without it.

- [ ] **Step 10.1: Verify the IMAP virtual folder shape**

Run (as olen):
```bash
python3 -c "
from imap_tools import MailBox
import getpass
pw = getpass.getpass('IMAP pwd: ')
with MailBox('localhost', port=993).login('olen', pw, initial_folder='_Virtual/Important') as mb:
    for m in mb.fetch(mark_seen=False, bulk=True, limit=3):
        print(m.uid, m.from_, m.subject)
        print('  message-id:', m.headers.get('message-id'))
"
```

Expected: prints up to 3 starred messages with non-empty Message-IDs.

- [ ] **Step 10.2: Find a single test star**

Pick one starred email you know about. Note its Message-ID and current From/Subject. If you have no starred mail right now, star one test message in Thunderbird first.

- [ ] **Step 10.3: Resolve passwords from 1Password and run a dry-run**

Run:
```bash
NEXTCLOUD_PASSWORD="$(op --item=nextcloud-olen-deck-app-password --field=password)"
IMAP_PASSWORD="$(op --item=apollo-imap-olen --field=password)"   # adjust item name as needed

cd /home/olen/prog/nextcloud-deck-cli && \
  ./nextcloud-deck-imap-sync.py \
    --url https://cloud.olen.net/ -u olen -p "$NEXTCLOUD_PASSWORD" -b 4 \
    --imap-host localhost --imap-port 993 \
    --imap-user olen --imap-password "$IMAP_PASSWORD" \
    --imap-folder "_Virtual/Important" \
    --dry-run --verbose
```

Expected: log lines saying "Found N starred message(s)…", "Plan: M action(s)", and `[dry-run] would create card in stack #... : 'Sender: Subject'` for each starred message that doesn't yet have a card. Exit code 0.

- [ ] **Step 10.4: First live run — drop `--dry-run`**

Run the same command without `--dry-run`. Check the Nextcloud Deck Todo board in your browser. Expected: one new card per starred message that didn't already have one, titled `Sender: Subject`, with the marker comment in its description.

- [ ] **Step 10.5: Test IMAP-unstar → Done direction**

In Thunderbird (or any IMAP client): unstar one of the messages that just got a card. Re-run the wrapper without `--dry-run`. Verify in Deck that the corresponding card moved from Todo to Done.

- [ ] **Step 10.6: Test Deck-Done → IMAP-unstar direction**

In Deck UI: drag one of the still-starred email-cards (currently in Todo) to the Done stack. Re-run the wrapper. In Thunderbird, refresh the `_Virtual/Important` folder — the message should have disappeared from it (its `\Flagged` flag was cleared).

- [ ] **Step 10.7: Idempotency check**

Run the wrapper twice in a row with nothing else changing. The second run should report `created=0 moved=0 unstarred=0 failures=0`.

- [ ] **Step 10.8: Install the systemd user units (optional, when you're confident)**

```bash
mkdir -p ~/.config/systemd/user
cp /home/olen/prog/nextcloud-deck-cli/systemd/imap-deck-sync.service ~/.config/systemd/user/
cp /home/olen/prog/nextcloud-deck-cli/systemd/imap-deck-sync.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now imap-deck-sync.timer
systemctl --user list-timers | grep imap-deck-sync
```

(Then move them into chezmoi management on your own schedule.)

---

## Self-Review

The following sections of the spec should each have a corresponding task:

- **Source of starred messages** (Dovecot virtual folder, configurable) → Task 7 (Config has `imap_folder` field, default `_Virtual/Important`).
- **Card identity (marker format, regex, manual-card invariant)** → Task 1.
- **Reconciliation algorithm (passes A/B/C, ordering invariant)** → Task 3.
- **Configuration table** → Task 7 (argparse + env-var fallback).
- **Deployment (systemd user timer)** → Task 9.
- **Error handling & logging (olen logger, Discord on run failure, dry-run)** → Tasks 6 (dry-run) + 8 (logger + Discord).
- **Behaviour decisions table** — verified per row in Task 3 tests (every row in §7 of the spec maps to a `TestMakePlan` case or to fetch_managed/execute_plan tests):
  - Doing → Done on unstar: `test_managed_card_in_doing_for_unstarred_message_moves_to_done`
  - Done with still-starred → unstar: `test_managed_card_in_done_with_still_starred_message_clears_flag`
  - Done with unstarred → no-op: `test_managed_card_in_done_with_unstarred_message_is_noop`
  - Active with still starred → no-op: `test_managed_card_in_todo_for_still_starred_message_is_noop`
  - Manual card (no marker) untouched: `test_skips_cards_without_marker` (in TestFetchManaged)
  - Duplicate Message-ID in IMAP: `test_first_message_wins_on_duplicate_message_id`
  - Missing Message-ID: `test_skips_messages_missing_message_id_header`
  - Card in custom stack: `test_managed_card_in_custom_stack_is_left_alone`
  - Title truncation: `test_truncates_at_200_chars`
  - Whitespace collapse: `test_collapses_internal_whitespace`
  - Missing required stack: handled in `run()` with explicit error log + exit 1 (Task 7)
- **Out-of-scope items** (IDLE, content edits back to email, multi-account, migration) — explicitly not covered. Good.

**Placeholder scan:** searched the plan for TBD, TODO, "implement later", "fill in", "similar to" — none present. All steps have either exact code or exact commands.

**Type/name consistency:** every dataclass/function name used in later tasks (`StarredMessage`, `ManagedCard`, `StackIds`, `UnstarAction`, `MoveToDoneAction`, `CreateCardAction`, `ExecutionSummary`, `Config`, `make_plan`, `fetch_starred`, `fetch_managed`, `execute_plan`, `run`, `parse_marker`, `build_marker`, `format_card_title`) is defined in an earlier task. The `mailbox.flag(uids, flag_set, value)` API matches imap_tools (`MailMessageFlags.FLAGGED`). The `deck.create_card(stack_id=, title=, description=)` signature matches the installed `olen_deck`.

---

**Plan complete and saved to `docs/superpowers/plans/2026-05-11-imap-deck-sync.md`.**
