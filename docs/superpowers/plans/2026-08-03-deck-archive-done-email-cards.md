# Auto-archive aged Email cards from Done — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Archive Deck cards that carry the `Email` label and have sat in the `Done` stack longer than a configurable number of days (default 7).

**Architecture:** A new pure planner `plan_archive()` produces `ArchiveAction`s that are appended to the plan `make_plan()` already returns, and executed by the existing `execute_plan()`. Time-in-Done comes from the Nextcloud Activity log, not `card.lastModified` (which is rewritten for the whole stack on every move). Two new additive methods land in the shared `olen_deck` package.

**Tech Stack:** Python 3.11+, `requests`, `pytest`, `imap-tools`, `olen-deck` (private Gitea PyPI).

**Spec:** `docs/superpowers/specs/2026-08-03-deck-archive-done-email-cards-design.md`

## Global Constraints

- `make_plan()` MUST NOT be modified. Its existing tests must remain green.
- Move events MUST be detected by the presence of `stackBefore` in `subject_rich[1]` — NEVER by string-matching `subject`. `"moved" in subject` also matches `"removed the tag"`, and `subject` is localised.
- Activity timestamps are offset-aware UTC (`2026-06-30T08:55:54+00:00`). Parse aware, compare as epoch ints.
- HTTP **304** from the activity endpoint means "no data", not an error. Return `[]`; never call `.json()` on it.
- Threshold config: `--archive-done-after-days` / env `ARCHIVE_DONE_AFTER_DAYS`, default `7`. `<= 0` disables the pass. `>= 25` is a hard startup error (activity retention is 30 days).
- Eligibility is the **Email label only**. The imap-sync marker is NOT required.
- A card in `Done` with no "moved to Done" record is skipped silently.
- `olen_deck` changes must be additive — SmartList also consumes this package.
- Repo has no CI. A locally passing `pytest` is the gate.
- Two repos are touched: `~/prog/python-modules-olen` (Tasks 1-3) and `~/prog/nextcloud-deck-cli` (Tasks 4-9).
- Work in `~/prog/nextcloud-deck-cli` happens on branch `feat/archive-done-email-cards` (already created).

---

### Task 1: `olen_deck.archive_card()`

**Files:**
- Modify: `~/prog/python-modules-olen/olen_deck/olen_deck/client.py` (after `delete_card`, ~line 137)
- Create: `~/prog/python-modules-olen/olen_deck/tests/test_client.py`

**Interfaces:**
- Consumes: `DeckClient._bid`, `DeckClient._get_json`, `DeckClient.api_base`, `parse_card` (all existing)
- Produces: `DeckClient.archive_card(stack_id: int, card_id: int, board_id: int | None = None) -> Card`

Route confirmed present in Deck 1.17.3 (`appinfo/routes.php:108`). The controller signature is `archive(int $cardId)` — it takes **no request body**.

- [ ] **Step 1: Write the failing test**

Create `~/prog/python-modules-olen/olen_deck/tests/test_client.py`:

```python
"""Tests for DeckClient HTTP surface (session mocked, no network)."""
from unittest.mock import MagicMock

from olen_deck.client import DeckClient


def make_client() -> DeckClient:
    c = DeckClient("https://cloud.example.com/", "user", "pw", board_id=4)
    c.session = MagicMock()
    return c


class TestArchiveCard:
    def test_puts_to_v1_archive_route(self):
        c = make_client()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"id": 319, "title": "Some card", "archived": True}
        c.session.put.return_value = resp

        card = c.archive_card(stack_id=9, card_id=319)

        url = c.session.put.call_args[0][0]
        assert url == (
            "https://cloud.example.com/index.php/apps/deck/api/v1.0"
            "/boards/4/stacks/9/cards/319/archive"
        )
        assert card.id == 319
        assert card.archived is True

    def test_sends_no_body(self):
        c = make_client()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"id": 1, "title": "x"}
        c.session.put.return_value = resp

        c.archive_card(stack_id=9, card_id=1)

        assert "json" not in c.session.put.call_args.kwargs
        assert "data" not in c.session.put.call_args.kwargs

    def test_explicit_board_id_overrides_bound_board(self):
        c = make_client()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"id": 1, "title": "x"}
        c.session.put.return_value = resp

        c.archive_card(stack_id=9, card_id=1, board_id=77)

        assert "/boards/77/" in c.session.put.call_args[0][0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/prog/python-modules-olen/olen_deck && python -m pytest tests/test_client.py -v`
Expected: FAIL — `AttributeError: 'DeckClient' object has no attribute 'archive_card'`

- [ ] **Step 3: Write minimal implementation**

In `client.py`, immediately after `delete_card` and before `move_card`:

