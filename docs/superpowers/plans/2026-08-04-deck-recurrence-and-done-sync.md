# Card Recurrence and done-tick Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Deck label expresses a repeating schedule so cards return from `Done` to `Todo` automatically, and Deck's per-card `done` field stays in agreement with membership of the `Done` stack.

**Architecture:** Three parts across two repos. `nextcloud-deck-client` (the public `ncdeck` library) gains a reusable full-payload card update, released as 1.1.0. `nextcloud-deck-cli` gains a new daily `deck_recur.py` job for recurrence, and a third pass inside the existing 5-minute `imap_deck_sync.py` for the done-tick sync.

**Tech Stack:** Python 3.11+, `ncdeck` (requests / httpx), pytest, Poetry, systemd user timers, chezmoi, 1Password CLI.

**Spec:** `docs/superpowers/specs/2026-08-04-deck-recurrence-and-done-sync-design.md`

## Global Constraints

- Tasks 1-4 are in `~/prog/nextcloud-deck-client`. Tasks 5-11 are in `~/prog/nextcloud-deck-cli`. Never mix the two in one commit.
- `nextcloud-deck-cli` must not be pinned to `nextcloud-deck-client>=1.1.0` until 1.1.0 is actually published to PyPI. Installing the library system-wide before the consumer is migrated caused a live sync outage on 2026-08-04; do the release first, the pin second.
- Every public client method takes `board_id` as its last named parameter. `tests/test_parity.py` asserts that `DeckClient` and `AsyncDeckClient` expose identical public method names, kinds and signatures — any change to one client must be mirrored exactly in the other.
- Deck's card update endpoint treats every omitted field as a default, silently blanking `description` and `duedate`. Never send a partial card payload.
- Deck's `CardService::update()` collapses an omitted `done` and an explicitly-null `done` to `setDone(null)`. Omitting the field is how you clear it.
- Exit codes for every CLI in this project: `0` clean, `1` setup or connection failure, `2` completed with per-action failures.
- Secrets come from 1Password via the `op` CLI at run time. Never hardcode a password, token, or app password in a script, test, or config file.
- Python: target Python 3, install with `pip install --user`, no venvs.
- All chezmoi-managed files start with the line `# Managed by chezmoi - do not edit directly`.
- Edit chezmoi sources under `~/.local/share/chezmoi/`, never the deployed target. Apply with `chezmoi apply` (no per-file paths — chezmoiscripts are involved).
- No emoji variation selectors (U+FE0F) in any string. `🔁` (U+1F501) is safe.
- Create new files with a shell heredoc (`cat > file <<'EOF'`). File-writing editor tools emit CRLF on this system.
- Service scripts must resolve `HOME` before use, per the pattern in `imap-deck-sync-wrapper.sh`.
- Do not restart or rebuild any container as part of this work.

## File Structure

**`~/prog/nextcloud-deck-client` (ncdeck 1.1.0):**

| File | Responsibility |
|---|---|
| `ncdeck/models.py` | `Card` gains `done` and `deleted_at` |
| `ncdeck/_parsing.py` | `parse_card` populates the two new fields |
| `ncdeck/_payload.py` | **New.** `_card_payload()` — sole owner of Deck's blanking and `done` quirks |
| `ncdeck/client.py` | `move_card` rebuilt on `_card_payload`, gains `clear_done`; new `patch_card` |
| `ncdeck/async_client.py` | Identical changes, mirrored |
| `tests/test_payload.py` | **New.** Payload construction in isolation |
| `pyproject.toml` | Version 1.1.0 |

**`~/prog/nextcloud-deck-cli`:**

| File | Responsibility |
|---|---|
| `deck_recur.py` | **New.** Schedule grammar, trigger evaluation, planning, execution, `run()` |
| `nextcloud-deck-recur.py` | **New.** CLI entrypoint for recurrence |
| `deck-recur-wrapper.sh` | **New.** `HOME` resolution, 1Password lookups |
| `imap_deck_sync.py` | Gains the done-tick pass and the `plan_archive` recurrence guard |
| `tests/test_deck_recur.py` | **New.** Recurrence tests |
| `tests/test_imap_deck_sync.py` | Done-tick pass and guard tests |
| `README.md` | Documents both features |

**`~/.local/share/chezmoi/dot_config/systemd/user/`:** `deck-recur.service`, `deck-recur.timer`

---

# Part 1 — ncdeck 1.1.0

### Task 1: Expose `done` and `deletedAt` on the Card model

Both new features branch on Deck's `done` field and must skip trashed cards, but `Card` exposes neither — today they are reachable only through `card.source`. Add them as first-class fields.

**Files:**
- Modify: `~/prog/nextcloud-deck-client/ncdeck/models.py`
- Modify: `~/prog/nextcloud-deck-client/ncdeck/_parsing.py`
- Test: `~/prog/nextcloud-deck-client/tests/test_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Card.done: str | None` (ISO 8601 timestamp with offset, e.g. `"2026-08-03T17:08:13+00:00"`, or `None` when unticked) and `Card.deleted_at: int` (0 when not trashed).

- [ ] **Step 1: Confirm no caller constructs `Card` positionally past `title`**

New fields are appended before `source`, which is safe only if nothing relies on positional order.

```bash
cd ~/prog/nextcloud-deck-client
grep -rn "Card(" ncdeck tests | grep -v "parse_card\|# " | head -30
```

Expected: every construction uses keyword arguments after `id` and `title`. If any passes a third positional argument, append the new fields at the very end of the dataclass instead, after `source`, and note it in your report.

- [ ] **Step 2: Write the failing test**

Add to `tests/test_models.py`:

```python
from ncdeck._parsing import parse_card


def test_parse_card_reads_done_and_deleted_at():
    raw = {
        "id": 323,
        "title": "Timeliste",
        "done": "2026-08-03T17:08:04+00:00",
        "deletedAt": 0,
    }
    card = parse_card(raw, stack_id=9)
    assert card.done == "2026-08-03T17:08:04+00:00"
    assert card.deleted_at == 0


def test_parse_card_defaults_done_and_deleted_at_when_absent():
    card = parse_card({"id": 1, "title": "No flags"}, stack_id=9)
    assert card.done is None
    assert card.deleted_at == 0


def test_parse_card_normalises_null_deleted_at_to_zero():
    card = parse_card({"id": 2, "title": "Nulled", "deletedAt": None}, stack_id=9)
    assert card.deleted_at == 0


def test_parse_card_reads_trashed_card():
    card = parse_card({"id": 3, "title": "Trashed", "deletedAt": 1754300000}, stack_id=9)
    assert card.deleted_at == 1754300000
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd ~/prog/nextcloud-deck-client && python -m pytest tests/test_models.py -v -k "done_and_deleted or deleted_at or trashed"`
Expected: FAIL with `AttributeError: 'Card' object has no attribute 'done'`

- [ ] **Step 4: Add the fields to the model**

In `ncdeck/models.py`, inside `@dataclass class Card`, insert immediately after the `last_modified` field and before `source`:

```python
    done: str | None = None
    deleted_at: int = 0
```

- [ ] **Step 5: Populate them in `parse_card`**

In `ncdeck/_parsing.py`, inside the `return Card(...)` of `parse_card`, add these two arguments alongside the existing ones:

```python
        done=raw.get("done"),
        deleted_at=raw.get("deletedAt", 0) or 0,
```

The `or 0` normalises both a missing key and an explicit `null` to `0`, so callers can write `if card.deleted_at:` without a None check.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd ~/prog/nextcloud-deck-client && python -m pytest tests/ -v`
Expected: PASS, including the whole existing suite.

- [ ] **Step 7: Lint**

Run: `cd ~/prog/nextcloud-deck-client && ruff check ncdeck tests`
Expected: no findings.

- [ ] **Step 8: Commit**

```bash
cd ~/prog/nextcloud-deck-client
git add ncdeck/models.py ncdeck/_parsing.py tests/test_models.py
git commit -m "feat(models): expose done and deleted_at on Card

Both fields were previously reachable only through card.source. The
recurrence and done-tick features branch on done and must skip trashed
cards, so they become first-class fields.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Extract `_card_payload` and add `move_card(clear_done=)`

`move_card` builds a full card payload inline, in both clients, because Deck blanks omitted fields. Two more callers now need that payload, so extract it into one shared helper and express the new `clear_done` option through it.

**Files:**
- Create: `~/prog/nextcloud-deck-client/ncdeck/_payload.py`
- Create: `~/prog/nextcloud-deck-client/tests/test_payload.py`
- Modify: `~/prog/nextcloud-deck-client/ncdeck/client.py` (`move_card`)
- Modify: `~/prog/nextcloud-deck-client/ncdeck/async_client.py` (`move_card`)

**Interfaces:**
- Consumes: `Card.done` and `Card.deleted_at` from Task 1.
- Produces:
  - `ncdeck._payload.card_payload(card, *, order=None, clear_done=False, **overrides) -> dict`
  - `DeckClient.move_card(card, target_stack_id, order=0, clear_done=False, board_id=None) -> Card`
  - `AsyncDeckClient.move_card` with the identical signature.

- [ ] **Step 1: Write the failing test**

Create `tests/test_payload.py`:

```python
"""Tests for the shared full-card payload builder."""

import pytest

from ncdeck._payload import card_payload
from ncdeck.models import Card


def make_card(**source_overrides) -> Card:
    source = {
        "id": 323,
        "title": "Timeliste",
        "type": "plain",
        "owner": {"uid": "olen", "primaryKey": "olen"},
        "description": "Fyll ut timeliste",
        "order": 4,
        "duedate": "2026-08-25T10:00:00+00:00",
        "deletedAt": 0,
        "archived": False,
        "done": "2026-08-03T17:08:04+00:00",
    }
    source.update(source_overrides)
    return Card(id=323, title="Timeliste", stack_id=9, source=source)


def test_payload_carries_full_card_state():
    payload = card_payload(make_card())
    assert payload["title"] == "Timeliste"
    assert payload["type"] == "plain"
    assert payload["description"] == "Fyll ut timeliste"
    assert payload["duedate"] == "2026-08-25T10:00:00+00:00"
    assert payload["archived"] is False
    assert payload["deletedAt"] == 0


def test_payload_flattens_owner_dict_to_uid():
    assert card_payload(make_card())["owner"] == "olen"


def test_payload_accepts_owner_already_a_string():
    assert card_payload(make_card(owner="olen"))["owner"] == "olen"


def test_payload_preserves_order_by_default():
    assert card_payload(make_card())["order"] == 4


def test_payload_order_override_wins():
    assert card_payload(make_card(), order=0)["order"] == 0


def test_payload_keeps_non_null_done_by_default():
    assert card_payload(make_card())["done"] == "2026-08-03T17:08:04+00:00"


def test_payload_omits_done_when_clear_done():
    # Deck's CardService::update() clears `done` when the field is absent.
    assert "done" not in card_payload(make_card(), clear_done=True)


def test_payload_omits_absent_done():
    assert "done" not in card_payload(make_card(done=None))


def test_payload_overrides_win_over_source():
    payload = card_payload(make_card(), description="replaced")
    assert payload["description"] == "replaced"


def test_payload_override_can_null_done():
    assert card_payload(make_card(), done=None)["done"] is None


def test_payload_normalises_null_description_to_empty_string():
    assert card_payload(make_card(description=None))["description"] == ""


def test_payload_normalises_null_deleted_at_to_zero():
    assert card_payload(make_card(deletedAt=None))["deletedAt"] == 0


def test_payload_requires_source():
    with pytest.raises(ValueError, match="source"):
        card_payload(Card(id=1, title="No source"))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ~/prog/nextcloud-deck-client && python -m pytest tests/test_payload.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ncdeck._payload'`