```python
    def archive_card(self, stack_id: int, card_id: int,
                     board_id: int | None = None) -> Card:
        """Archive a card. Reversible via the matching unarchive route."""
        bid = self._bid(board_id)
        resp = self.session.put(
            f"{self.api_base}/boards/{bid}/stacks/{stack_id}/cards/{card_id}/archive",
            timeout=self.timeout,
        )
        return parse_card(self._get_json(resp), stack_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/prog/python-modules-olen/olen_deck && python -m pytest tests/ -v`
Expected: PASS (new tests plus existing `test_models.py`)

- [ ] **Step 5: Commit**

```bash
cd ~/prog/python-modules-olen
git add olen_deck/olen_deck/client.py olen_deck/tests/test_client.py
git commit -m "feat(deck): add archive_card()

Archives a card via PUT /api/v1.0/boards/{b}/stacks/{s}/cards/{c}/archive.
The Deck controller takes only the route param, so no body is sent."
```

---

### Task 2: `olen_deck.get_deck_activity()`

**Files:**
- Modify: `~/prog/python-modules-olen/olen_deck/olen_deck/client.py` (new `# --- Activity ---` section at end of class)
- Modify: `~/prog/python-modules-olen/olen_deck/tests/test_client.py`

**Interfaces:**
- Consumes: `DeckClient.session`, `DeckClient.base_url`, `DeckClient._get_json`
- Produces: `DeckClient.get_deck_activity(limit: int = 200, since: int | None = None) -> list[dict]`

This endpoint lives under `/ocs/v2.php`, NOT under `api_base`. It returns HTTP 304 with an empty body when there is no data — `requests.raise_for_status()` does not raise on 304, so `.json()` would blow up on the empty body. The 304 check must come first.

- [ ] **Step 1: Write the failing test**

Append to `~/prog/python-modules-olen/olen_deck/tests/test_client.py`:

```python
class TestGetDeckActivity:
    def test_returns_empty_list_on_304(self):
        c = make_client()
        c.session.get.return_value = MagicMock(status_code=304)

        assert c.get_deck_activity() == []

    def test_does_not_parse_body_on_304(self):
        c = make_client()
        resp = MagicMock(status_code=304)
        resp.json.side_effect = ValueError("no body")
        c.session.get.return_value = resp

        assert c.get_deck_activity() == []
        resp.json.assert_not_called()

    def test_unwraps_ocs_envelope(self):
        c = make_client()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"ocs": {"data": [{"activity_id": 1}]}}
        c.session.get.return_value = resp

        assert c.get_deck_activity() == [{"activity_id": 1}]

    def test_hits_the_ocs_deck_activity_route(self):
        c = make_client()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"ocs": {"data": []}}
        c.session.get.return_value = resp

        c.get_deck_activity()

        url = c.session.get.call_args[0][0]
        assert url == (
            "https://cloud.example.com/ocs/v2.php/apps/activity/api/v2/activity/deck"
        )

    def test_passes_limit_and_since(self):
        c = make_client()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"ocs": {"data": []}}
        c.session.get.return_value = resp

        c.get_deck_activity(limit=50, since=1234)

        params = c.session.get.call_args.kwargs["params"]
        assert params["limit"] == 50
        assert params["since"] == 1234
        assert params["format"] == "json"

    def test_omits_since_when_none(self):
        c = make_client()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"ocs": {"data": []}}
        c.session.get.return_value = resp

        c.get_deck_activity()

        assert "since" not in c.session.get.call_args.kwargs["params"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/prog/python-modules-olen/olen_deck && python -m pytest tests/test_client.py::TestGetDeckActivity -v`
Expected: FAIL — `AttributeError: 'DeckClient' object has no attribute 'get_deck_activity'`

- [ ] **Step 3: Write minimal implementation**

At the end of the `DeckClient` class in `client.py`, after `get_board_labels`:

```python
    # --- Activity ---

    def get_deck_activity(self, limit: int = 200,
                          since: int | None = None) -> list[dict]:
        """
        Return Deck activity entries for the authenticated user, newest first.

        Spans every board the user can see, so callers must filter on board id.
        The endpoint answers HTTP 304 with an empty body when there is nothing
        to return; that is "no data", not an error.
        """
        url = f"{self.base_url}/ocs/v2.php/apps/activity/api/v2/activity/deck"
        params: dict[str, Any] = {"format": "json", "limit": limit}
        if since is not None:
            params["since"] = since

        resp = self.session.get(url, params=params, timeout=self.timeout)
        if resp.status_code == 304:
            return []
        return self._get_json(resp)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/prog/python-modules-olen/olen_deck && python -m pytest tests/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd ~/prog/python-modules-olen
git add olen_deck/olen_deck/client.py olen_deck/tests/test_client.py
git commit -m "feat(deck): add get_deck_activity()

Reads /ocs/v2.php/apps/activity/api/v2/activity/deck using the client's
existing authenticated session. Returns [] on HTTP 304, which is how this
endpoint signals 'no data' - parsing the empty body as JSON raises."
```

---

### Task 3: Publish `olen-deck` 0.3.0