The spec refers to this helper as `_card_payload`. It is named
`card_payload` here and lives in the already-private module
`ncdeck/_payload.py`, so the leading underscore would be redundant. Same
function, same contract.

- [ ] **Step 3: Create the payload module**

Create `ncdeck/_payload.py`:

```python
"""The full-card payload Deck's update endpoint requires.

Deck's card update endpoint treats every omitted field as a default rather
than leaving it alone: a partial payload silently blanks `description` and
`duedate`. Every write that changes a card therefore has to resend the
card's whole state. This module is the single place that knows that, and
the single place that knows Deck's `done` semantics.
"""

from __future__ import annotations

from ncdeck.models import Card


def card_payload(
    card: Card,
    *,
    order: int | None = None,
    clear_done: bool = False,
    **overrides,
) -> dict:
    """Build the full update payload for `card`, with `overrides` applied.

    `order` defaults to the card's current order, so a payload that is not
    trying to move the card leaves its position alone.

    `done` is included only when the card has a non-null value and
    `clear_done` is false. Deck's `CardService::update()` does not
    distinguish an omitted `done` from an explicitly-null one — both
    collapse to `$card->setDone(null)` — so omitting the field is how you
    clear it, and sending a present non-null value is the only way to
    preserve it.

    `deletedAt` is sent for completeness but does not round-trip:
    `CardApiController::update()` hardcodes it to 0 and never reads the body
    value, so updating a trashed card has the side effect of restoring it
    from the trash. Callers must filter trashed cards themselves.
    """
    if card.source is None:
        raise ValueError(
            "Card has no source data; fetch it via get_stacks() before writing"
        )

    raw = card.source
    owner = raw.get("owner")
    if isinstance(owner, dict):
        owner = owner.get("uid") or owner.get("primaryKey")

    payload = {
        "title": raw.get("title", card.title),
        "type": raw.get("type", "plain"),
        "owner": owner,
        "description": raw.get("description") or "",
        "order": raw.get("order", 0) if order is None else order,
        "duedate": raw.get("duedate"),
        "deletedAt": raw.get("deletedAt", 0) or 0,
        "archived": raw.get("archived", False),
    }

    if not clear_done and raw.get("done") is not None:
        payload["done"] = raw["done"]

    payload.update(overrides)
    return payload
```

- [ ] **Step 4: Run the payload tests to verify they pass**

Run: `cd ~/prog/nextcloud-deck-client && python -m pytest tests/test_payload.py -v`
Expected: PASS, 13 tests.

- [ ] **Step 5: Rebuild the synchronous `move_card` on the helper**

In `ncdeck/client.py`, add the import near the existing ones:

```python
from ncdeck._payload import card_payload
```

Replace the whole body of `move_card` after its docstring, and change its signature. The new signature is:

```python
    def move_card(
        self,
        card: Card,
        target_stack_id: int,
        order: int = 0,
        clear_done: bool = False,
        board_id: int | None = None,
    ) -> Card:
```

Keep the existing docstring, and append this paragraph to it:

```
        Set `clear_done=True` to drop Deck's done-tick as part of the move,
        which is what returning a recurring card to the backlog needs.
```

The body becomes:

```python
        bid = self._bid(board_id)
        payload = card_payload(card, order=order, clear_done=clear_done)
        resp = self.session.put(
            f"{self.api_base}/boards/{bid}/stacks/{target_stack_id}/cards/{card.id}",
            json=payload,
            timeout=self.timeout,
        )
        return parse_card(self._get_json(resp), target_stack_id)
```

The explicit `if card.source is None: raise ValueError(...)` guard is removed from `move_card` — `card_payload` raises the same error with the same message fragment.

- [ ] **Step 6: Mirror it in the asynchronous client**

In `ncdeck/async_client.py`, add the same import, apply the identical signature change and docstring paragraph, and replace the body with:

```python
        bid = self._bid(board_id)
        payload = card_payload(card, order=order, clear_done=clear_done)
        async with self._client() as client:
            resp = await client.put(
                f"{self.api_base}/boards/{bid}/stacks/{target_stack_id}/cards/{card.id}",
                json=payload,
            )
            resp.raise_for_status()
            return parse_card(resp.json(), target_stack_id)
```

- [ ] **Step 7: Add client-level tests for `clear_done`**

Add to `tests/test_client.py`, following the request-capturing style already used in that file. **Read the existing `move_card` test first.** The `client` and `capture_put` names below stand in for whatever that file actually uses to build a client and inspect the outgoing request — adapt them rather than adding a second, parallel way of testing requests:

```python
def test_move_card_preserves_done_by_default(client, capture_put):
    card = make_card_with_done("2026-08-03T17:08:04+00:00")
    client.move_card(card, target_stack_id=22)
    assert capture_put.json["done"] == "2026-08-03T17:08:04+00:00"


def test_move_card_clear_done_omits_the_field(client, capture_put):
    card = make_card_with_done("2026-08-03T17:08:04+00:00")
    client.move_card(card, target_stack_id=22, clear_done=True)
    assert "done" not in capture_put.json


def test_move_card_targets_the_destination_stack(client, capture_put):
    card = make_card_with_done(None)
    client.move_card(card, target_stack_id=22)
    assert "/stacks/22/cards/" in capture_put.url
```

Define `make_card_with_done` as a module-level helper in the test file, mirroring `make_card` from `tests/test_payload.py` but taking the `done` value as its argument. Add the equivalent three tests to `tests/test_async_client.py` using that file's async request-capturing pattern.

- [ ] **Step 8: Run the full suite and lint**

Run: `cd ~/prog/nextcloud-deck-client && python -m pytest tests/ -v && ruff check ncdeck tests`
Expected: PASS with no lint findings. `tests/test_parity.py` must pass — it is what proves the two clients still match.

- [ ] **Step 9: Commit**

```bash
cd ~/prog/nextcloud-deck-client
git add ncdeck/_payload.py ncdeck/client.py ncdeck/async_client.py tests/
git commit -m "feat: extract card_payload and add move_card(clear_done=)

Deck blanks every field omitted from a card update, so move_card resent
the card's whole state inline in both clients. Two more callers need that
payload, so it moves to ncdeck/_payload.py as the single owner of Deck's
blanking and done semantics.

clear_done omits the done field, which is how Deck clears it.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Add `patch_card`

The done-tick sync needs to set one field on a card without moving it. `update_card` cannot do that safely — it sends exactly the kwargs it is given, so it blanks everything else.

**Files:**
- Modify: `~/prog/nextcloud-deck-client/ncdeck/client.py`
- Modify: `~/prog/nextcloud-deck-client/ncdeck/async_client.py`
- Test: `~/prog/nextcloud-deck-client/tests/test_client.py`, `tests/test_async_client.py`

**Interfaces:**
- Consumes: `card_payload` from Task 2.
- Produces: `DeckClient.patch_card(card, board_id=None, **changes) -> Card` and the identical `AsyncDeckClient.patch_card`. The parameter shape mirrors the existing `update_card(self, stack_id, card_id, board_id=None, **kwargs)`, which is why `board_id` can precede `**changes` and still satisfy the board_id-last convention.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_client.py`:

```python
def test_patch_card_sends_full_state_with_one_field_changed(client, capture_put):
    card = make_card_with_done(None)
    client.patch_card(card, done="2026-08-04T12:00:00+00:00")
    sent = capture_put.json
    assert sent["done"] == "2026-08-04T12:00:00+00:00"
    # Everything else must survive: a partial payload blanks these.
    assert sent["description"] == "Fyll ut timeliste"
    assert sent["duedate"] == "2026-08-25T10:00:00+00:00"
    assert sent["title"] == "Timeliste"


def test_patch_card_targets_the_cards_current_stack(client, capture_put):
    card = make_card_with_done(None)          # stack_id=9
    client.patch_card(card, done="2026-08-04T12:00:00+00:00")
    assert "/stacks/9/cards/323" in capture_put.url


def test_patch_card_preserves_order(client, capture_put):
    card = make_card_with_done(None)          # source order is 4
    client.patch_card(card, done="2026-08-04T12:00:00+00:00")
    assert capture_put.json["order"] == 4


def test_patch_card_requires_a_stack_id(client):
    card = make_card_with_done(None)
    card.stack_id = None
    with pytest.raises(ValueError, match="stack_id"):
        client.patch_card(card, done="2026-08-04T12:00:00+00:00")
```

Add the same four tests to `tests/test_async_client.py`, awaiting the call and using that file's async capture pattern.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ~/prog/nextcloud-deck-client && python -m pytest tests/test_client.py -v -k patch_card`
Expected: FAIL with `AttributeError: 'DeckClient' object has no attribute 'patch_card'`

- [ ] **Step 3: Implement it on the synchronous client**

In `ncdeck/client.py`, immediately after `move_card`:

```python
    def patch_card(self, card: Card, board_id: int | None = None, **changes) -> Card:
        """Change specific fields on a card, leaving the rest intact.

        Deck's update endpoint blanks every field it is not given, so this
        resends the card's full state with `changes` applied on top. Use it
        instead of `update_card` whenever you hold a Card and want to change
        part of it.

        The card stays in its current stack; use `move_card` to move it.

        Clearing the done-tick is `patch_card(card, done=None)`: Deck treats a
        null `done` and an absent `done` alike.
        """
        if card.stack_id is None:
            raise ValueError(
                "Card has no stack_id; fetch it via get_stacks() before patching"
            )
        bid = self._bid(board_id)
        payload = card_payload(card, **changes)
        resp = self.session.put(
            f"{self.api_base}/boards/{bid}/stacks/{card.stack_id}/cards/{card.id}",
            json=payload,
            timeout=self.timeout,
        )
        return parse_card(self._get_json(resp), card.stack_id)
```

- [ ] **Step 4: Mirror it on the asynchronous client**

In `ncdeck/async_client.py`, immediately after `move_card`, with the identical signature and docstring:

```python
    async def patch_card(self, card: Card, board_id: int | None = None, **changes) -> Card:
        """Change specific fields on a card, leaving the rest intact.

        Deck's update endpoint blanks every field it is not given, so this
        resends the card's full state with `changes` applied on top. Use it
        instead of `update_card` whenever you hold a Card and want to change
        part of it.

        The card stays in its current stack; use `move_card` to move it.

        Clearing the done-tick is `patch_card(card, done=None)`: Deck treats a
        null `done` and an absent `done` alike.
        """
        if card.stack_id is None:
            raise ValueError(
                "Card has no stack_id; fetch it via get_stacks() before patching"
            )
        bid = self._bid(board_id)
        payload = card_payload(card, **changes)
        async with self._client() as client:
            resp = await client.put(
                f"{self.api_base}/boards/{bid}/stacks/{card.stack_id}/cards/{card.id}",
                json=payload,
            )
            resp.raise_for_status()
            return parse_card(resp.json(), card.stack_id)
```

- [ ] **Step 5: Run the full suite and lint**

Run: `cd ~/prog/nextcloud-deck-client && python -m pytest tests/ -v && ruff check ncdeck tests`
Expected: PASS. `tests/test_parity.py` must pass without modification — if it reports `patch_card` missing from one client, that client was not updated.