**Files:**
- Modify: `~/prog/python-modules-olen/olen_deck/pyproject.toml`

**Interfaces:**
- Consumes: `archive_card` (Task 1), `get_deck_activity` (Task 2)
- Produces: `olen-deck==0.3.0` on the Gitea PyPI, installed system-wide

`olen-deck` is currently installed system-wide at `/usr/local/lib/python3.13/site-packages` (v0.2.1), matching how `update.sh` deploys. Do NOT run `update.sh` itself — it rebuilds and re-uploads all four packages, and twine rejects duplicate versions for the three that did not change.

- [ ] **Step 1: Bump the version**

In `~/prog/python-modules-olen/olen_deck/pyproject.toml` change:

```toml
version = "0.2.1"
```

to:

```toml
version = "0.3.0"
```

- [ ] **Step 2: Run the full package test suite**

Run: `cd ~/prog/python-modules-olen/olen_deck && python -m pytest tests/ -v`
Expected: PASS

- [ ] **Step 3: Build and upload only this package**

```bash
cd ~/prog/python-modules-olen/olen_deck
rm -rf dist/ build/ *.egg-info
python3 -m build
python3 -m twine upload --repository gitea dist/*
```

- [ ] **Step 4: Reinstall system-wide and verify**

```bash
sudo python3 -m pip install --upgrade \
  --extra-index-url https://git.olen.net/api/packages/Olen/pypi/simple/ olen-deck
python3 -c "import olen_deck, inspect; from olen_deck import DeckClient; \
print(hasattr(DeckClient,'archive_card'), hasattr(DeckClient,'get_deck_activity'))"
pip show olen-deck | grep Version
```

Expected: `True True` and `Version: 0.3.0`

- [ ] **Step 5: Commit**

```bash
cd ~/prog/python-modules-olen
git add olen_deck/pyproject.toml
git commit -m "chore(deck): release olen-deck 0.3.0

Adds archive_card() and get_deck_activity()."
```

---

### Task 4: `ArchiveAction` and `latest_done_at()`

**Files:**
- Modify: `~/prog/nextcloud-deck-cli/imap_deck_sync.py` (dataclasses ~line 105, new function after `make_plan`)
- Modify: `~/prog/nextcloud-deck-cli/tests/test_imap_deck_sync.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces:
  - `ArchiveAction(stack_id: int, card_id: int, card_title: str, done_at: int)` — frozen dataclass
  - `latest_done_at(entries: list[dict], board_id: int, done_stack_id: int) -> dict[int, int]`
  - `Action` union extended with `ArchiveAction`

`entries` are newest-first, so the FIRST match per card wins.

- [ ] **Step 1: Write the failing test**

Append to `~/prog/nextcloud-deck-cli/tests/test_imap_deck_sync.py`:

```python
def activity_entry(card_id, when, board="4", stack="9",
                   stack_before="22", object_type="deck_card", subject="You have moved the card X"):
    """Build an activity entry shaped like the real Nextcloud response."""
    params = {"board": {"id": board}, "stack": {"id": stack}}
    if stack_before is not None:
        params["stackBefore"] = {"id": stack_before}
    return {
        "object_type": object_type,
        "object_id": card_id,
        "datetime": when,
        "subject": subject,
        "subject_rich": ["template", params],
    }


class TestLatestDoneAt:
    def test_extracts_move_to_done(self):
        entries = [activity_entry(319, "2026-07-29T12:31:01+00:00")]
        assert latest_done_at(entries, board_id=4, done_stack_id=9) == {319: 1785328261}

    def test_newest_first_wins_for_repeated_moves(self):
        entries = [
            activity_entry(7, "2026-07-29T12:31:01+00:00"),
            activity_entry(7, "2026-06-01T00:00:00+00:00"),
        ]
        assert latest_done_at(entries, 4, 9) == {7: 1785328261}

    def test_ignores_other_boards(self):
        entries = [activity_entry(7, "2026-07-29T12:31:01+00:00", board="8")]
        assert latest_done_at(entries, 4, 9) == {}

    def test_ignores_other_stacks(self):
        entries = [activity_entry(7, "2026-07-29T12:31:01+00:00", stack="22")]
        assert latest_done_at(entries, 4, 9) == {}

    def test_ignores_entries_without_stack_before(self):
        # "You have removed the tag X from card Y in list Done" - has stack, no stackBefore
        entries = [activity_entry(7, "2026-07-29T12:31:01+00:00", stack_before=None,
                                  subject="You have removed the tag Email from card Y")]
        assert latest_done_at(entries, 4, 9) == {}

    def test_does_not_string_match_subject(self):
        # Guards the "removed" substring trap: no stackBefore means not a move,
        # even though the subject contains the letters "moved".
        entries = [activity_entry(7, "2026-07-29T12:31:01+00:00", stack_before=None,
                                  subject="You have removed the tag 10 from card Coca Cola")]
        assert latest_done_at(entries, 4, 9) == {}

    def test_ignores_non_card_object_types(self):
        entries = [activity_entry(7, "2026-07-29T12:31:01+00:00", object_type="deck_board")]
        assert latest_done_at(entries, 4, 9) == {}

    def test_empty_input(self):
        assert latest_done_at([], 4, 9) == {}

    def test_malformed_entries_are_skipped_not_fatal(self):
        entries = [
            {"object_type": "deck_card", "object_id": 1},                      # no subject_rich
            {"object_type": "deck_card", "object_id": 2, "subject_rich": []},   # empty
            {"object_type": "deck_card", "object_id": 3, "subject_rich": ["t"]},  # no params
            {"object_type": "deck_card", "object_id": 4, "subject_rich": ["t", None]},
            activity_entry(9, "not-a-date"),
            activity_entry(10, "2026-07-29T12:31:01+00:00"),
        ]
        assert latest_done_at(entries, 4, 9) == {10: 1785328261}

    def test_naive_datetime_is_treated_as_utc(self):
        entries = [activity_entry(11, "2026-07-29T12:31:01")]
        assert latest_done_at(entries, 4, 9) == {11: 1785328261}

    def test_ids_may_be_strings_or_ints(self):
        entries = [activity_entry(12, "2026-07-29T12:31:01+00:00", board=4, stack=9)]
        assert latest_done_at(entries, 4, 9) == {12: 1785328261}
```

Add `latest_done_at` and `ArchiveAction` to the import block at the top of the test file.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/prog/nextcloud-deck-cli && python -m pytest tests/test_imap_deck_sync.py::TestLatestDoneAt -v`
Expected: FAIL — `ImportError: cannot import name 'latest_done_at'`

- [ ] **Step 3: Write minimal implementation**

Add to the imports at the top of `imap_deck_sync.py`:

```python
from datetime import datetime, timezone
```

Add the dataclass after `AssignLabelAction` (~line 111) and extend the union:

```python
@dataclass(frozen=True)
class ArchiveAction:
    """Archive a Deck card that has aged out of the Done stack."""
    stack_id: int
    card_id: int
    card_title: str
    done_at: int


Action = (
    UnstarAction | MoveToDoneAction | CreateCardAction
    | AssignLabelAction | ArchiveAction
)
```

Add after `make_plan()`:

```python
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
        if card_id is None or card_id in out:
            continue

        ts = _parse_activity_datetime(entry.get("datetime"))
        if ts is None:
            continue
        out[card_id] = ts

    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/prog/nextcloud-deck-cli && python -m pytest tests/ -v`
Expected: PASS (new class plus all pre-existing tests)

- [ ] **Step 5: Commit**

```bash
cd ~/prog/nextcloud-deck-cli
git add imap_deck_sync.py tests/test_imap_deck_sync.py
git commit -m "feat(sync): add ArchiveAction and latest_done_at()

Resolves each card's most recent move into Done from the Nextcloud activity
feed. Move events are detected structurally via stackBefore rather than by
string-matching the localised subject, which would also match 'removed'."
```

---

### Task 5: `plan_archive()`

**Files:**
- Modify: `~/prog/nextcloud-deck-cli/imap_deck_sync.py` (after `latest_done_at`)
- Modify: `~/prog/nextcloud-deck-cli/tests/test_imap_deck_sync.py`

**Interfaces:**
- Consumes: `ArchiveAction` (Task 4)
- Produces: `plan_archive(done_cards, done_at: dict[int, int], email_label_id: int, now: int, max_age_days: int) -> list[ArchiveAction]`

`done_cards` are `olen_deck.Card` objects taken from the Done stack; each has `.id`, `.title`, `.stack_id` (set by `parse_stack`) and `.labels` (a list of objects with `.id`). `now` is a parameter — never read the clock inside the planner.

- [ ] **Step 1: Write the failing test**

Append to `~/prog/nextcloud-deck-cli/tests/test_imap_deck_sync.py`:

```python
DAY = 86400
NOW = 1785328261


def done_card(card_id, label_ids=(145,), title="A card", stack_id=9):
    return SimpleNamespace(
        id=card_id,
        title=title,
        stack_id=stack_id,
        labels=[SimpleNamespace(id=lid) for lid in label_ids],
    )


class TestPlanArchive:
    def test_archives_card_past_threshold(self):
        cards = [done_card(1)]
        done_at = {1: NOW - 8 * DAY}
        actions = plan_archive(cards, done_at, email_label_id=145,
                               now=NOW, max_age_days=7)
        assert actions == [ArchiveAction(stack_id=9, card_id=1,
                                         card_title="A card",
                                         done_at=NOW - 8 * DAY)]

    def test_exact_boundary_archives(self):
        actions = plan_archive([done_card(1)], {1: NOW - 7 * DAY},
                               email_label_id=145, now=NOW, max_age_days=7)
        assert len(actions) == 1

    def test_one_second_under_threshold_waits(self):
        actions = plan_archive([done_card(1)], {1: NOW - 7 * DAY + 1},
                               email_label_id=145, now=NOW, max_age_days=7)
        assert actions == []

    def test_card_without_email_label_is_skipped(self):
        actions = plan_archive([done_card(1, label_ids=(999,))], {1: NOW - 30 * DAY},
                               email_label_id=145, now=NOW, max_age_days=7)
        assert actions == []

    def test_card_with_no_labels_is_skipped(self):
        actions = plan_archive([done_card(1, label_ids=())], {1: NOW - 30 * DAY},
                               email_label_id=145, now=NOW, max_age_days=7)
        assert actions == []

    def test_card_without_done_at_record_is_skipped(self):
        actions = plan_archive([done_card(1)], {},
                               email_label_id=145, now=NOW, max_age_days=7)
        assert actions == []

    def test_zero_days_disables_pass(self):
        actions = plan_archive([done_card(1)], {1: NOW - 99 * DAY},
                               email_label_id=145, now=NOW, max_age_days=0)
        assert actions == []

    def test_negative_days_disables_pass(self):
        actions = plan_archive([done_card(1)], {1: NOW - 99 * DAY},
                               email_label_id=145, now=NOW, max_age_days=-1)
        assert actions == []

    def test_mixed_set_yields_only_eligible(self):
        cards = [
            done_card(1),                                  # eligible
            done_card(2, label_ids=(999,)),                # wrong label
            done_card(3),                                  # too recent
            done_card(4),                                  # no record
        ]
        done_at = {1: NOW - 10 * DAY, 2: NOW - 10 * DAY, 3: NOW - 1 * DAY}
        actions = plan_archive(cards, done_at, email_label_id=145,
                               now=NOW, max_age_days=7)
        assert [a.card_id for a in actions] == [1]

    def test_uses_each_cards_own_stack_id(self):
        actions = plan_archive([done_card(1, stack_id=42)], {1: NOW - 10 * DAY},
                               email_label_id=145, now=NOW, max_age_days=7)
        assert actions[0].stack_id == 42

    def test_empty_inputs(self):
        assert plan_archive([], {}, email_label_id=145, now=NOW, max_age_days=7) == []
```

Add `plan_archive` to the test file's import block.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/prog/nextcloud-deck-cli && python -m pytest tests/test_imap_deck_sync.py::TestPlanArchive -v`
Expected: FAIL — `ImportError: cannot import name 'plan_archive'`

- [ ] **Step 3: Write minimal implementation**

Add after `latest_done_at` in `imap_deck_sync.py`:

```python
def plan_archive(done_cards, done_at: dict[int, int], email_label_id: int,
                 now: int, max_age_days: int) -> list[ArchiveAction]:
    """
    Pure planner for the archive pass.

    A card is archived when all of the following hold:
      1. it is in the Done stack (the caller passes only those)
      2. it carries `email_label_id`
      3. `done_at` has a record for it — otherwise skip silently, the user
         archives those by hand
      4. it has been in Done for at least `max_age_days`

    `max_age_days <= 0` disables the pass entirely.
    """
    if max_age_days <= 0:
        return []

    cutoff_seconds = max_age_days * 86400
    actions: list[ArchiveAction] = []

    for card in done_cards or []:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/prog/nextcloud-deck-cli && python -m pytest tests/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd ~/prog/nextcloud-deck-cli
git add imap_deck_sync.py tests/test_imap_deck_sync.py
git commit -m "feat(sync): add plan_archive() pure planner

Eligibility is the Email label plus a known move-to-Done timestamp older
than the threshold. 'now' is injected so the time rule is testable without
freezing the clock."
```

---

### Task 6: Execute `ArchiveAction`

**Files:**
- Modify: `~/prog/nextcloud-deck-cli/imap_deck_sync.py` (`ExecutionSummary` ~line 191, `execute_plan` ~line 274)
- Modify: `~/prog/nextcloud-deck-cli/tests/test_imap_deck_sync.py`

**Interfaces:**
- Consumes: `ArchiveAction` (Task 4); the module-level test helpers `NOW`, `DAY` and `done_card()` added to the test file in Task 5 — Task 5 must land first
- Produces: `ExecutionSummary.archived: int`; `execute_plan` handles `ArchiveAction` by calling `deck.archive_card(stack_id=..., card_id=...)`

- [ ] **Step 1: Write the failing test**

Append to `~/prog/nextcloud-deck-cli/tests/test_imap_deck_sync.py`:

```python
class TestExecuteArchiveAction:
    def _deck(self):
        return SimpleNamespace(archive_card=MagicMock(return_value=None))

    def test_calls_archive_card_with_stack_and_id(self):
        deck = self._deck()
        plan = [ArchiveAction(stack_id=9, card_id=319,
                              card_title="X", done_at=NOW - 10 * DAY)]

        summary = execute_plan(plan=plan, mailbox=None, deck=deck, dry_run=False)

        deck.archive_card.assert_called_once_with(stack_id=9, card_id=319)
        assert summary.archived == 1
        assert summary.failures == 0

    def test_dry_run_makes_no_api_call(self):
        deck = self._deck()
        plan = [ArchiveAction(stack_id=9, card_id=319,
                              card_title="X", done_at=NOW - 10 * DAY)]

        summary = execute_plan(plan=plan, mailbox=None, deck=deck, dry_run=True)

        deck.archive_card.assert_not_called()
        assert summary.archived == 1

    def test_failure_is_counted_and_others_still_run(self):
        deck = SimpleNamespace(
            archive_card=MagicMock(side_effect=[RuntimeError("boom"), None])
        )
        plan = [
            ArchiveAction(stack_id=9, card_id=1, card_title="A", done_at=1),
            ArchiveAction(stack_id=9, card_id=2, card_title="B", done_at=1),
        ]

        summary = execute_plan(plan=plan, mailbox=None, deck=deck, dry_run=False)

        assert deck.archive_card.call_count == 2
        assert summary.archived == 1
        assert summary.failures == 1
```

Add `from unittest.mock import MagicMock` to the test file's imports if not already present.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/prog/nextcloud-deck-cli && python -m pytest tests/test_imap_deck_sync.py::TestExecuteArchiveAction -v`
Expected: FAIL — `execute_plan` logs "Unknown action type 'ArchiveAction'" and `summary.archived` does not exist

- [ ] **Step 3: Write minimal implementation**

Add the counter to `ExecutionSummary`:

```python
@dataclass
class ExecutionSummary:
    """Counts of actions actually applied during execute_plan."""
    created: int = 0
    moved: int = 0
    unstarred: int = 0
    labels_assigned: int = 0
    archived: int = 0
    failures: int = 0
```

In `execute_plan`, insert a branch after the `AssignLabelAction` branch and before the final `else`:

```python
            elif isinstance(action, ArchiveAction):
                if dry_run:
                    log.info("[dry-run] would archive card #%s %r (in Done since %s)",
                             action.card_id, action.card_title, action.done_at)
                else:
                    log.info("Archiving card #%s %r (in Done since %s)",
                             action.card_id, action.card_title, action.done_at)
                    deck.archive_card(stack_id=action.stack_id, card_id=action.card_id)
                summary.archived += 1
```

Also extend the docstring's `deck` duck-type line to mention `.archive_card(...)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/prog/nextcloud-deck-cli && python -m pytest tests/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd ~/prog/nextcloud-deck-cli
git add imap_deck_sync.py tests/test_imap_deck_sync.py
git commit -m "feat(sync): execute ArchiveAction and count archived cards

Honours --dry-run and the existing per-action failure semantics."
```

---

### Task 7: Config, CLI flag and startup validation

**Files:**
- Modify: `~/prog/nextcloud-deck-cli/imap_deck_sync.py` (`Config` ~line 382, module constant)
- Modify: `~/prog/nextcloud-deck-cli/nextcloud-deck-imap-sync.py` (argparse + validation)
- Modify: `~/prog/nextcloud-deck-cli/tests/test_imap_deck_sync.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Config.archive_done_after_days: int = 7`; module constant `ARCHIVE_MAX_DAYS_LIMIT = 25`; CLI flag `--archive-done-after-days`; env `ARCHIVE_DONE_AFTER_DAYS`

`>= 25` must be a hard startup error: activity retention is 30 days, so records would expire before a card could ever qualify and archiving would silently stop.

- [ ] **Step 1: Write the failing test**

Append to `~/prog/nextcloud-deck-cli/tests/test_imap_deck_sync.py`:

```python
class TestArchiveThresholdLimit:
    def test_limit_constant_is_below_activity_retention(self):
        # Nextcloud activity_expire_days is 30; the limit must leave headroom.
        assert ARCHIVE_MAX_DAYS_LIMIT < 30

    def test_config_defaults_to_seven_days(self):
        cfg = Config(
            nc_url="u", nc_username="u", nc_password="p", nc_board_id=4,
            todo_stack_name="Todo", doing_stack_name="Doing", done_stack_name="Done",
            email_label_name="Email", email_label_color="808080",
            imap_host="h", imap_port=993, imap_user="u", imap_password="p",
            imap_folder="_Virtual/Important",
        )
        assert cfg.archive_done_after_days == 7
```

Add `ARCHIVE_MAX_DAYS_LIMIT` and `Config` to the test file's import block.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/prog/nextcloud-deck-cli && python -m pytest tests/test_imap_deck_sync.py::TestArchiveThresholdLimit -v`
Expected: FAIL — `ImportError: cannot import name 'ARCHIVE_MAX_DAYS_LIMIT'`

- [ ] **Step 3: Write minimal implementation**

In `imap_deck_sync.py`, add near `MARKER_RE` at the top:

```python
# Nextcloud's activity_expire_days is 30 on this deployment. A threshold at or
# near that value means records expire before a card can qualify, so archiving
# would silently stop. Refuse thresholds that leave no headroom.
ARCHIVE_MAX_DAYS_LIMIT = 25
```

Add the field to `Config` (after `imap_folder`, before `dry_run`):

```python
    archive_done_after_days: int = 7
```

In `nextcloud-deck-imap-sync.py`, add the argument after `--imap-folder`:

```python
    p.add_argument("--archive-done-after-days", type=int,
                   default=int(env("ARCHIVE_DONE_AFTER_DAYS", "7") or 7),
                   help="Archive Email-labelled cards that have been in Done this "
                        "many days. 0 or less disables the pass.")
```

Add validation immediately after the existing `missing` check block:

```python
    if args.archive_done_after_days >= ARCHIVE_MAX_DAYS_LIMIT:
        print(
            f"--archive-done-after-days must be below {ARCHIVE_MAX_DAYS_LIMIT} "
            f"(got {args.archive_done_after_days}). Nextcloud keeps only 30 days "
            f"of activity, so a larger window would silently archive nothing.",
            file=sys.stderr,
        )
        return 2
```

Import the constant at the top of `nextcloud-deck-imap-sync.py`:

```python
from imap_deck_sync import ARCHIVE_MAX_DAYS_LIMIT, Config, run  # noqa: E402
```

Pass it into `Config(...)`:

```python
        archive_done_after_days=args.archive_done_after_days,
```

- [ ] **Step 4: Run tests and exercise the guard**

Run: `cd ~/prog/nextcloud-deck-cli && python -m pytest tests/ -v`
Expected: PASS

Run: `cd ~/prog/nextcloud-deck-cli && ./nextcloud-deck-imap-sync.py --url x --username u --password p --board-id 4 --imap-user u --imap-password p --archive-done-after-days 30; echo "exit=$?"`
Expected: the guard message on stderr and `exit=2`

- [ ] **Step 5: Commit**

```bash
cd ~/prog/nextcloud-deck-cli
git add imap_deck_sync.py nextcloud-deck-imap-sync.py tests/test_imap_deck_sync.py
git commit -m "feat(sync): add --archive-done-after-days with retention guard

Defaults to 7. Values <= 0 disable the pass; values >= 25 are rejected at
startup because Nextcloud only retains 30 days of activity."
```

---

### Task 8: Wire the archive pass into `run()`

**Files:**
- Modify: `~/prog/nextcloud-deck-cli/imap_deck_sync.py` (`run()` ~lines 477-509, new `fetch_deck_activity` helper)
- Modify: `~/prog/nextcloud-deck-cli/requirements.txt`
- Modify: `~/prog/nextcloud-deck-cli/tests/test_imap_deck_sync.py`

**Interfaces:**
- Consumes: `latest_done_at` (Task 4), `plan_archive` (Task 5), `ArchiveAction` execution (Task 6), `Config.archive_done_after_days` (Task 7), `DeckClient.get_deck_activity` (Task 2)
- Produces: `fetch_deck_activity(deck, page_size: int = 200, max_pages: int = 20) -> list[dict]`

The activity fetch happens BEFORE the IMAP block, because it is Deck-side. Cards moved to Done during this same run therefore have no record yet and are skipped — they archive on a later run.

- [ ] **Step 1: Write the failing test**

Append to `~/prog/nextcloud-deck-cli/tests/test_imap_deck_sync.py`:

```python
class TestFetchDeckActivity:
    def test_single_page(self):
        deck = SimpleNamespace(get_deck_activity=MagicMock(return_value=[
            {"activity_id": 3}, {"activity_id": 2},
        ]))
        assert len(fetch_deck_activity(deck, page_size=200)) == 2
        deck.get_deck_activity.assert_called_once_with(limit=200, since=None)

    def test_pages_until_short_page(self):
        pages = [
            [{"activity_id": i} for i in (5, 4)],
            [{"activity_id": 3}],
        ]
        deck = SimpleNamespace(get_deck_activity=MagicMock(side_effect=pages))
        assert len(fetch_deck_activity(deck, page_size=2)) == 3
        assert deck.get_deck_activity.call_args_list[1].kwargs["since"] == 4

    def test_stops_at_max_pages(self):
        deck = SimpleNamespace(
            get_deck_activity=MagicMock(return_value=[{"activity_id": 1}, {"activity_id": 1}])
        )
        fetch_deck_activity(deck, page_size=2, max_pages=3)
        assert deck.get_deck_activity.call_count == 3

    def test_empty_first_page(self):
        deck = SimpleNamespace(get_deck_activity=MagicMock(return_value=[]))
        assert fetch_deck_activity(deck) == []
```

Add `fetch_deck_activity` to the test file's import block.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/prog/nextcloud-deck-cli && python -m pytest tests/test_imap_deck_sync.py::TestFetchDeckActivity -v`
Expected: FAIL — `ImportError: cannot import name 'fetch_deck_activity'`