- [ ] **Step 6: Commit**

```bash
cd ~/prog/nextcloud-deck-client
git add ncdeck/client.py ncdeck/async_client.py tests/
git commit -m "feat: add patch_card for single-field card updates

update_card sends exactly the kwargs it is given, which blanks every
other field. patch_card resends the card's full state with the requested
changes applied, so callers can set one field safely.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Release ncdeck 1.1.0 to PyPI

**Files:**
- Modify: `~/prog/nextcloud-deck-client/pyproject.toml`
- Modify: `~/prog/nextcloud-deck-client/README.md`

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: `nextcloud-deck-client==1.1.0` on PyPI, and the same version importable locally, so Task 10 can pin `>=1.1.0`.

- [ ] **Step 1: Document the new methods in the library README**

Read `README.md` first and match its existing section style. Add `patch_card` to whatever method listing it already has, and add this note:

```markdown
### Changing a card without blanking it

Deck's card update endpoint treats every omitted field as a default, so a
partial payload silently clears `description` and `duedate`. `patch_card`
resends the card's full state with your changes applied:

```python
card = next(c for c in stacks[0].cards if c.title == "Timeliste")
client.patch_card(card, done="2026-08-04T12:00:00+00:00")   # tick it
client.patch_card(card, done=None)                          # un-tick it
```

`move_card(card, stack_id, clear_done=True)` moves a card and drops its
done-tick in one request.
```

- [ ] **Step 2: Bump the version**

In `pyproject.toml`, change line 3:

```toml
version = "1.1.0"
```

- [ ] **Step 3: Run every check CI runs, locally**

Read `.github/workflows/*.yml` first and run each test and lint command it defines, matching any pinned tool versions. At minimum:

Run: `cd ~/prog/nextcloud-deck-client && ruff check ncdeck tests && python -m pytest tests/ -v`
Expected: no lint findings, all tests pass.

- [ ] **Step 4: Commit and push, which triggers the OIDC release**

Publishing is a GitHub Actions trusted-publisher workflow that fires on a push touching `pyproject.toml`. There is no API token to supply.

```bash
cd ~/prog/nextcloud-deck-client
git add pyproject.toml README.md
git commit -m "release: 1.1.0

Adds patch_card, move_card(clear_done=), and Card.done / Card.deleted_at.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
git push
```

- [ ] **Step 5: Watch the release workflow to completion**

```bash
cd ~/prog/nextcloud-deck-client
gh run watch "$(gh run list --limit 1 --json databaseId --jq '.[0].databaseId')"
```

Expected: the workflow ends green. If it fails, fix and re-push — do not proceed to Task 10 with an unpublished version.

- [ ] **Step 6: Verify the release is installable and install it**

```bash
pip install --user --upgrade 'nextcloud-deck-client>=1.1.0'
python -c "import ncdeck, inspect; print(inspect.signature(ncdeck.DeckClient.patch_card)); print(inspect.signature(ncdeck.DeckClient.move_card))"
```

Expected: both signatures print, `move_card` showing `clear_done`. PyPI can take a minute to serve a new release; retry rather than assuming failure.

**Report in your task report:** the published version, and confirmation that the installed `ncdeck` exposes `patch_card`.

---

# Part 2 — Feature A: Label-Driven Recurrence

### Task 5: The schedule grammar

The parsing and trigger rules are pure functions with no network and no Deck client, so they are built and tested first, in isolation.

**Files:**
- Create: `~/prog/nextcloud-deck-cli/deck_recur.py`
- Create: `~/prog/nextcloud-deck-cli/tests/test_deck_recur.py`

**Interfaces:**
- Consumes: nothing. This task must not import `ncdeck`.
- Produces:
  - `DEFAULT_LABEL_PREFIX = "Repeat:"`
  - `class ScheduleError(ValueError)`
  - `Schedule(kind: str, weekdays: frozenset[int] = frozenset(), day: int | None = None)` — frozen dataclass; `kind` is one of `"daily"`, `"weekdays"`, `"dayofmonth"`; weekday numbers are Python's, 0 = Monday.
  - `parse_schedule(expr: str) -> Schedule`
  - `fires_on(schedule: Schedule, on: datetime.date) -> bool`
  - `parse_labels(labels, prefix: str = DEFAULT_LABEL_PREFIX) -> tuple[dict[int, Schedule], dict[int, str]]` — returns `{label_id: Schedule}` and `{label_id: problem_message}`. It collects problems rather than logging them so it stays pure.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_deck_recur.py`:

```python
"""Tests for label-driven Deck card recurrence."""

from dataclasses import dataclass
from datetime import date

import pytest

from deck_recur import (
    DEFAULT_LABEL_PREFIX,
    Schedule,
    ScheduleError,
    fires_on,
    parse_labels,
    parse_schedule,
)


@dataclass
class FakeLabel:
    id: int
    title: str


# --- parse_schedule -------------------------------------------------------

def test_parse_daily():
    assert parse_schedule("daily") == Schedule(kind="daily")


def test_parse_single_weekday():
    assert parse_schedule("mon") == Schedule(kind="weekdays", weekdays=frozenset({0}))


def test_parse_weekday_list():
    assert parse_schedule("mon,thu") == Schedule(
        kind="weekdays", weekdays=frozenset({0, 3})
    )


def test_parse_weekday_list_tolerates_spaces():
    assert parse_schedule(" mon , thu ") == Schedule(
        kind="weekdays", weekdays=frozenset({0, 3})
    )


def test_parse_is_case_insensitive():
    assert parse_schedule("MON") == Schedule(kind="weekdays", weekdays=frozenset({0}))


def test_parse_day_of_month():
    assert parse_schedule("25") == Schedule(kind="dayofmonth", day=25)


def test_parse_all_seven_weekdays():
    for name, number in [
        ("mon", 0), ("tue", 1), ("wed", 2), ("thu", 3),
        ("fri", 4), ("sat", 5), ("sun", 6),
    ]:
        assert parse_schedule(name).weekdays == frozenset({number})


@pytest.mark.parametrize("expr", ["modnay", "", "   ", "mon,", "mon,xyz", "0", "32", "-1"])
def test_parse_rejects_bad_expressions(expr):
    with pytest.raises(ScheduleError):
        parse_schedule(expr)


def test_parse_names_the_interval_form_specifically():
    with pytest.raises(ScheduleError, match="not implemented yet"):
        parse_schedule("14d")


def test_interval_error_quotes_the_expression():
    with pytest.raises(ScheduleError, match="14d"):
        parse_schedule("14d")


# --- fires_on -------------------------------------------------------------

def test_daily_fires_every_day():
    assert fires_on(Schedule(kind="daily"), date(2026, 8, 4))
    assert fires_on(Schedule(kind="daily"), date(2026, 8, 5))


def test_weekday_fires_on_its_day():
    monday = Schedule(kind="weekdays", weekdays=frozenset({0}))
    assert fires_on(monday, date(2026, 8, 3))       # a Monday
    assert not fires_on(monday, date(2026, 8, 4))   # a Tuesday


def test_weekday_list_fires_on_either_day():
    sched = Schedule(kind="weekdays", weekdays=frozenset({0, 3}))
    assert fires_on(sched, date(2026, 8, 3))        # Monday
    assert fires_on(sched, date(2026, 8, 6))        # Thursday
    assert not fires_on(sched, date(2026, 8, 5))    # Wednesday


def test_day_of_month_fires_on_its_day():
    sched = Schedule(kind="dayofmonth", day=25)
    assert fires_on(sched, date(2026, 8, 25))
    assert not fires_on(sched, date(2026, 8, 24))


def test_day_31_clamps_to_end_of_february():
    sched = Schedule(kind="dayofmonth", day=31)
    assert fires_on(sched, date(2026, 2, 28))
    assert not fires_on(sched, date(2026, 2, 27))


def test_day_31_clamps_to_29_february_in_a_leap_year():
    sched = Schedule(kind="dayofmonth", day=31)
    assert fires_on(sched, date(2028, 2, 29))
    assert not fires_on(sched, date(2028, 2, 28))


def test_day_31_clamps_to_30_april():
    sched = Schedule(kind="dayofmonth", day=31)
    assert fires_on(sched, date(2026, 4, 30))


def test_day_31_does_not_clamp_in_a_31_day_month():
    sched = Schedule(kind="dayofmonth", day=31)
    assert fires_on(sched, date(2026, 8, 31))
    assert not fires_on(sched, date(2026, 8, 30))


def test_day_29_does_not_fire_on_28_february_in_a_leap_year():
    # February 2028 has 29 days, so there is no clamping to do.
    sched = Schedule(kind="dayofmonth", day=29)
    assert not fires_on(sched, date(2028, 2, 28))


# --- parse_labels ---------------------------------------------------------

def test_parse_labels_picks_up_prefixed_labels():
    schedules, problems = parse_labels([FakeLabel(146, "Repeat: 25")])
    assert schedules == {146: Schedule(kind="dayofmonth", day=25)}
    assert problems == {}


def test_parse_labels_ignores_other_labels():
    schedules, problems = parse_labels(
        [FakeLabel(145, "Email"), FakeLabel(16, "Later")]
    )
    assert schedules == {}
    assert problems == {}


def test_parse_labels_matches_prefix_case_insensitively():
    schedules, _ = parse_labels([FakeLabel(1, "repeat:MON")])
    assert schedules == {1: Schedule(kind="weekdays", weekdays=frozenset({0}))}


def test_parse_labels_collects_problems_instead_of_raising():
    schedules, problems = parse_labels([FakeLabel(9, "Repeat: modnay")])
    assert schedules == {}
    assert 9 in problems
    assert "Repeat: modnay" in problems[9]


def test_parse_labels_reports_the_interval_form():
    _, problems = parse_labels([FakeLabel(9, "Repeat: 14d")])
    assert "not implemented yet" in problems[9]


def test_parse_labels_honours_a_custom_prefix():
    schedules, _ = parse_labels([FakeLabel(1, "@ mon")], prefix="@")
    assert schedules == {1: Schedule(kind="weekdays", weekdays=frozenset({0}))}


def test_default_prefix_is_repeat_colon():
    assert DEFAULT_LABEL_PREFIX == "Repeat:"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/prog/nextcloud-deck-cli && python -m pytest tests/test_deck_recur.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'deck_recur'`

- [ ] **Step 3: Create the module with the grammar**

Create `deck_recur.py`:

```python
#!/usr/bin/env python3
"""
Label-driven recurrence for Nextcloud Deck cards.

A card carrying a label whose title starts with `Repeat:` returns from the
Done stack to the Todo stack on the days its schedule names. Labels are
created by hand in the Deck UI; this module only reads them.

The trigger model is deliberately edge-triggered by the move itself: once a
card has been moved out of the source stack it no longer matches, so there
is no per-card state to keep and no state file. That only works because the
job runs once a day — at a shorter interval, re-completing a card on its own
trigger day would send it straight back.
"""
from __future__ import annotations

import calendar
import logging
import re
from dataclasses import dataclass
from datetime import date

log = logging.getLogger(__name__)

DEFAULT_LABEL_PREFIX = "Repeat:"

WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

# Recognised so it can be rejected with a message that names it, rather than
# falling through to the generic "unknown weekday" error.
INTERVAL_RE = re.compile(r"^(\d+)d$")


class ScheduleError(ValueError):
    """A `Repeat:` label carries an expression we cannot use."""


@dataclass(frozen=True)
class Schedule:
    """A parsed recurrence expression.

    `kind` is one of "daily", "weekdays", "dayofmonth". Weekday numbers are
    Python's: 0 is Monday.
    """
    kind: str
    weekdays: frozenset[int] = frozenset()
    day: int | None = None


def parse_schedule(expr: str) -> Schedule:
    """Parse the part of a label title that follows the prefix.

    Raises ScheduleError on anything unusable, including the reserved
    interval form, whose message names it specifically.
    """
    text = (expr or "").strip().lower()
    if not text:
        raise ScheduleError("empty schedule expression")

    if text == "daily":
        return Schedule(kind="daily")

    if INTERVAL_RE.match(text):
        raise ScheduleError(
            f"interval schedules like {text!r} are not implemented yet"
        )

    if text.isdigit():
        day = int(text)
        if not 1 <= day <= 31:
            raise ScheduleError(f"day-of-month {day} is outside 1..31")
        return Schedule(kind="dayofmonth", day=day)

    parts = [p.strip() for p in text.split(",")]
    if not all(parts):
        raise ScheduleError(f"malformed weekday list {expr!r}")
    unknown = sorted({p for p in parts if p not in WEEKDAYS})
    if unknown:
        raise ScheduleError(
            f"unknown weekday(s) {', '.join(unknown)} in {expr!r}"
        )
    return Schedule(
        kind="weekdays",
        weekdays=frozenset(WEEKDAYS[p] for p in parts),
    )


def fires_on(schedule: Schedule, on: date) -> bool:
    """Does `schedule` name `on` as a trigger day?"""
    if schedule.kind == "daily":
        return True

    if schedule.kind == "weekdays":
        return on.weekday() in schedule.weekdays

    if schedule.kind == "dayofmonth":
        if on.day == schedule.day:
            return True
        # A day-of-month past the end of a short month fires on its last day,
        # so "Repeat: 31" still happens in February.
        last = calendar.monthrange(on.year, on.month)[1]
        return schedule.day > last and on.day == last

    raise ScheduleError(f"unknown schedule kind {schedule.kind!r}")


def parse_labels(
    labels, prefix: str = DEFAULT_LABEL_PREFIX
) -> tuple[dict[int, Schedule], dict[int, str]]:
    """Read every recurrence label on a board.

    Returns `({label_id: Schedule}, {label_id: problem_message})`. Problems
    are collected rather than logged so this stays pure and testable; the
    caller logs them, and can name the cards affected.
    """
    pref = (prefix or "").strip().lower()
    schedules: dict[int, Schedule] = {}
    problems: dict[int, str] = {}

    for label in labels or []:
        title = (getattr(label, "title", "") or "").strip()
        if not title.lower().startswith(pref):
            continue
        try:
            schedules[label.id] = parse_schedule(title[len(pref):])
        except ScheduleError as e:
            problems[label.id] = f"Ignoring label {title!r}: {e}"

    return schedules, problems
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/prog/nextcloud-deck-cli && python -m pytest tests/test_deck_recur.py -v`
Expected: PASS, all tests.

- [ ] **Step 5: Commit**

```bash
cd ~/prog/nextcloud-deck-cli
git add deck_recur.py tests/test_deck_recur.py
git commit -m "feat(recur): schedule grammar for Repeat: labels

Parses daily, weekday lists and day-of-month from a label title, and
evaluates whether a schedule fires on a given date. Day-of-month clamps to
the end of short months so Repeat: 31 still happens in February.

The reserved interval form (14d) is recognised and rejected with a message
that names it, so a deferred feature does not look like a typo.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Recurrence planning, execution and `run()`

**Files:**
- Modify: `~/prog/nextcloud-deck-cli/deck_recur.py`
- Test: `~/prog/nextcloud-deck-cli/tests/test_deck_recur.py`

**Interfaces:**
- Consumes: `Schedule`, `fires_on`, `parse_labels`, `DEFAULT_LABEL_PREFIX` from Task 5. `DeckClient.move_card(card, target_stack_id, order=0, clear_done=False, board_id=None)` from Task 2.
- Produces:
  - `RecurAction(card, target_stack_id: int, label_titles: tuple[str, ...])` — frozen dataclass.
  - `ExecutionSummary(moved: int = 0, failures: int = 0)`
  - `plan_recurrence(cards, schedules, on: date, target_stack_id: int) -> list[RecurAction]`
  - `execute_plan(actions, deck, dry_run: bool = False) -> ExecutionSummary`
  - `Config` dataclass with fields `nc_url, nc_username, nc_password, nc_board_id, source_stack_name="Done", target_stack_name="Todo", label_prefix=DEFAULT_LABEL_PREFIX, on_date: date | None = None, dry_run=False, verbose=False`
  - `run(config: Config) -> int`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_deck_recur.py`:

```python
from deck_recur import (
    Config,
    RecurAction,
    execute_plan,
    plan_recurrence,
    run,
)


@dataclass
class FakeCard:
    id: int
    title: str
    labels: list
    archived: bool = False
    deleted_at: int = 0
    stack_id: int = 9


@dataclass
class FakeStack:
    id: int
    title: str
    cards: list


class FakeDeck:
    """Records move_card calls instead of performing them."""

    def __init__(self, board_labels=None, stacks=None, fail_on=()):
        self._board_labels = board_labels or []
        self._stacks = stacks or []
        self._fail_on = set(fail_on)
        self.moves = []

    def get_board(self, board_id=None):
        return type("B", (), {"labels": self._board_labels})()

    def get_stacks(self, include_archived=True, board_id=None):
        return self._stacks

    def move_card(self, card, target_stack_id, order=0, clear_done=False,
                  board_id=None):
        if card.id in self._fail_on:
            raise RuntimeError("boom")
        self.moves.append((card.id, target_stack_id, order, clear_done))


DUE = Schedule(kind="dayofmonth", day=25)
THE_25TH = date(2026, 8, 25)
THE_24TH = date(2026, 8, 24)


# --- plan_recurrence ------------------------------------------------------

def test_plans_a_move_for_a_card_that_is_due():
    card = FakeCard(323, "Timeliste", [FakeLabel(146, "Repeat: 25")])
    actions = plan_recurrence([card], {146: DUE}, THE_25TH, target_stack_id=22)
    assert len(actions) == 1
    assert actions[0].card is card
    assert actions[0].target_stack_id == 22
    assert actions[0].label_titles == ("Repeat: 25",)


def test_plans_nothing_when_not_due():
    card = FakeCard(323, "Timeliste", [FakeLabel(146, "Repeat: 25")])
    assert plan_recurrence([card], {146: DUE}, THE_24TH, 22) == []


def test_skips_cards_without_a_recurrence_label():
    card = FakeCard(110, "Vanne planter", [FakeLabel(145, "Email")])
    assert plan_recurrence([card], {146: DUE}, THE_25TH, 22) == []


def test_skips_cards_with_no_labels_at_all():
    card = FakeCard(145, "Migrere lupus", [])
    assert plan_recurrence([card], {146: DUE}, THE_25TH, 22) == []


def test_skips_archived_cards():
    card = FakeCard(323, "Timeliste", [FakeLabel(146, "Repeat: 25")], archived=True)
    assert plan_recurrence([card], {146: DUE}, THE_25TH, 22) == []


def test_skips_trashed_cards():
    card = FakeCard(323, "Timeliste", [FakeLabel(146, "Repeat: 25")],
                    deleted_at=1754300000)
    assert plan_recurrence([card], {146: DUE}, THE_25TH, 22) == []


def test_multiple_labels_union_so_either_can_fire():
    card = FakeCard(
        1, "Chore", [FakeLabel(146, "Repeat: 25"), FakeLabel(147, "Repeat: mon")]
    )
    schedules = {146: DUE, 147: Schedule(kind="weekdays", weekdays=frozenset({0}))}
    # 2026-08-03 is a Monday and not the 25th: the weekday label alone fires.
    actions = plan_recurrence([card], schedules, date(2026, 8, 3), 22)
    assert len(actions) == 1
    assert actions[0].label_titles == ("Repeat: mon",)


def test_a_card_matching_two_labels_yields_one_action():
    card = FakeCard(
        1, "Chore", [FakeLabel(146, "Repeat: 25"), FakeLabel(148, "Repeat: daily")]
    )
    schedules = {146: DUE, 148: Schedule(kind="daily")}
    actions = plan_recurrence([card], schedules, THE_25TH, 22)
    assert len(actions) == 1
    assert set(actions[0].label_titles) == {"Repeat: 25", "Repeat: daily"}


# --- execute_plan ---------------------------------------------------------

def test_execute_moves_with_order_zero_and_clears_done():
    card = FakeCard(323, "Timeliste", [FakeLabel(146, "Repeat: 25")])
    deck = FakeDeck()
    summary = execute_plan([RecurAction(card, 22, ("Repeat: 25",))], deck)
    assert deck.moves == [(323, 22, 0, True)]
    assert summary.moved == 1
    assert summary.failures == 0


def test_execute_dry_run_performs_no_moves():
    card = FakeCard(323, "Timeliste", [FakeLabel(146, "Repeat: 25")])
    deck = FakeDeck()
    summary = execute_plan([RecurAction(card, 22, ("Repeat: 25",))], deck, dry_run=True)
    assert deck.moves == []
    assert summary.moved == 1


def test_execute_counts_a_failure_and_keeps_going():
    a = FakeCard(1, "A", [])
    b = FakeCard(2, "B", [])
    deck = FakeDeck(fail_on={1})
    summary = execute_plan(
        [RecurAction(a, 22, ()), RecurAction(b, 22, ())], deck
    )
    assert summary.failures == 1
    assert summary.moved == 1
    assert deck.moves == [(2, 22, 0, True)]


# --- run ------------------------------------------------------------------

def base_config(**kw):
    defaults = dict(
        nc_url="https://example.invalid",
        nc_username="u",
        nc_password="p",
        nc_board_id=4,
    )
    defaults.update(kw)
    return Config(**defaults)


def test_run_moves_a_due_card_end_to_end():
    card = FakeCard(323, "Timeliste", [FakeLabel(146, "Repeat: 25")])
    deck = FakeDeck(
        board_labels=[FakeLabel(146, "Repeat: 25")],
        stacks=[FakeStack(22, "Todo", []), FakeStack(9, "Done", [card])],
    )
    rc = run(base_config(on_date=THE_25TH), deck=deck)
    assert rc == 0
    assert deck.moves == [(323, 22, 0, True)]


def test_run_moves_nothing_on_a_non_trigger_day():
    card = FakeCard(323, "Timeliste", [FakeLabel(146, "Repeat: 25")])
    deck = FakeDeck(
        board_labels=[FakeLabel(146, "Repeat: 25")],
        stacks=[FakeStack(22, "Todo", []), FakeStack(9, "Done", [card])],
    )
    assert run(base_config(on_date=THE_24TH), deck=deck) == 0
    assert deck.moves == []


def test_run_dry_run_performs_no_moves():
    card = FakeCard(323, "Timeliste", [FakeLabel(146, "Repeat: 25")])
    deck = FakeDeck(
        board_labels=[FakeLabel(146, "Repeat: 25")],
        stacks=[FakeStack(22, "Todo", []), FakeStack(9, "Done", [card])],
    )
    assert run(base_config(on_date=THE_25TH, dry_run=True), deck=deck) == 0
    assert deck.moves == []


def test_run_exits_1_when_a_stack_is_missing():
    deck = FakeDeck(
        board_labels=[FakeLabel(146, "Repeat: 25")],
        stacks=[FakeStack(9, "Done", [])],          # no Todo
    )
    assert run(base_config(on_date=THE_25TH), deck=deck) == 1


def test_run_exits_0_when_the_board_has_no_recurrence_labels():
    deck = FakeDeck(
        board_labels=[FakeLabel(145, "Email")],
        stacks=[FakeStack(22, "Todo", []), FakeStack(9, "Done", [])],
    )
    assert run(base_config(on_date=THE_25TH), deck=deck) == 0


def test_run_exits_2_when_a_move_fails():
    card = FakeCard(323, "Timeliste", [FakeLabel(146, "Repeat: 25")])
    deck = FakeDeck(
        board_labels=[FakeLabel(146, "Repeat: 25")],
        stacks=[FakeStack(22, "Todo", []), FakeStack(9, "Done", [card])],
        fail_on={323},
    )
    assert run(base_config(on_date=THE_25TH), deck=deck) == 2


def test_run_warns_about_an_unparseable_label_and_names_the_cards(caplog):
    card = FakeCard(1, "Chore", [FakeLabel(9, "Repeat: modnay")])
    deck = FakeDeck(
        board_labels=[FakeLabel(9, "Repeat: modnay")],
        stacks=[FakeStack(22, "Todo", []), FakeStack(9, "Done", [card])],
    )
    with caplog.at_level("WARNING"):
        run(base_config(on_date=THE_25TH), deck=deck)
    text = caplog.text
    assert "Repeat: modnay" in text
    assert "Chore" in text
```

Note the `run(config, deck=...)` seam: `run` takes an optional pre-built client so the end-to-end tests need no network. When `deck` is `None` it builds a real `DeckClient`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/prog/nextcloud-deck-cli && python -m pytest tests/test_deck_recur.py -v -k "plan or execute or run"`
Expected: FAIL with `ImportError: cannot import name 'Config' from 'deck_recur'`

- [ ] **Step 3: Add the actions, planner and executor**

Append to `deck_recur.py`:

```python
@dataclass(frozen=True)
class RecurAction:
    """Return a card to the target stack because a schedule fired today."""
    card: Any               # ncdeck.Card
    target_stack_id: int
    label_titles: tuple[str, ...]


@dataclass
class ExecutionSummary:
    """Counts of actions actually applied during execute_plan."""
    moved: int = 0
    failures: int = 0


def plan_recurrence(
    cards, schedules: dict[int, Schedule], on: date, target_stack_id: int
) -> list[RecurAction]:
    """Pure planner. One action per card that is due today.

    A card is due when it carries at least one recurrence label whose
    schedule fires on `on`. Archived and trashed cards are skipped: a chore
    deliberately archived must not resurrect, and Deck's update endpoint
    would restore a trashed card as a side effect of touching it.
    """
    actions: list[RecurAction] = []

    for card in cards or []:
        if getattr(card, "archived", False):
            continue
        if getattr(card, "deleted_at", 0):
            continue

        matched = [
            (getattr(label, "title", "") or "")
            for label in (getattr(card, "labels", None) or [])
            if (sched := schedules.get(getattr(label, "id", None))) is not None
            and fires_on(sched, on)
        ]
        if matched:
            actions.append(
                RecurAction(
                    card=card,
                    target_stack_id=target_stack_id,
                    label_titles=tuple(matched),
                )
            )

    return actions


def execute_plan(actions, deck, dry_run: bool = False) -> ExecutionSummary:
    """Apply a plan from plan_recurrence() against a Deck client.

    Per-action failures are logged and counted; the rest of the plan still
    runs, because the next daily run would retry a card that is still due.

    `deck` must duck-type as ncdeck.DeckClient (we call `.move_card(...)`).
    """
    summary = ExecutionSummary()

    for action in actions:
        matched = ", ".join(action.label_titles)
        try:
            if dry_run:
                log.info(
                    "[dry-run] would move card #%s %r to stack #%s and clear "
                    "its done-tick (matched %s)",
                    action.card.id, action.card.title, action.target_stack_id,
                    matched,
                )
            else:
                log.info(
                    "Moving card #%s %r to stack #%s and clearing its "
                    "done-tick (matched %s)",
                    action.card.id, action.card.title, action.target_stack_id,
                    matched,
                )
                deck.move_card(
                    action.card,
                    action.target_stack_id,
                    order=0,
                    clear_done=True,
                )
            summary.moved += 1
        except Exception as e:
            log.warning("Failed to recur card #%s: %s", action.card.id, e)
            summary.failures += 1

    return summary
```

Add `from typing import Any` to the imports at the top of the file.

- [ ] **Step 4: Add `Config`, `_find_stack` and `run`**

Append to `deck_recur.py`:

```python
@dataclass
class Config:
    nc_url: str
    nc_username: str
    nc_password: str
    nc_board_id: int
    source_stack_name: str = "Done"
    target_stack_name: str = "Todo"
    label_prefix: str = DEFAULT_LABEL_PREFIX
    on_date: date | None = None
    dry_run: bool = False
    verbose: bool = False


def _find_stack(stacks, name: str):
    target = (name or "").strip().lower()
    for s in stacks:
        if (getattr(s, "title", "") or "").strip().lower() == target:
            return s
    return None


def _cards_carrying(cards, label_id: int) -> list[str]:
    return [
        getattr(c, "title", "") or ""
        for c in cards
        if any(
            getattr(lb, "id", None) == label_id
            for lb in (getattr(c, "labels", None) or [])
        )
    ]


def run(config: Config, deck=None) -> int:
    """
    Run one recurrence pass. Returns a process exit code:
      0 — clean run (including "nothing to do")
      1 — setup/connection error, or a configured stack does not exist
      2 — run completed but at least one card failed to move

    `deck` is an optional pre-built client, for tests. In production it is
    None and a DeckClient is built from `config`.
    """
    if deck is None:
        from ncdeck import DeckClient
        deck = DeckClient(
            config.nc_url,
            config.nc_username,
            config.nc_password,
            config.nc_board_id,
        )

    try:
        board = deck.get_board()
        stacks = deck.get_stacks(include_archived=False)
    except Exception as e:
        log.error("Failed to fetch Deck board or stacks: %s", e)
        return 1

    source = _find_stack(stacks, config.source_stack_name)
    target = _find_stack(stacks, config.target_stack_name)
    missing = [
        n for n, s in (
            (config.source_stack_name, source),
            (config.target_stack_name, target),
        ) if s is None
    ]
    if missing:
        log.error(
            "Could not find required stack(s): %s on board %s",
            ", ".join(missing), config.nc_board_id,
        )
        return 1

    source_cards = getattr(source, "cards", None) or []
    schedules, problems = parse_labels(
        getattr(board, "labels", None) or [], config.label_prefix
    )

    for label_id, problem in problems.items():
        carriers = _cards_carrying(source_cards, label_id)
        if carriers:
            log.warning(
                "%s - affects %d card(s) in %s: %s",
                problem, len(carriers), config.source_stack_name,
                "; ".join(carriers),
            )
        else:
            log.warning("%s", problem)

    if not schedules:
        log.info(
            "No usable %s label(s) on board %s - nothing to do",
            config.label_prefix, config.nc_board_id,
        )
        return 0

    on = config.on_date or date.today()
    actions = plan_recurrence(source_cards, schedules, on, target.id)
    log.info(
        "Recurrence pass for %s: %d of %d card(s) in %s due "
        "(%d schedule(s) parsed, %d label(s) skipped)",
        on.isoformat(), len(actions), len(source_cards),
        config.source_stack_name, len(schedules), len(problems),
    )

    summary = execute_plan(actions, deck, dry_run=config.dry_run)
    log.info(
        "Done: moved=%d failures=%d%s",
        summary.moved, summary.failures,
        " (dry-run)" if config.dry_run else "",
    )
    return 0 if summary.failures == 0 else 2
```

- [ ] **Step 5: Run the whole suite to verify it passes**

Run: `cd ~/prog/nextcloud-deck-cli && python -m pytest tests/ -v`
Expected: PASS, including the pre-existing `tests/test_imap_deck_sync.py`.

- [ ] **Step 6: Commit**

```bash
cd ~/prog/nextcloud-deck-cli
git add deck_recur.py tests/test_deck_recur.py
git commit -m "feat(recur): planning, execution and run()

plan_recurrence is pure: one action per due card, skipping archived and
trashed cards. execute_plan moves each to order=0 with clear_done=True, so
a recurring card arrives at the top of Todo un-ticked.

run() takes an optional pre-built client so the end-to-end tests need no
network.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Recurrence CLI, wrapper and README

**Files:**
- Create: `~/prog/nextcloud-deck-cli/nextcloud-deck-recur.py`
- Create: `~/prog/nextcloud-deck-cli/deck-recur-wrapper.sh`
- Modify: `~/prog/nextcloud-deck-cli/README.md`

**Interfaces:**
- Consumes: `Config`, `run`, `DEFAULT_LABEL_PREFIX` from Task 6.
- Produces: an executable CLI accepting `--url -u -p -b --source-name --target-name --label-prefix --date --dry-run -v`, and `deck-recur-wrapper.sh` for the systemd unit in Task 10.

- [ ] **Step 1: Read the entrypoint this one mirrors**

Read `nextcloud-deck-imap-sync.py` in full. The new CLI copies its structure: the `env()` helper, the olen logging setup, `logging.basicConfig`, the required-argument check, and the Discord alerting on non-zero exit. Match it rather than inventing a new shape.

- [ ] **Step 2: Create the CLI entrypoint**

```bash
cd ~/prog/nextcloud-deck-cli
cat > nextcloud-deck-recur.py <<'EOF'
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
EOF
chmod +x nextcloud-deck-recur.py
```

- [ ] **Step 3: Create the wrapper**

`NEXTCLOUD_BOARD_ID` defaults to 4, the `Personal` board, matching `imap-deck-sync-wrapper.sh`.

```bash
cd ~/prog/nextcloud-deck-cli
cat > deck-recur-wrapper.sh <<'EOF'
#!/bin/bash
# Resolve HOME if invoked from a context where it's unset (systemd cron-like).
if [ -z "$HOME" ]; then
    HOME=$(getent passwd "$(id -u)" | cut -d: -f6)
fi
PATH=$HOME/.local/bin/:$PATH

set -euo pipefail

# 1Password item name — adjust to match what you have in your vault.
NC_OP_ITEM="${NC_OP_ITEM:-nextcloud-olen-deck-app-password}"

NEXTCLOUD_PASSWORD="$(op --item="$NC_OP_ITEM" --field=password)"

exec /home/olen/prog/nextcloud-deck-cli/nextcloud-deck-recur.py \
    --url "${NEXTCLOUD_BASE_URL:-https://cloud.olen.net/}" \
    --username "${NEXTCLOUD_USERNAME:-olen}" \
    --password "$NEXTCLOUD_PASSWORD" \
    --board-id "${NEXTCLOUD_BOARD_ID:-4}" \
    "$@"
EOF
chmod +x deck-recur-wrapper.sh
```

- [ ] **Step 4: Verify the CLI parses and rejects bad input**

```bash
cd ~/prog/nextcloud-deck-cli
./nextcloud-deck-recur.py --help
./nextcloud-deck-recur.py --url x -u u -p p -b 4 --date not-a-date; echo "exit=$?"
```

Expected: help text lists every flag; the second command prints `--date must be YYYY-MM-DD, got 'not-a-date'` and `exit=2`. It must not attempt a network call.

- [ ] **Step 5: Document the feature in the README**

Read `README.md` first — it is organised as one `#` section per script (`nextcloud-deck-list`, `nextcloud-deck-todo`, `nextcloud-deck-imap-sync`), each with a `## Usage:` block. Append a new section in that same shape:

````markdown
# nextcloud-deck-recur

Returns recurring cards from `Done` to `Todo` on the days their label names.

A recurrence label is any Deck label whose title starts with `Repeat:`. The
rest of the title is the schedule. Create the labels you need by hand in the
Deck UI; this script only reads them.

| Label title | Fires on |
|---|---|
| `Repeat: daily` | every day |
| `Repeat: mon` | every Monday |
| `Repeat: mon,thu` | every Monday and Thursday |
| `Repeat: 1` | the 1st of each month |
| `Repeat: 25` | the 25th of each month |

- Weekday names are `mon tue wed thu fri sat sun`, comma-separated.
- Day-of-month clamps to the end of short months, so `Repeat: 31` fires on
  28 February and on 30 April.
- The prefix and expression are matched case-insensitively.
- Several recurrence labels on one card union together.
- An unparseable expression is logged as a WARNING naming the label and the
  cards carrying it, and those cards are skipped, so a typo is visible rather
  than silently disabling a chore.
- Interval schedules (`Repeat: 14d`) are **not implemented**. The parser
  recognises the form and says so explicitly.

When a card is returned to `Todo` its Deck done-tick is cleared, so it
arrives as an open task.

There is no state file. The move out of `Done` is itself the record that the
card has recurred, which is why this must run **once a day** — at a shorter
interval, re-completing a card on its own trigger day would send it straight
back. Deployed as `deck-recur.timer`, daily at 04:15.

If the host is down for a whole trigger day, those cards wait for the next
occurrence. Replay a missed day with `--date`.

## Usage:

```
nextcloud-deck-recur.py --url URL -u USER -p PASS -b BOARD_ID
                        [--source-name Done] [--target-name Todo]
                        [--label-prefix "Repeat:"]
                        [--date YYYY-MM-DD] [--dry-run] [-v]
```

Every flag also reads a matching environment variable: `NEXTCLOUD_BASE_URL`,
`NEXTCLOUD_USERNAME`, `NEXTCLOUD_PASSWORD`, `NEXTCLOUD_BOARD_ID`,
`DONE_STACK_NAME`, `TODO_STACK_NAME`, `RECUR_LABEL_PREFIX`, `RECUR_DATE`.

`--date` evaluates a date other than today, for testing a schedule without
waiting for it and for replaying a missed day:

```bash
./deck-recur-wrapper.sh --date 2026-08-25 --dry-run -v
```

Exit codes: `0` clean, `1` setup or connection failure, `2` completed with
per-card failures.
````

- [ ] **Step 6: Commit**

```bash
cd ~/prog/nextcloud-deck-cli
git add nextcloud-deck-recur.py deck-recur-wrapper.sh README.md
git commit -m "feat(recur): CLI entrypoint, wrapper and README

Mirrors nextcloud-deck-imap-sync.py: env-var fallbacks for every flag, the
olen logger, and Discord alerting on a non-zero exit.

--date evaluates a day other than today, which is the only way to test a
monthly schedule without waiting for it.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

# Part 3 — Feature B: done-tick Sync

### Task 8: The done-tick pass in imap_deck_sync

**Files:**
- Modify: `~/prog/nextcloud-deck-cli/imap_deck_sync.py`
- Modify: `~/prog/nextcloud-deck-cli/nextcloud-deck-imap-sync.py`
- Modify: `~/prog/nextcloud-deck-cli/README.md`
- Test: `~/prog/nextcloud-deck-cli/tests/test_imap_deck_sync.py`

**Interfaces:**
- Consumes: `Card.done`, `Card.deleted_at` from Task 1; `DeckClient.patch_card(card, board_id=None, **changes)` from Task 3; `move_card(..., order=0)` from Task 2.
- Produces:
  - `TickDoneAction(card, card_title: str, done_at: str)` — frozen dataclass.
  - `MoveToDoneByTickAction(card, card_title: str, target_stack_id: int)` — frozen dataclass.
  - `plan_done_sync(done_cards, open_cards, done_stack_id: int, now_iso: str) -> list`
  - `ExecutionSummary` gains `ticked: int = 0` and `moved_by_tick: int = 0`.
  - `Config` gains `done_sync: bool = True`.

The spec sketched these actions carrying `card_id`; they carry the whole `card` instead, because `patch_card` and `move_card` both take a `Card`.

The spec also lists "a card in a stack that is neither Todo, Doing, nor Done is ignored" as a case. That is structural rather than testable at the planner: `plan_done_sync` only ever sees the cards `run()` hands it, and `run()` passes exactly the `Done` stack and the `Todo`+`Doing` stacks. Verify it by reading the `run()` wiring in Step 6; do not add a test that fakes an impossible input.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_imap_deck_sync.py`, reusing whatever card/stack fakes that file already defines — read it first and match them. If its existing fakes lack `done` or `deleted_at`, extend them rather than adding a second parallel set of fakes.

```python
from imap_deck_sync import (
    MoveToDoneByTickAction,
    TickDoneAction,
    plan_done_sync,
)

NOW_ISO = "2026-08-04T15:30:00+00:00"


def test_ticks_an_unticked_card_in_done():
    card = make_card(id=110, title="Vanne planter", stack_id=9, done=None)
    actions = plan_done_sync([card], [], done_stack_id=9, now_iso=NOW_ISO)
    assert actions == [
        TickDoneAction(card=card, card_title="Vanne planter", done_at=NOW_ISO)
    ]


def test_leaves_an_already_ticked_card_in_done_alone():
    card = make_card(id=323, title="Timeliste", stack_id=9,
                     done="2026-08-03T17:08:04+00:00")
    assert plan_done_sync([card], [], done_stack_id=9, now_iso=NOW_ISO) == []


def test_moves_a_ticked_card_out_of_todo_into_done():
    card = make_card(id=1, title="Chore", stack_id=22,
                     done="2026-08-04T09:00:00+00:00")
    actions = plan_done_sync([], [card], done_stack_id=9, now_iso=NOW_ISO)
    assert actions == [
        MoveToDoneByTickAction(card=card, card_title="Chore", target_stack_id=9)
    ]


def test_leaves_an_unticked_card_outside_done_alone():
    card = make_card(id=1, title="Chore", stack_id=22, done=None)
    assert plan_done_sync([], [card], done_stack_id=9, now_iso=NOW_ISO) == []


def test_done_sync_skips_archived_cards_in_both_directions():
    in_done = make_card(id=1, title="A", stack_id=9, done=None, archived=True)
    outside = make_card(id=2, title="B", stack_id=22, done=NOW_ISO, archived=True)
    assert plan_done_sync([in_done], [outside], done_stack_id=9, now_iso=NOW_ISO) == []


def test_done_sync_skips_trashed_cards_in_both_directions():
    in_done = make_card(id=1, title="A", stack_id=9, done=None, deleted_at=17543)
    outside = make_card(id=2, title="B", stack_id=22, done=NOW_ISO, deleted_at=17543)
    assert plan_done_sync([in_done], [outside], done_stack_id=9, now_iso=NOW_ISO) == []


def test_done_sync_converges_after_one_pass():
    """Applying the plan and re-planning must yield nothing to do."""
    unticked_in_done = make_card(id=1, title="A", stack_id=9, done=None)
    ticked_in_todo = make_card(id=2, title="B", stack_id=22, done=NOW_ISO)

    first = plan_done_sync([unticked_in_done], [ticked_in_todo],
                           done_stack_id=9, now_iso=NOW_ISO)
    assert len(first) == 2

    # Apply: the Done card gains a tick; the Todo card moves into Done.
    unticked_in_done.done = NOW_ISO
    ticked_in_todo.stack_id = 9

    second = plan_done_sync([unticked_in_done, ticked_in_todo], [],
                            done_stack_id=9, now_iso=NOW_ISO)
    assert second == []


def test_execute_tick_calls_patch_card_with_the_timestamp():
    card = make_card(id=110, title="Vanne planter", stack_id=9, done=None)
    deck = FakeDeck()
    summary = execute_plan(
        [TickDoneAction(card=card, card_title="Vanne planter", done_at=NOW_ISO)],
        mailbox=FakeMailbox(), deck=deck,
    )
    assert deck.patched == [(110, {"done": NOW_ISO})]
    assert summary.ticked == 1


def test_execute_move_by_tick_calls_move_card_at_order_zero():
    card = make_card(id=1, title="Chore", stack_id=22, done=NOW_ISO)
    deck = FakeDeck()
    summary = execute_plan(
        [MoveToDoneByTickAction(card=card, card_title="Chore", target_stack_id=9)],
        mailbox=FakeMailbox(), deck=deck,
    )
    assert deck.moves == [(1, 9, 0)]
    assert summary.moved_by_tick == 1


def test_execute_done_sync_actions_do_nothing_under_dry_run():
    card = make_card(id=110, title="Vanne planter", stack_id=9, done=None)
    deck = FakeDeck()
    summary = execute_plan(
        [TickDoneAction(card=card, card_title="Vanne planter", done_at=NOW_ISO)],
        mailbox=FakeMailbox(), deck=deck, dry_run=True,
    )
    assert deck.patched == []
    assert summary.ticked == 1
```

The existing `FakeDeck` in that test file needs a `patch_card(self, card, board_id=None, **changes)` recording `(card.id, changes)` into `self.patched`, and its `move_card` must accept the `order` and `clear_done` keywords.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/prog/nextcloud-deck-cli && python -m pytest tests/test_imap_deck_sync.py -v -k "tick or done_sync"`
Expected: FAIL with `ImportError: cannot import name 'TickDoneAction'`

- [ ] **Step 3: Add the actions**

In `imap_deck_sync.py`, after the existing `ArchiveAction` dataclass:

```python
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
```

Extend the `Action` union immediately below:

```python
Action = (
    UnstarAction | MoveToDoneAction | CreateCardAction
    | AssignLabelAction | ArchiveAction
    | TickDoneAction | MoveToDoneByTickAction
)
```

- [ ] **Step 4: Add the planner**

In `imap_deck_sync.py`, after `plan_archive`:

```python
def plan_done_sync(done_cards, open_cards, done_stack_id: int,
                   now_iso: str) -> list:
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
    """
    actions: list = []

    for card in done_cards or []:
        if getattr(card, "archived", False) or getattr(card, "deleted_at", 0):
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
        if getattr(card, "done", None) is not None:
            actions.append(
                MoveToDoneByTickAction(
                    card=card,
                    card_title=getattr(card, "title", "") or "",
                    target_stack_id=done_stack_id,
                )
            )

    return actions
```

- [ ] **Step 5: Extend the summary and the executor**

In `ExecutionSummary`, add two counters after `archived`:

```python
    ticked: int = 0
    moved_by_tick: int = 0
```

In `execute_plan`, add two branches immediately before the final `else:` that logs an unknown action type:

```python
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
```

`clear_done` is deliberately left at its default here: the tick that triggered the move must survive it.

Also extend `execute_plan`'s docstring, which lists the client methods it calls, to include `.patch_card(...)`.

- [ ] **Step 6: Wire the pass into `run()`**

Add `done_sync: bool = True` to `Config`, after `archive_done_after_days`.

Add `from datetime import datetime, timezone` to the module imports.

In `run()`, immediately after the `managed = fetch_managed(stacks)` line and its log call, and before the `archive_actions` block:

```python
    done_sync_actions: list = []
    if config.done_sync:
        open_cards = (
            (getattr(todo, "cards", None) or [])
            + (getattr(doing, "cards", None) or [])
        )
        done_sync_actions = plan_done_sync(
            done_cards=getattr(done, "cards", None) or [],
            open_cards=open_cards,
            done_stack_id=done.id,
            now_iso=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        )
        ticks = sum(1 for a in done_sync_actions if isinstance(a, TickDoneAction))
        log.info(
            "Done-tick pass: %d card(s) in %s to tick, %d ticked card(s) to move in",
            ticks, config.done_stack_name, len(done_sync_actions) - ticks,
        )
```

Then change the plan assembly so the done pass runs after the IMAP actions and before the archive actions:

```python
            plan = make_plan(
                starred=starred,
                managed=managed,
                stack_ids=stack_ids,
                email_label_id=email_label.id,
            ) + done_sync_actions + archive_actions
```

Finally extend the closing summary log to report the new counters:

```python
    log.info(
        "Done: created=%d moved=%d unstarred=%d labels_assigned=%d "
        "archived=%d ticked=%d moved_by_tick=%d failures=%d%s",
        summary.created, summary.moved, summary.unstarred,
        summary.labels_assigned, summary.archived, summary.ticked,
        summary.moved_by_tick, summary.failures,
        " (dry-run)" if config.dry_run else "",
    )
```

- [ ] **Step 7: Add the CLI flag**

In `nextcloud-deck-imap-sync.py`, add to the IMAP/modes argument group:

```python
    p.add_argument("--no-done-sync", action="store_false", dest="done_sync",
                   default=env("DONE_SYNC", "1").lower()
                   not in ("0", "false", "no", "off"),
                   help="Disable the done-tick <-> Done stack sync pass.")
```

and pass it into the `Config(...)` construction:

```python
        done_sync=args.done_sync,
```

- [ ] **Step 8: Run the whole suite**

Run: `cd ~/prog/nextcloud-deck-cli && python -m pytest tests/ -v`
Expected: PASS, including every pre-existing test. If existing `execute_plan` tests fail on the new `ExecutionSummary` fields, they were asserting on the whole summary object — update them to assert on the specific counters they care about.

- [ ] **Step 9: Document it in the README**

In the `# nextcloud-deck-imap-sync` section of `README.md`, after whatever it says about the archive pass, add:

````markdown
### done-tick sync

Deck's per-card done-tick and membership of the `Done` stack are kept in
agreement on every run:

- a card in `Done` with no done-tick gets one
- a card in `Todo` or `Doing` with a done-tick is moved into `Done`

Stack membership wins inside `Done`; the tick wins outside it. That is
loop-free — after one pass every card is either in `Done` and ticked, or
outside `Done` and unticked.

The consequence is that un-ticking a card while leaving it in `Done` does not
stick: the next run re-ticks it. To reopen a card, drag it out of `Done`.

Archived and trashed cards are left alone. Disable the pass with
`--no-done-sync` or `DONE_SYNC=0`.
````

- [ ] **Step 10: Commit**

```bash
cd ~/prog/nextcloud-deck-cli
git add imap_deck_sync.py nextcloud-deck-imap-sync.py tests/test_imap_deck_sync.py README.md
git commit -m "feat(sync): keep Deck's done-tick and the Done stack in agreement

A card in Done with no done-tick gets one; a card in Todo or Doing with a
done-tick moves into Done. Stack membership wins inside Done, the tick wins
outside it, so the rule converges after a single pass.

Runs as a third pass on the existing 5-minute timer, on the same stack
snapshot, after the IMAP passes and before the archive pass.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Stop the archive pass from eating recurring cards

A card carrying both `Email` and a `Repeat:` label would be archived after 7 days in `Done` and could never recur.

**Files:**
- Modify: `~/prog/nextcloud-deck-cli/imap_deck_sync.py` (`plan_archive`)
- Test: `~/prog/nextcloud-deck-cli/tests/test_imap_deck_sync.py`

**Interfaces:**
- Consumes: `DEFAULT_LABEL_PREFIX` from `deck_recur` (Task 5).
- Produces: `plan_archive(done_cards, done_at, email_label_id, now, max_age_days, recur_label_prefix=DEFAULT_LABEL_PREFIX)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_imap_deck_sync.py`, matching the fakes the existing `plan_archive` tests use:

```python
def test_archive_skips_cards_carrying_a_recurrence_label():
    old = 1_700_000_000
    now = old + 30 * 86400
    card = make_card(
        id=1, title="Monthly report", stack_id=9,
        labels=[FakeLabel(145, "Email"), FakeLabel(146, "Repeat: 25")],
    )
    actions = plan_archive(
        done_cards=[card], done_at={1: old}, email_label_id=145,
        now=now, max_age_days=7,
    )
    assert actions == []


def test_archive_still_archives_a_plain_email_card():
    old = 1_700_000_000
    now = old + 30 * 86400
    card = make_card(id=2, title="Plain email", stack_id=9,
                     labels=[FakeLabel(145, "Email")])
    actions = plan_archive(
        done_cards=[card], done_at={2: old}, email_label_id=145,
        now=now, max_age_days=7,
    )
    assert len(actions) == 1
    assert actions[0].card_id == 2


def test_archive_recurrence_guard_matches_prefix_case_insensitively():
    old = 1_700_000_000
    now = old + 30 * 86400
    card = make_card(id=3, title="Odd casing", stack_id=9,
                     labels=[FakeLabel(145, "Email"), FakeLabel(9, "repeat: mon")])
    assert plan_archive(
        done_cards=[card], done_at={3: old}, email_label_id=145,
        now=now, max_age_days=7,
    ) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ~/prog/nextcloud-deck-cli && python -m pytest tests/test_imap_deck_sync.py -v -k recurrence_label`
Expected: FAIL — the card is archived, so `actions` has one entry instead of none.

- [ ] **Step 3: Add the guard**

In `imap_deck_sync.py`, add the import near the top:

```python
from deck_recur import DEFAULT_LABEL_PREFIX
```

Change the `plan_archive` signature to:

```python
def plan_archive(done_cards, done_at: dict[int, int], email_label_id: int,
                 now: int, max_age_days: int,
                 recur_label_prefix: str = DEFAULT_LABEL_PREFIX) -> list[ArchiveAction]:
```

Add to its docstring, in the numbered list of conditions:

```
      5. it carries no recurrence label — a card that recurs must not be
         archived out from under the recurrence job
```

Inside the loop, immediately after the existing `label_ids` set is built and before the `email_label_id` check, add:

```python
        recur_prefix = (recur_label_prefix or "").strip().lower()
        if any(
            ((getattr(label, "title", "") or "").strip().lower()).startswith(recur_prefix)
            for label in (getattr(card, "labels", None) or [])
        ):
            continue
```

- [ ] **Step 4: Run the whole suite**

Run: `cd ~/prog/nextcloud-deck-cli && python -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/prog/nextcloud-deck-cli
git add imap_deck_sync.py tests/test_imap_deck_sync.py
git commit -m "fix(sync): never archive a card that carries a recurrence label

A card labelled both Email and Repeat: would be archived after 7 days in
Done and could never recur. Recurrence wins.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

# Part 4 — Deployment and Verification

### Task 10: Pin the library, deploy the timer

This is the task that touches the live system. Order matters: ncdeck 1.1.0 must already be published (Task 4) and installed before the sync is allowed to depend on it.

**Files:**
- Modify: `~/prog/nextcloud-deck-cli/requirements.txt`
- Create: `~/.local/share/chezmoi/dot_config/systemd/user/deck-recur.service`
- Create: `~/.local/share/chezmoi/dot_config/systemd/user/deck-recur.timer`

**Interfaces:**
- Consumes: `nextcloud-deck-client==1.1.0` on PyPI (Task 4); `deck-recur-wrapper.sh` (Task 7).
- Produces: an enabled `deck-recur.timer` firing daily.

- [ ] **Step 1: Confirm the installed library actually has the new methods**

```bash
python3 -c "
import ncdeck, inspect
from importlib.metadata import version
print('version', version('nextcloud-deck-client'))
print(inspect.signature(ncdeck.DeckClient.patch_card))
print(inspect.signature(ncdeck.DeckClient.move_card))
"
```

Expected: version `1.1.0` or later, and both signatures present. **If this fails, stop** — deploying now would break the running 5-minute sync, which is exactly what happened on 2026-08-04.

- [ ] **Step 2: Pin the floor**

In `requirements.txt`, change the `nextcloud-deck-client` line to:

```
nextcloud-deck-client>=1.1.0
```

- [ ] **Step 3: Create the service unit**

```bash
cat > ~/.local/share/chezmoi/dot_config/systemd/user/deck-recur.service <<'EOF'
# Managed by chezmoi - do not edit directly
[Unit]
Description=Return recurring Deck cards from Done to Todo
After=network.target
OnFailure=notify-failure@%n.service

[Service]
Type=oneshot
ExecStart=%h/prog/nextcloud-deck-cli/deck-recur-wrapper.sh
Environment=RUN_FROM_CRON=1
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
EOF
```

- [ ] **Step 4: Create the timer unit**

`Persistent=true` makes a run missed while the host was down fire at boot. That does not recover a missed trigger day — the job evaluates the date it actually runs on — but it does recover a run missed by a few minutes around a reboot.

```bash
cat > ~/.local/share/chezmoi/dot_config/systemd/user/deck-recur.timer <<'EOF'
# Managed by chezmoi - do not edit directly
[Unit]
Description=Run deck-recur daily

[Timer]
OnCalendar=*-*-* 04:15
RandomizedDelaySec=5min
FixedRandomDelay=true
Persistent=true

[Install]
WantedBy=timers.target
EOF
```

- [ ] **Step 5: Preview what chezmoi will change, then apply**

```bash
chezmoi diff
chezmoi apply
systemctl --user daemon-reload
```

Expected: the diff shows only the two new unit files. If it shows unrelated pending changes, report them and stop rather than applying someone else's half-finished work.

- [ ] **Step 6: Enable and start the timer**

```bash
systemctl --user enable --now deck-recur.timer
systemctl --user list-timers deck-recur.timer --all
```

Expected: the timer is listed with a NEXT time tomorrow at about 04:15.

- [ ] **Step 7: Commit the pin**

```bash
cd ~/prog/nextcloud-deck-cli
git add requirements.txt
git commit -m "chore: require nextcloud-deck-client >= 1.1.0

Both new features need patch_card and move_card(clear_done=).

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 8: Commit the chezmoi source**

```bash
cd ~/.local/share/chezmoi
git add dot_config/systemd/user/deck-recur.service dot_config/systemd/user/deck-recur.timer
git commit -m "feat(systemd): daily deck-recur timer

Returns recurring Deck cards from Done to Todo. Daily rather than on the
5-minute sync timer: at a shorter interval, re-completing a card on its own
trigger day would send it straight back.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

If the chezmoi repo has other uncommitted changes, commit only these two paths.

---

### Task 11: Verify against the live board

Board 4 is `Personal`. `Todo` is stack 22, `Doing` is 8, `Done` is 9. Cards **323 "Timeliste"** and **324 "Visma Expenses"** carry label 146 `Repeat: 25` and both sit in `Done` with a non-null `done`. Nine other cards in `Done` are unticked.

**Files:** none — this task changes no code.

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Dry-run the recurrence job on a non-trigger day**

```bash
cd ~/prog/nextcloud-deck-cli
./deck-recur-wrapper.sh --date 2026-08-24 --dry-run -v 2>&1 | tail -20
```

Expected: `0 of 11 card(s) in Done due`, no `[dry-run] would move` lines, exit 0.

- [ ] **Step 2: Dry-run on the trigger day**

```bash
cd ~/prog/nextcloud-deck-cli
./deck-recur-wrapper.sh --date 2026-08-25 --dry-run -v 2>&1 | tail -20
```

Expected: exactly two `[dry-run] would move card #323 ...` / `#324 ...` lines, both naming `Repeat: 25` and stack 22, and nothing about any other card. Exit 0.

**If any other card appears, stop and report it** — that means the label matching is too loose.

- [ ] **Step 3: Dry-run the done-tick pass**

```bash
cd ~/prog/nextcloud-deck-cli
./imap-deck-sync-wrapper.sh --dry-run -v 2>&1 | grep -i "done-tick\|would tick\|would move ticked"
```

Expected: a `Done-tick pass:` line reporting 9 cards to tick and 0 to move in, followed by nine `[dry-run] would tick card #...` lines. No real mutation.

- [ ] **Step 4: Confirm the live sync is still healthy**

The 5-minute timer has been running throughout. Check it did not start failing when the new code landed:

```bash
systemctl --user status imap-deck-sync.service --no-pager | tail -5
journalctl --user -u imap-deck-sync.service --since "30 min ago" -p warning --no-pager | tail -20
```

Expected: last run succeeded, no new errors. `'DeckClient' object has no attribute 'patch_card'` here means the library was not upgraded before the consumer — fix by completing Task 10 Step 1.

- [ ] **Step 5: Let one real done-tick pass run and confirm the board**

Wait for the next scheduled run, then verify the nine cards are ticked:

```bash
journalctl --user -u imap-deck-sync.service --since "10 min ago" --no-pager | grep -i "ticked="
```

Expected: a `Done:` summary line with `ticked=9` on the first real run, then `ticked=0` on subsequent runs — proving convergence.

- [ ] **Step 6: Report the outcome**

Report: the two dry-run outputs, the `ticked=` counts from the first and second real runs, and whether any card other than 323/324 was planned for recurrence.

---

## Notes for the implementer

- **Do not run the recurrence job for real against the live board without `--dry-run`** unless the plan explicitly says to. Moving cards 323 and 324 out of `Done` on a non-trigger day would have to be undone by hand.
- The 5-minute `imap-deck-sync.timer` runs throughout this work. Between Task 8 landing and Task 10 completing, the deployed code and the installed library must stay compatible — this is why Task 4 (release) precedes Task 10 (pin), and why Task 10 Step 1 is a hard gate.
- `tests/conftest.py` puts the repo root on `sys.path`, so tests import `deck_recur` and `imap_deck_sync` directly with no install step.
- `imap_deck_sync.py` imports `imap_tools` lazily inside functions so the pure-logic tests run without it. Keep that property: do not add a module-level `imap_tools` import.

---

### Task 12: Rename imap-deck-sync to deck-sync

With the done-tick pass added, two of the module's three passes are Deck-internal and the `imap` in its name misleads. This task is a **pure rename: no behaviour changes at all.** Keeping it separate and last means every feature diff above stays free of churn, and this one is trivial to review or revert.

**Files:**

| From | To |
|---|---|
| `imap_deck_sync.py` | `deck_sync.py` |
| `nextcloud-deck-imap-sync.py` | `nextcloud-deck-sync.py` |
| `imap-deck-sync-wrapper.sh` | `deck-sync-wrapper.sh` |
| `tests/test_imap_deck_sync.py` | `tests/test_deck_sync.py` |
| chezmoi `dot_config/systemd/user/imap-deck-sync.service` | `deck-sync.service` |
| chezmoi `dot_config/systemd/user/imap-deck-sync.timer` | `deck-sync.timer` |

Also modified: `README.md`, and the olen logger app name and Discord strings inside the entrypoint.

**Interfaces:**
- Consumes: everything above, verified working in Task 11.
- Produces: no API change. `deck_recur.py` is untouched — it never imported the sync module. `deck_sync.py` keeps its `from deck_recur import DEFAULT_LABEL_PREFIX` import from Task 9.

- [ ] **Step 1: Find every reference before moving anything**

```bash
cd ~/prog/nextcloud-deck-cli
grep -rn "imap_deck_sync\|imap-deck-sync" \
    --include='*.py' --include='*.sh' --include='*.md' --include='*.txt' \
    . | grep -v '^./docs/superpowers/' | grep -v '^./.git/'
grep -rn "imap-deck-sync" ~/.local/share/chezmoi/dot_config/ ~/.config/systemd/user/ 2>/dev/null
```

Record the full list in your report. Files under `docs/superpowers/` are historical spec and plan records — **leave those unchanged**, they describe what was true when written. Everything else must be updated.

- [ ] **Step 2: Stop the old timer first**

The unit must be disabled before its `ExecStart` target disappears, or the next tick fires `notify-failure@imap-deck-sync.service` at you.

```bash
systemctl --user disable --now imap-deck-sync.timer
systemctl --user list-timers imap-deck-sync.timer --all
```

Expected: the timer is gone from the list. The sync is now stopped; the rest of this task should follow promptly.

- [ ] **Step 3: Move the repo files**

```bash
cd ~/prog/nextcloud-deck-cli
git mv imap_deck_sync.py deck_sync.py
git mv nextcloud-deck-imap-sync.py nextcloud-deck-sync.py
git mv imap-deck-sync-wrapper.sh deck-sync-wrapper.sh
git mv tests/test_imap_deck_sync.py tests/test_deck_sync.py
```

- [ ] **Step 4: Update the imports and strings**

In `nextcloud-deck-sync.py`:

```python
from deck_sync import Config, run, validate_archive_days  # noqa: E402
```

and change the olen app name from `"imap-deck-sync"` to `"deck-sync"`:

```python
    APP_CONFIG.set(ATTR_APP, "name", "deck-sync")
```

In the same file, update the two Discord blocks — titles `imap-deck-sync completed with per-card failures` and `imap-deck-sync failed` become `deck-sync ...`, the `app=` keyword becomes `"deck-sync"`, and the log path in both messages becomes `~/bin/logs/deck-sync.log`.

In `tests/test_deck_sync.py`, change every `from imap_deck_sync import ...` and `import imap_deck_sync` to `deck_sync`.

In `deck-sync-wrapper.sh`, change the `exec` line to:

```bash
exec /home/olen/prog/nextcloud-deck-cli/nextcloud-deck-sync.py \
```

- [ ] **Step 5: Verify nothing still refers to the old names**

```bash
cd ~/prog/nextcloud-deck-cli
grep -rn "imap_deck_sync\|imap-deck-sync" \
    --include='*.py' --include='*.sh' --include='*.txt' . | grep -v '^./.git/'
```

Expected: no output. (`.md` is excluded here only because `README.md` is handled in Step 7; if you have already done Step 7, add `--include='*.md'` and expect no output outside `docs/superpowers/`.)

- [ ] **Step 6: Run the whole suite**

Run: `cd ~/prog/nextcloud-deck-cli && python -m pytest tests/ -v`
Expected: PASS, exactly the same test count as at the end of Task 11. A rename that changes the number of passing tests is not a rename.

- [ ] **Step 7: Update the README heading**

Change the section heading `# nextcloud-deck-imap-sync` to `# nextcloud-deck-sync`, and update any prose or usage block in that section that names the old script, wrapper, or unit. The `# nextcloud-deck-recur` section added in Task 7 needs no change.

- [ ] **Step 8: Verify the CLI still runs**

```bash
cd ~/prog/nextcloud-deck-cli
./nextcloud-deck-sync.py --help >/dev/null && echo "cli ok"
./deck-sync-wrapper.sh --dry-run 2>&1 | tail -5
```

Expected: `cli ok`, and the wrapper completes a full dry-run with a `Done:` summary line and exit 0.

- [ ] **Step 9: Commit the repo rename**

```bash
cd ~/prog/nextcloud-deck-cli
git add -A
git commit -m "refactor: rename imap-deck-sync to deck-sync

Two of the module's three passes are now Deck-internal -- the archive pass
and the done-tick sync -- so the imap in the name misleads. Pure rename:
no behaviour change.

Historical specs and plans under docs/superpowers keep the old name, since
they describe what was true when they were written.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 10: Rename the chezmoi units**

```bash
cd ~/.local/share/chezmoi
git mv dot_config/systemd/user/imap-deck-sync.service dot_config/systemd/user/deck-sync.service
git mv dot_config/systemd/user/imap-deck-sync.timer dot_config/systemd/user/deck-sync.timer
```

Then edit the two files. In `deck-sync.service`, the `Description` becomes `Sync IMAP starred messages, done-ticks and archiving on a Nextcloud Deck board` and the `ExecStart` becomes:

```
ExecStart=%h/prog/nextcloud-deck-cli/deck-sync-wrapper.sh
```

In `deck-sync.timer`, the `Description` becomes `Run deck-sync every 5 minutes`. Every other line in both files, including the chezmoi header, stays exactly as it is.

- [ ] **Step 11: Apply, remove the stale targets, and enable**

`chezmoi apply` writes the new units but does **not** delete the old deployed ones — they are no longer in the source, so chezmoi simply stops managing them. They must be removed by hand or systemd will keep offering a unit whose `ExecStart` no longer exists.

```bash
chezmoi diff
chezmoi apply
rm -f ~/.config/systemd/user/imap-deck-sync.service ~/.config/systemd/user/imap-deck-sync.timer
systemctl --user daemon-reload
systemctl --user enable --now deck-sync.timer
systemctl --user list-timers deck-sync.timer --all
```

Expected: `chezmoi diff` shows only the two unit files; the timer is listed with a NEXT time within 5 minutes.

- [ ] **Step 12: Watch a real run complete**

```bash
systemctl --user start deck-sync.service
journalctl --user -u deck-sync.service --since "2 min ago" --no-pager | tail -20
```

Expected: a `Done:` summary line with `failures=0`. Confirm `~/bin/logs/deck-sync.log` now exists.

- [ ] **Step 13: Commit the chezmoi source**

```bash
cd ~/.local/share/chezmoi
git add dot_config/systemd/user/deck-sync.service dot_config/systemd/user/deck-sync.timer \
        dot_config/systemd/user/imap-deck-sync.service dot_config/systemd/user/imap-deck-sync.timer
git commit -m "refactor(systemd): rename imap-deck-sync units to deck-sync

The service now runs three passes, only one of which is an IMAP sync.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

If the chezmoi repo has other uncommitted changes, commit only these paths.

- [ ] **Step 14: Report**

Report: the reference list from Step 1, the test count before and after, and confirmation that `deck-sync.timer` is enabled and has completed a run with `failures=0`.