- [ ] **Step 3: Write minimal implementation**

Add before `run()` in `imap_deck_sync.py`:

```python
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
```

In `run()`, insert after the `managed = fetch_managed(stacks)` log line and before the `try:` that opens the mailbox:

```python
    archive_actions: list[ArchiveAction] = []
    activity_failures = 0
    if config.archive_done_after_days > 0:
        try:
            activity = fetch_deck_activity(deck)
        except Exception as e:
            log.error("Failed to fetch Deck activity; skipping archive pass: %s", e)
            activity_failures = 1
        else:
            done_at = latest_done_at(activity, config.nc_board_id, done.id)
            archive_actions = plan_archive(
                done_cards=getattr(done, "cards", None) or [],
                done_at=done_at,
                email_label_id=email_label.id,
                now=int(time.time()),
                max_age_days=config.archive_done_after_days,
            )
            log.info("Archive pass: %d of %d card(s) in %s eligible",
                     len(archive_actions), len(getattr(done, "cards", None) or []),
                     config.done_stack_name)
```

Add `import time` to the module imports.

Change the plan assembly to append the archive actions:

```python
            plan = make_plan(
                starred=starred,
                managed=managed,
                stack_ids=stack_ids,
                email_label_id=email_label.id,
            ) + archive_actions
```

After `execute_plan(...)` returns, fold in the activity failure and extend the summary log:

```python
    summary.failures += activity_failures

    log.info(
        "Done: created=%d moved=%d unstarred=%d labels_assigned=%d "
        "archived=%d failures=%d%s",
        summary.created, summary.moved, summary.unstarred,
        summary.labels_assigned, summary.archived, summary.failures,
        " (dry-run)" if config.dry_run else "",
    )
    return 0 if summary.failures == 0 else 2
```

Note `summary.failures += activity_failures` must sit AFTER the `except` block that returns 1, alongside the existing final log call.

- [ ] **Step 4: Run the whole suite**

Run: `cd ~/prog/nextcloud-deck-cli && python -m pytest tests/ -v`
Expected: PASS, including all pre-existing tests

- [ ] **Step 5: Pin the dependency**

Replace the `olen_deck` comment line in `requirements.txt`:

```
imap-tools>=1.0
# olen_deck is installed from the private Olen PyPI index
olen-deck>=0.3.0
# olen is installed from the private Olen PyPI index
```

- [ ] **Step 6: Commit**

```bash
cd ~/prog/nextcloud-deck-cli
git add imap_deck_sync.py requirements.txt tests/test_imap_deck_sync.py
git commit -m "feat(sync): wire archive pass into run()

Fetches the activity window before the IMAP block, resolves move-to-Done
timestamps, and appends ArchiveActions to the existing plan. An activity
fetch failure skips the pass and yields rc=2 rather than failing silently."
```

---

### Task 9: Verify against the live board

**Files:** none modified

**Interfaces:**
- Consumes: everything from Tasks 1-8

Expected outcome as measured 2026-08-03: the Done stack holds 28 cards, 17 Email-labelled, and only card #319 has a usable record (4 days old). **The dry run should report 0 cards to archive.** Anything else means the eligibility logic is wrong — investigate before proceeding.

- [ ] **Step 1: Dry run and read the output**

```bash
cd ~/prog/nextcloud-deck-cli
./imap-deck-sync-wrapper.sh --dry-run -v 2>&1 | grep -E 'Archive pass|would archive|Done:'
```

Expected: an `Archive pass: 0 of 28 card(s) in Done eligible` line, `archived=0`, and no `would archive` lines.

- [ ] **Step 2: Prove the plumbing works with a low threshold**

Still a dry run, so nothing is mutated:

```bash
./imap-deck-sync-wrapper.sh --dry-run -v --archive-done-after-days 1 2>&1 \
  | grep -E 'Archive pass|would archive'
```

Expected: card #319 appears as `[dry-run] would archive card #319`. This confirms activity lookup, label filtering and threshold maths all work end to end.

- [ ] **Step 3: Confirm the retention guard fires**

```bash
./imap-deck-sync-wrapper.sh --archive-done-after-days 30; echo "exit=$?"
```

Expected: guard message on stderr, `exit=2`, no API calls.

- [ ] **Step 4: Let one real timer cycle run**

```bash
systemctl --user list-timers imap-deck-sync.timer
journalctl --user -u imap-deck-sync.service --since '10 min ago' \
  | grep -E 'Archive pass|Done:'
```

Expected: `failures=0`, `archived=0`. No wrapper or systemd change is needed — the default is 7 and the wrapper already forwards `"$@"`.

- [ ] **Step 5: Merge to main**

```bash
cd ~/prog/nextcloud-deck-cli
git checkout main
git merge --no-ff feat/archive-done-email-cards
```

Do NOT push unless the user asks.
