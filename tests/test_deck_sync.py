from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from deck_sync import (
    ARCHIVE_MAX_DAYS_LIMIT,
    parse_marker,
    build_marker,
    format_card_title,
    StarredMessage,
    ManagedCard,
    UnstarAction,
    MoveToDoneAction,
    CreateCardAction,
    AssignLabelAction,
    ArchiveAction,
    TickDoneAction,
    MoveToDoneByTickAction,
    StackIds,
    make_plan,
    latest_done_at,
    plan_archive,
    plan_done_sync,
    fetch_managed,
    fetch_starred,
    fetch_deck_activity,
    execute_plan,
    ExecutionSummary,
    Config,
    validate_archive_days,
    validate_recur_label_prefix,
    run,
)
from deck_recur import DEFAULT_LABEL_PREFIX


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


def _stack_ids():
    return StackIds(todo=1, doing=2, done=3)


def _starred(msgid, uid="1", name="Alice", addr="alice@example.com", subject="Hi"):
    return StarredMessage(message_id=msgid, uid=uid, from_name=name, from_addr=addr, subject=subject)


def _managed(msgid, stack_id, card_id=42, label_ids=(), label_titles=()):
    # Card is opaque to the planner — we only need an identity for downstream
    # actions, plus the label titles Pass B's recurrence exemption reads.
    # We pass a SimpleNamespace so production code can attach a real Card here.
    from types import SimpleNamespace
    card = SimpleNamespace(
        id=card_id,
        title="(irrelevant)",
        labels=[_FakeLabel(id=900 + i, title=t) for i, t in enumerate(label_titles)],
    )
    return ManagedCard(
        message_id=msgid,
        card=card,
        stack_id=stack_id,
        label_ids=frozenset(label_ids),
    )


EMAIL_LABEL_ID = 99


class TestMakePlan:
    def test_empty_inputs_yield_empty_plan(self):
        assert make_plan(starred={}, managed={}, stack_ids=_stack_ids(), email_label_id=EMAIL_LABEL_ID) == []

    def test_new_starred_message_creates_card_in_todo(self):
        starred = {"<a@x>": _starred("<a@x>")}
        plan = make_plan(starred=starred, managed={}, stack_ids=_stack_ids(), email_label_id=EMAIL_LABEL_ID)
        assert plan == [
            CreateCardAction(
                stack_id=1,
                title="Alice: Hi",
                description="<!-- imap-sync: message-id=<a@x> -->\n",
                message_id="<a@x>",
                label_ids=(EMAIL_LABEL_ID,),
            )
        ]

    def test_managed_card_in_todo_for_still_starred_message_is_noop(self):
        starred = {"<a@x>": _starred("<a@x>")}
        managed = {"<a@x>": _managed("<a@x>", stack_id=1, label_ids=(EMAIL_LABEL_ID,))}
        assert make_plan(starred=starred, managed=managed, stack_ids=_stack_ids(), email_label_id=EMAIL_LABEL_ID) == []

    def test_managed_card_in_doing_for_unstarred_message_moves_to_done(self):
        managed = {"<a@x>": _managed("<a@x>", stack_id=2, label_ids=(EMAIL_LABEL_ID,))}
        plan = make_plan(starred={}, managed=managed, stack_ids=_stack_ids(), email_label_id=EMAIL_LABEL_ID)
        assert len(plan) == 1
        assert isinstance(plan[0], MoveToDoneAction)
        assert plan[0].card is managed["<a@x>"].card
        assert plan[0].target_stack_id == 3

    def test_managed_card_in_todo_for_unstarred_message_moves_to_done(self):
        managed = {"<a@x>": _managed("<a@x>", stack_id=1, label_ids=(EMAIL_LABEL_ID,))}
        plan = make_plan(starred={}, managed=managed, stack_ids=_stack_ids(), email_label_id=EMAIL_LABEL_ID)
        assert isinstance(plan[0], MoveToDoneAction)

    def test_managed_card_in_done_with_still_starred_message_clears_flag(self):
        starred = {"<a@x>": _starred("<a@x>", uid="77")}
        managed = {"<a@x>": _managed("<a@x>", stack_id=3, label_ids=(EMAIL_LABEL_ID,))}
        plan = make_plan(starred=starred, managed=managed, stack_ids=_stack_ids(), email_label_id=EMAIL_LABEL_ID)
        assert plan == [UnstarAction(uid="77", message_id="<a@x>")]

    def test_managed_card_in_done_with_unstarred_message_is_noop(self):
        managed = {"<a@x>": _managed("<a@x>", stack_id=3, label_ids=(EMAIL_LABEL_ID,))}
        assert make_plan(starred={}, managed=managed, stack_ids=_stack_ids(), email_label_id=EMAIL_LABEL_ID) == []

    def test_managed_card_in_custom_stack_is_left_alone(self):
        # User moved an email-card to a "Later" stack (id=99). Sync recognises
        # the marker so it must NOT create a duplicate even though the message
        # is still in the starred set.
        starred = {"<a@x>": _starred("<a@x>")}
        managed = {"<a@x>": _managed("<a@x>", stack_id=99, label_ids=(EMAIL_LABEL_ID,))}
        plan = make_plan(starred=starred, managed=managed, stack_ids=_stack_ids(), email_label_id=EMAIL_LABEL_ID)
        assert plan == []

    def test_pass_a_clears_flag_then_pass_c_does_not_recreate(self):
        # Card in Done + message still starred → unstar; no Create on same run.
        starred = {"<a@x>": _starred("<a@x>", uid="9")}
        managed = {"<a@x>": _managed("<a@x>", stack_id=3, label_ids=(EMAIL_LABEL_ID,))}
        plan = make_plan(starred=starred, managed=managed, stack_ids=_stack_ids(), email_label_id=EMAIL_LABEL_ID)
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
            "<keep@x>": _managed("<keep@x>", stack_id=1, label_ids=(EMAIL_LABEL_ID,)),           # active, still starred — no-op
            "<unstar-me@x>": _managed("<unstar-me@x>", stack_id=3, label_ids=(EMAIL_LABEL_ID,)), # in Done, still starred — clear flag
            "<gone@x>": _managed("<gone@x>", stack_id=2, label_ids=(EMAIL_LABEL_ID,)),           # was doing, no longer starred — to Done
        }
        plan = make_plan(starred=starred, managed=managed, stack_ids=_stack_ids(), email_label_id=EMAIL_LABEL_ID)

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
        assert create.label_ids == (EMAIL_LABEL_ID,)

    def test_unstar_pass_runs_before_create_pass(self):
        # If a message is in Done AND somehow still in starred, we must not
        # *also* create a new card for it. (Same as test above but verifies
        # ordering invariant.)
        starred = {"<x@x>": _starred("<x@x>", uid="11")}
        managed = {"<x@x>": _managed("<x@x>", stack_id=3, label_ids=(EMAIL_LABEL_ID,))}
        plan = make_plan(starred=starred, managed=managed, stack_ids=_stack_ids(), email_label_id=EMAIL_LABEL_ID)
        assert not any(isinstance(a, CreateCardAction) for a in plan)

    def test_create_action_carries_email_label(self):
        starred = {"<a@x>": _starred("<a@x>")}
        plan = make_plan(starred=starred, managed={}, stack_ids=_stack_ids(),
                         email_label_id=EMAIL_LABEL_ID)
        creates = [a for a in plan if isinstance(a, CreateCardAction)]
        assert len(creates) == 1
        assert creates[0].label_ids == (EMAIL_LABEL_ID,)

    def test_pass_d_emits_assign_label_for_managed_card_missing_email_label(self):
        # Managed card in Done, message no longer starred, missing Email label.
        # Pass D should emit AssignLabelAction. (No Move/Unstar/Create.)
        managed = {"<a@x>": _managed("<a@x>", stack_id=3, label_ids=())}
        plan = make_plan(starred={}, managed=managed, stack_ids=_stack_ids(),
                         email_label_id=EMAIL_LABEL_ID)
        assigns = [a for a in plan if isinstance(a, AssignLabelAction)]
        assert len(assigns) == 1
        assert assigns[0].card_id == 42
        assert assigns[0].label_id == EMAIL_LABEL_ID
        assert assigns[0].stack_id == 3

    def test_pass_d_skips_managed_card_already_carrying_email_label(self):
        managed = {"<a@x>": _managed("<a@x>", stack_id=1, label_ids=(EMAIL_LABEL_ID,))}
        starred = {"<a@x>": _starred("<a@x>")}
        plan = make_plan(starred=starred, managed=managed, stack_ids=_stack_ids(),
                         email_label_id=EMAIL_LABEL_ID)
        assert not any(isinstance(a, AssignLabelAction) for a in plan)

    def test_pass_d_tags_managed_card_in_custom_stack(self):
        # Origin label should be applied even to cards in custom stacks.
        managed = {"<a@x>": _managed("<a@x>", stack_id=99, label_ids=())}
        plan = make_plan(starred={}, managed=managed, stack_ids=_stack_ids(),
                         email_label_id=EMAIL_LABEL_ID)
        assigns = [a for a in plan if isinstance(a, AssignLabelAction)]
        assert len(assigns) == 1
        assert assigns[0].stack_id == 99


class TestMakePlanRecurrenceExemption:
    """Pass B must not undo what the recurrence job just did.

    A card created from a starred email keeps its imap-sync marker forever, so
    it stays in `managed`. Add `Repeat: 25` to it and complete it once: Pass A
    unstars the message permanently. On the 25th the recurrence job puts the
    card back in Todo — and without this exemption the next 5-minute sync would
    move it straight back to Done.
    """

    def test_pass_b_skips_a_card_carrying_a_recurrence_label(self):
        managed = {
            "<a@x>": _managed("<a@x>", stack_id=1, label_ids=(EMAIL_LABEL_ID,),
                              label_titles=("Email", "Repeat: 25")),
        }
        plan = make_plan(starred={}, managed=managed, stack_ids=_stack_ids(),
                         email_label_id=EMAIL_LABEL_ID)
        assert [a for a in plan if isinstance(a, MoveToDoneAction)] == []

    def test_pass_b_still_moves_a_managed_card_without_a_recurrence_label(self):
        # The regression guard: the ordinary email flow must keep working.
        managed = {
            "<a@x>": _managed("<a@x>", stack_id=1, label_ids=(EMAIL_LABEL_ID,),
                              label_titles=("Email",)),
        }
        plan = make_plan(starred={}, managed=managed, stack_ids=_stack_ids(),
                         email_label_id=EMAIL_LABEL_ID)
        moves = [a for a in plan if isinstance(a, MoveToDoneAction)]
        assert len(moves) == 1
        assert moves[0].target_stack_id == 3

    def test_pass_b_exemption_also_covers_the_doing_stack(self):
        managed = {
            "<a@x>": _managed("<a@x>", stack_id=2, label_ids=(EMAIL_LABEL_ID,),
                              label_titles=("Repeat: daily",)),
        }
        plan = make_plan(starred={}, managed=managed, stack_ids=_stack_ids(),
                         email_label_id=EMAIL_LABEL_ID)
        assert [a for a in plan if isinstance(a, MoveToDoneAction)] == []

    def test_pass_b_exemption_matches_the_prefix_case_insensitively(self):
        managed = {
            "<a@x>": _managed("<a@x>", stack_id=1, label_ids=(EMAIL_LABEL_ID,),
                              label_titles=("  repeat: mon  ",)),
        }
        plan = make_plan(starred={}, managed=managed, stack_ids=_stack_ids(),
                         email_label_id=EMAIL_LABEL_ID)
        assert [a for a in plan if isinstance(a, MoveToDoneAction)] == []

    def test_pass_b_honours_a_custom_recurrence_prefix(self):
        managed = {
            "<a@x>": _managed("<a@x>", stack_id=1, label_ids=(EMAIL_LABEL_ID,),
                              label_titles=("Gjenta: 25",)),
        }
        # Default prefix does not recognise it -> card is moved.
        assert any(
            isinstance(a, MoveToDoneAction)
            for a in make_plan(starred={}, managed=managed, stack_ids=_stack_ids(),
                               email_label_id=EMAIL_LABEL_ID)
        )
        # Configured prefix does -> card is left alone.
        plan = make_plan(starred={}, managed=managed, stack_ids=_stack_ids(),
                         email_label_id=EMAIL_LABEL_ID,
                         recur_label_prefix="Gjenta:")
        assert [a for a in plan if isinstance(a, MoveToDoneAction)] == []

    def test_empty_prefix_does_not_exempt_every_card(self):
        # "".startswith("") is True for every label, so a naive implementation
        # would treat the whole board as recurring and stop syncing entirely.
        # An empty prefix is rejected at startup; if one gets through anyway,
        # fail towards the ordinary email flow.
        managed = {
            "<a@x>": _managed("<a@x>", stack_id=1, label_ids=(EMAIL_LABEL_ID,),
                              label_titles=("Email",)),
        }
        plan = make_plan(starred={}, managed=managed, stack_ids=_stack_ids(),
                         email_label_id=EMAIL_LABEL_ID, recur_label_prefix="")
        assert len([a for a in plan if isinstance(a, MoveToDoneAction)]) == 1

    def test_pass_a_is_unaffected_by_a_recurrence_label(self):
        # A recurring card sitting in Done with its message still starred must
        # still get the star cleared — the exemption is Pass B only.
        managed = {
            "<a@x>": _managed("<a@x>", stack_id=3, label_ids=(EMAIL_LABEL_ID,),
                              label_titles=("Repeat: 25",)),
        }
        plan = make_plan(starred={"<a@x>": _starred("<a@x>")}, managed=managed,
                         stack_ids=_stack_ids(), email_label_id=EMAIL_LABEL_ID)
        assert [a for a in plan if isinstance(a, UnstarAction)] == [
            UnstarAction(uid="1", message_id="<a@x>")
        ]


class _FakeLabel:
    """Minimal stand-in for ncdeck.Label."""
    def __init__(self, id, title=""):
        self.id = id
        self.title = title


class _FakeCard:
    """Minimal stand-in for ncdeck.Card."""
    def __init__(self, id, description="", stack_id=None, labels=None):
        self.id = id
        self.description = description
        self.stack_id = stack_id
        self.labels = labels or []


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

    def test_label_ids_default_to_empty_frozenset(self):
        stacks = [_FakeStack(id=1, cards=[
            _FakeCard(id=10, description="<!-- imap-sync: message-id=<a@x> -->", stack_id=1, labels=[]),
        ])]
        managed = fetch_managed(stacks=stacks)
        assert managed["<a@x>"].label_ids == frozenset()

    def test_label_ids_populated_from_card_labels(self):
        stacks = [_FakeStack(id=1, cards=[
            _FakeCard(
                id=10,
                description="<!-- imap-sync: message-id=<a@x> -->",
                stack_id=1,
                labels=[_FakeLabel(id=7), _FakeLabel(id=42)],
            ),
        ])]
        managed = fetch_managed(stacks=stacks)
        assert managed["<a@x>"].label_ids == frozenset({7, 42})

    def test_handles_card_with_no_labels_attribute(self):
        # Some serialised Cards may not have a labels attribute at all.
        class _CardWithoutLabels:
            id = 10
            description = "<!-- imap-sync: message-id=<a@x> -->"
            stack_id = 1
        stacks = [_FakeStack(id=1, cards=[_CardWithoutLabels()])]
        managed = fetch_managed(stacks=stacks)
        assert managed["<a@x>"].label_ids == frozenset()


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


class _FakeMailbox:
    """imap_tools MailBox stand-in supporting just flag(uids, flag_set, value)."""
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
    def __init__(self, fail_creates=False, fail_moves_for_card_ids=(),
                 fail_assigns_for_label_ids=()):
        self.created = []
        self.moved = []
        self.assigned = []
        self.patched = []
        self.fail_creates = fail_creates
        self.fail_moves_for_card_ids = set(fail_moves_for_card_ids)
        self.fail_assigns_for_label_ids = set(fail_assigns_for_label_ids)
        self._next_card_id = 1000

    def create_card(self, stack_id, title, description="", **kwargs):
        if self.fail_creates:
            raise RuntimeError("simulated create failure")
        self._next_card_id += 1
        new_id = self._next_card_id
        card = SimpleNamespace(id=new_id, title=title, stack_id=stack_id, description=description)
        self.created.append({"id": new_id, "stack_id": stack_id, "title": title, "description": description})
        return card

    def move_card(self, card, target_stack_id, order=0, clear_done=False, board_id=None):
        if card.id in self.fail_moves_for_card_ids:
            raise RuntimeError(f"simulated move failure for card {card.id}")
        self.moved.append({
            "card_id": card.id, "target": target_stack_id,
            "order": order, "clear_done": clear_done,
        })
        card.stack_id = target_stack_id
        return card

    def assign_label(self, stack_id, card_id, label_id, **kwargs):
        if label_id in self.fail_assigns_for_label_ids:
            raise RuntimeError(f"simulated label failure for label {label_id}")
        self.assigned.append({"stack_id": stack_id, "card_id": card_id, "label_id": label_id})

    def patch_card(self, card, board_id=None, **changes):
        for k, v in changes.items():
            setattr(card, k, v)
        self.patched.append((card.id, changes))
        return card


class TestExecutePlan:
    def test_empty_plan_is_noop(self):
        mailbox, deck = _FakeMailbox(), _FakeDeck()
        summary = execute_plan(plan=[], mailbox=mailbox, deck=deck, dry_run=False)
        assert summary == ExecutionSummary(
            created=0, moved=0, unstarred=0, labels_assigned=0, failures=0
        )
        assert mailbox.flag_calls == []
        assert deck.created == []
        assert deck.moved == []
        assert deck.assigned == []

    def test_creates_card_and_applies_labels(self):
        mailbox, deck = _FakeMailbox(), _FakeDeck()
        plan = [CreateCardAction(
            stack_id=1, title="t", description="d", message_id="<a@x>",
            label_ids=(7,),
        )]
        summary = execute_plan(plan=plan, mailbox=mailbox, deck=deck, dry_run=False)
        assert summary.created == 1
        assert summary.labels_assigned == 1  # the post-create label application
        assert len(deck.created) == 1
        assert deck.created[0]["title"] == "t"
        # Label assigned to the new card's id
        new_card_id = deck.created[0]["id"]
        assert deck.assigned == [{"stack_id": 1, "card_id": new_card_id, "label_id": 7}]

    def test_create_with_empty_label_ids_skips_assign(self):
        mailbox, deck = _FakeMailbox(), _FakeDeck()
        plan = [CreateCardAction(
            stack_id=1, title="t", description="d", message_id="<a@x>",
            label_ids=(),
        )]
        summary = execute_plan(plan=plan, mailbox=mailbox, deck=deck, dry_run=False)
        assert summary.created == 1
        assert summary.labels_assigned == 0
        assert deck.assigned == []

    def test_moves_card(self):
        mailbox, deck = _FakeMailbox(), _FakeDeck()
        card = SimpleNamespace(id=42, title="t", stack_id=2)
        plan = [MoveToDoneAction(card=card, target_stack_id=3)]
        summary = execute_plan(plan=plan, mailbox=mailbox, deck=deck, dry_run=False)
        assert summary.moved == 1
        assert deck.moved == [{"card_id": 42, "target": 3, "order": 0, "clear_done": False}]

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

    def test_assigns_label_to_existing_card(self):
        mailbox, deck = _FakeMailbox(), _FakeDeck()
        plan = [AssignLabelAction(stack_id=1, card_id=42, label_id=7)]
        summary = execute_plan(plan=plan, mailbox=mailbox, deck=deck, dry_run=False)
        assert summary.labels_assigned == 1
        assert deck.assigned == [{"stack_id": 1, "card_id": 42, "label_id": 7}]

    def test_dry_run_performs_no_mutations(self):
        mailbox, deck = _FakeMailbox(), _FakeDeck()
        card = SimpleNamespace(id=42, title="t", stack_id=2)
        plan = [
            CreateCardAction(stack_id=1, title="t", description="d", message_id="<a@x>", label_ids=(7,)),
            MoveToDoneAction(card=card, target_stack_id=3),
            UnstarAction(uid="55", message_id="<a@x>"),
            AssignLabelAction(stack_id=2, card_id=43, label_id=7),
        ]
        summary = execute_plan(plan=plan, mailbox=mailbox, deck=deck, dry_run=True)
        # Counts reflect what would happen (create + post-create-label + move + unstar + assign)
        assert summary == ExecutionSummary(
            created=1, moved=1, unstarred=1, labels_assigned=2, failures=0
        )
        # But no IO was performed
        assert deck.created == [] and deck.moved == [] and deck.assigned == []
        assert mailbox.flag_calls == []

    def test_per_action_failures_increment_failure_counter_and_continue(self, caplog):
        import logging
        mailbox = _FakeMailbox(fail_on=["bad-uid"])
        deck = _FakeDeck(fail_moves_for_card_ids={42}, fail_assigns_for_label_ids={99})
        card_good = SimpleNamespace(id=43, title="t", stack_id=2)
        card_bad = SimpleNamespace(id=42, title="t", stack_id=2)
        plan = [
            UnstarAction(uid="bad-uid", message_id="<a@x>"),
            MoveToDoneAction(card=card_bad, target_stack_id=3),                     # fails
            MoveToDoneAction(card=card_good, target_stack_id=3),                    # succeeds
            CreateCardAction(stack_id=1, title="t", description="d", message_id="<b@x>", label_ids=()),  # succeeds (no labels)
            AssignLabelAction(stack_id=1, card_id=43, label_id=99),                 # fails
        ]
        with caplog.at_level(logging.WARNING):
            summary = execute_plan(plan=plan, mailbox=mailbox, deck=deck, dry_run=False)
        assert summary.failures == 3
        assert summary.created == 1
        assert summary.moved == 1
        assert summary.unstarred == 0
        assert summary.labels_assigned == 0
        # All five actions were attempted; failures didn't abort the run
        assert deck.created and deck.moved

    def test_create_label_assignment_failure_counts_as_failure_not_unaffecting_create(self):
        # If the post-create label assign fails, the card was still created.
        # The failure increments `failures` but `created` stays at 1.
        mailbox = _FakeMailbox()
        deck = _FakeDeck(fail_assigns_for_label_ids={7})
        plan = [CreateCardAction(
            stack_id=1, title="t", description="d", message_id="<a@x>",
            label_ids=(7,),
        )]
        summary = execute_plan(plan=plan, mailbox=mailbox, deck=deck, dry_run=False)
        assert summary.created == 1
        assert summary.labels_assigned == 0
        assert summary.failures == 1
        assert deck.created  # card was still created
        assert deck.assigned == []  # label failed


class _FakeDeckLabels:
    """DeckClient stand-in supporting just get_board_labels / create_label."""
    def __init__(self, existing_labels=None, create_returns=None):
        self.existing_labels = existing_labels or []
        self.create_returns = create_returns
        self.create_calls = []

    def get_board_labels(self, board_id=None):
        return self.existing_labels

    def create_label(self, title="", color="808080", board_id=None):
        self.create_calls.append({"board_id": board_id, "title": title, "color": color})
        return self.create_returns


class TestFindOrCreateEmailLabel:
    def test_returns_existing_label_matched_by_title(self):
        from deck_sync import _find_or_create_email_label
        existing = SimpleNamespace(id=7, title="Email", color="123456")
        other = SimpleNamespace(id=8, title="Other", color="123456")
        deck = _FakeDeckLabels(existing_labels=[other, existing])
        result = _find_or_create_email_label(deck, board_id=4, name="Email", color="808080")
        assert result is existing
        assert deck.create_calls == []

    def test_match_is_by_title_only_not_color(self):
        # A same-color label with a different title must not match.
        from deck_sync import _find_or_create_email_label
        existing_other = SimpleNamespace(id=8, title="Spam", color="808080")
        created = SimpleNamespace(id=99, title="Email", color="808080")
        deck = _FakeDeckLabels(existing_labels=[existing_other], create_returns=created)
        result = _find_or_create_email_label(deck, board_id=4, name="Email", color="808080")
        assert result is created
        assert deck.create_calls == [{"board_id": 4, "title": "Email", "color": "808080"}]

    def test_creates_label_when_absent(self):
        from deck_sync import _find_or_create_email_label
        created = SimpleNamespace(id=42, title="Email", color="abcdef")
        deck = _FakeDeckLabels(existing_labels=[], create_returns=created)
        result = _find_or_create_email_label(deck, board_id=4, name="Email", color="abcdef")
        assert result is created
        assert deck.create_calls == [{"board_id": 4, "title": "Email", "color": "abcdef"}]

    def test_raises_runtime_error_if_create_returns_none(self):
        import pytest
        from deck_sync import _find_or_create_email_label
        deck = _FakeDeckLabels(existing_labels=[], create_returns=None)
        with pytest.raises(RuntimeError, match="Email"):
            _find_or_create_email_label(deck, board_id=4, name="Email", color="808080")


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

    def test_object_id_is_coerced_to_int(self):
        # The live feed types board/stack ids as strings (handled above via
        # str() comparison); guard the same possibility for object_id so a
        # string card id still resolves to the int key plan_archive looks up
        # via card.id (always int, from the Deck library's parser).
        entries = [activity_entry("319", "2026-07-29T12:31:01+00:00")]
        assert latest_done_at(entries, 4, 9) == {319: 1785328261}


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

    def test_archive_skips_cards_carrying_a_recurrence_label(self):
        old = 1_700_000_000
        now = old + 30 * 86400
        card = make_card(
            id=1, title="Monthly report", stack_id=9,
            labels=[_FakeLabel(145, "Email"), _FakeLabel(146, "Repeat: 25")],
        )
        actions = plan_archive(
            done_cards=[card], done_at={1: old}, email_label_id=145,
            now=now, max_age_days=7,
        )
        assert actions == []

    def test_archive_still_archives_a_plain_email_card(self):
        old = 1_700_000_000
        now = old + 30 * 86400
        card = make_card(id=2, title="Plain email", stack_id=9,
                         labels=[_FakeLabel(145, "Email")])
        actions = plan_archive(
            done_cards=[card], done_at={2: old}, email_label_id=145,
            now=now, max_age_days=7,
        )
        assert len(actions) == 1
        assert actions[0].card_id == 2

    def test_archive_recurrence_guard_matches_prefix_case_insensitively(self):
        old = 1_700_000_000
        now = old + 30 * 86400
        card = make_card(id=3, title="Odd casing", stack_id=9,
                         labels=[_FakeLabel(145, "Email"), _FakeLabel(9, "repeat: mon")])
        assert plan_archive(
            done_cards=[card], done_at={3: old}, email_label_id=145,
            now=now, max_age_days=7,
        ) == []

    def test_archive_honours_a_custom_recurrence_prefix(self):
        old = 1_700_000_000
        now = old + 30 * 86400
        card = make_card(id=4, title="Custom prefix", stack_id=9,
                         labels=[_FakeLabel(145, "Email"), _FakeLabel(9, "Gjenta: 25")])
        assert plan_archive(
            done_cards=[card], done_at={4: old}, email_label_id=145,
            now=now, max_age_days=7, recur_label_prefix="Gjenta:",
        ) == []
        # ... and the default prefix does not recognise it.
        assert len(plan_archive(
            done_cards=[card], done_at={4: old}, email_label_id=145,
            now=now, max_age_days=7,
        )) == 1

    def test_empty_prefix_does_not_exempt_every_card_from_archiving(self):
        # An empty prefix would make startswith() true for every label and
        # silently switch the archive pass off board-wide.
        old = 1_700_000_000
        now = old + 30 * 86400
        card = make_card(id=5, title="Plain email", stack_id=9,
                         labels=[_FakeLabel(145, "Email")])
        assert len(plan_archive(
            done_cards=[card], done_at={5: old}, email_label_id=145,
            now=now, max_age_days=7, recur_label_prefix="",
        )) == 1


class TestValidateRecurLabelPrefix:
    def test_rejects_empty_string(self):
        assert validate_recur_label_prefix("") is not None

    def test_rejects_whitespace_only(self):
        assert validate_recur_label_prefix("   \t ") is not None

    def test_rejects_none(self):
        assert validate_recur_label_prefix(None) is not None

    def test_accepts_the_default(self):
        assert validate_recur_label_prefix(DEFAULT_LABEL_PREFIX) is None

    def test_accepts_a_custom_prefix(self):
        assert validate_recur_label_prefix("Gjenta:") is None

    def test_message_names_the_flag(self):
        assert "--recur-label-prefix" in validate_recur_label_prefix("")


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


class TestValidateArchiveDays:
    def test_rejects_at_the_limit(self):
        err = validate_archive_days(ARCHIVE_MAX_DAYS_LIMIT)
        assert err is not None
        assert str(ARCHIVE_MAX_DAYS_LIMIT) in err

    def test_rejects_above_the_limit(self):
        assert validate_archive_days(30) is not None

    def test_accepts_just_below_the_limit(self):
        assert validate_archive_days(ARCHIVE_MAX_DAYS_LIMIT - 1) is None

    def test_accepts_the_default(self):
        assert validate_archive_days(7) is None

    def test_accepts_zero_which_disables_the_pass(self):
        assert validate_archive_days(0) is None

    def test_accepts_negative_which_disables_the_pass(self):
        assert validate_archive_days(-1) is None

    def test_message_reports_the_offending_value(self):
        assert "99" in validate_archive_days(99)


class TestConfigArchiveDays:
    def test_config_defaults_to_seven_days(self):
        cfg = Config(
            nc_url="u", nc_username="u", nc_password="p", nc_board_id=4,
            todo_stack_name="Todo", doing_stack_name="Doing", done_stack_name="Done",
            email_label_name="Email", email_label_color="808080",
            imap_host="h", imap_port=993, imap_user="u", imap_password="p",
            imap_folder="_Virtual/Important",
        )
        assert cfg.archive_done_after_days == 7


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


# --- run() wiring: fakes for the archive-pass integration ---
#
# run() imports its IO dependencies *inside the function body*
# (`from imap_tools import MailBox`, `from ncdeck import DeckClient`), so
# tests must patch the attribute on the source module — patching
# `deck_sync.MailBox` etc. would have no effect, since that name is
# never bound at module scope.

ARCHIVE_EMAIL_LABEL_ID = 145


def _run_stack(stack_id, title, cards=()):
    return SimpleNamespace(id=stack_id, title=title, cards=list(cards))


class _FakeRunMailbox:
    """Stands in for the `with MailBox(...).login(...) as mailbox:` object.

    Combines fetch() (read starred messages) and flag() (unstar), and is its
    own context manager so `MailBox(...).login(...)` can just return it.
    """
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.flag_calls = []

    def fetch(self, mark_seen=False, bulk=True):
        return iter(self.messages)

    def flag(self, uids, flag_set, value):
        uid_list = [uids] if isinstance(uids, str) else list(uids)
        self.flag_calls.append((uid_list, flag_set, value))

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _fake_mailbox_class(fake_mailbox):
    """Returns a stand-in for the `imap_tools.MailBox` class/constructor."""
    class _FakeMailBox:
        def __init__(self, host, port=None):
            pass

        def login(self, user, password, initial_folder=None):
            return fake_mailbox

    return _FakeMailBox


class _FakeRunDeck:
    """DeckClient stand-in exercising every method run() can call."""
    def __init__(self, stacks, activity=None, activity_exc=None,
                 email_label=None):
        self._stacks = stacks
        self._activity = list(activity) if activity is not None else []
        self._activity_exc = activity_exc
        self._email_label = email_label or SimpleNamespace(
            id=ARCHIVE_EMAIL_LABEL_ID, title="Email", color="808080"
        )
        self.get_deck_activity_calls = []
        self.created = []
        self.moved = []
        self.assigned = []
        self.archived = []
        self.patched = []
        self._next_card_id = 5000

    def get_stacks(self, include_archived=False):
        return self._stacks

    def get_board_labels(self, board_id=None):
        return [self._email_label]

    def create_label(self, title="", color="808080", board_id=None):
        raise AssertionError("create_label should not be needed; label already exists")

    def get_deck_activity(self, limit=200, since=None):
        self.get_deck_activity_calls.append({"limit": limit, "since": since})
        if self._activity_exc is not None:
            raise self._activity_exc
        return self._activity

    def create_card(self, stack_id, title, description="", **kwargs):
        self._next_card_id += 1
        card = SimpleNamespace(
            id=self._next_card_id, title=title, stack_id=stack_id, description=description,
        )
        self.created.append({"stack_id": stack_id, "title": title})
        return card

    def move_card(self, card, target_stack_id, order=0, clear_done=False, board_id=None):
        self.moved.append({
            "card_id": card.id, "target": target_stack_id,
            "order": order, "clear_done": clear_done,
        })
        card.stack_id = target_stack_id
        return card

    def assign_label(self, stack_id, card_id, label_id, **kwargs):
        self.assigned.append({"stack_id": stack_id, "card_id": card_id, "label_id": label_id})

    def archive_card(self, stack_id, card_id, board_id=None):
        self.archived.append({"stack_id": stack_id, "card_id": card_id})

    def patch_card(self, card, board_id=None, **changes):
        for k, v in changes.items():
            setattr(card, k, v)
        self.patched.append((card.id, changes))
        return card


def _run_config(**overrides):
    fields = dict(
        nc_url="https://cloud.example.com", nc_username="u", nc_password="p",
        nc_board_id=4,
        todo_stack_name="Todo", doing_stack_name="Doing", done_stack_name="Done",
        email_label_name="Email", email_label_color="808080",
        imap_host="imap.example.com", imap_port=993, imap_user="u", imap_password="p",
        imap_folder="_Virtual/Important",
    )
    fields.update(overrides)
    return Config(**fields)


class TestRunArchiveWiring:
    def _patch_deps(self, monkeypatch, deck, mailbox):
        monkeypatch.setattr("ncdeck.DeckClient", lambda *a, **kw: deck)
        monkeypatch.setattr("imap_tools.MailBox", _fake_mailbox_class(mailbox))

    def test_archive_pass_skipped_when_disabled(self, monkeypatch):
        stacks = [
            _run_stack(7, "Todo"), _run_stack(8, "Doing"), _run_stack(9, "Done"),
        ]
        deck = _FakeRunDeck(stacks=stacks)
        mailbox = _FakeRunMailbox(messages=[])
        self._patch_deps(monkeypatch, deck, mailbox)

        config = _run_config(archive_done_after_days=0)
        rc = run(config)

        assert rc == 0
        assert deck.get_deck_activity_calls == []
        assert deck.archived == []

    def test_activity_fetch_failure_is_counted_not_silent(self, monkeypatch, caplog):
        import logging
        stacks = [
            _run_stack(7, "Todo"), _run_stack(8, "Doing"), _run_stack(9, "Done"),
        ]
        deck = _FakeRunDeck(stacks=stacks, activity_exc=RuntimeError("activity API down"))
        # An unstarred-in-managed-sense, newly-starred message with no matching
        # managed card: make_plan's Pass C turns this into a CreateCardAction,
        # proving the main sync still ran to completion.
        mailbox = _FakeRunMailbox(messages=[_imap_msg(uid="1", message_id="<new@x>")])
        self._patch_deps(monkeypatch, deck, mailbox)

        config = _run_config(archive_done_after_days=7)
        with caplog.at_level(logging.ERROR):
            rc = run(config)

        assert rc == 2
        assert deck.archived == []
        assert deck.created  # main IMAP<->Deck sync still completed
        assert any(
            rec.levelno == logging.ERROR and "activity" in rec.message.lower()
            for rec in caplog.records
        )

    def test_eligible_card_is_archived_through_run(self, monkeypatch):
        eligible = done_card(319, label_ids=(ARCHIVE_EMAIL_LABEL_ID,), stack_id=9)
        stacks = [
            _run_stack(7, "Todo"), _run_stack(8, "Doing"),
            _run_stack(9, "Done", cards=[eligible]),
        ]
        activity = [activity_entry(319, "2020-01-01T00:00:00+00:00", board="4", stack="9")]
        deck = _FakeRunDeck(stacks=stacks, activity=activity)
        mailbox = _FakeRunMailbox(messages=[])
        self._patch_deps(monkeypatch, deck, mailbox)

        config = _run_config(archive_done_after_days=7)
        rc = run(config)

        assert rc == 0
        assert deck.archived == [{"stack_id": 9, "card_id": 319}]

    def test_archive_pass_log_line_reports_activity_counts(self, monkeypatch, caplog):
        # Guards against a broken detection pipeline (renamed field, wrong
        # board id, disabled Activity app, ...) silently reading as a healthy
        # idle "0 of N eligible" forever. The two extra counts must appear in
        # the rendered log line so `len(done_at) == 0` while
        # `len(activity) > 0` is visible without instrumenting the code.
        import logging
        eligible = done_card(319, label_ids=(ARCHIVE_EMAIL_LABEL_ID,), stack_id=9)
        stacks = [
            _run_stack(7, "Todo"), _run_stack(8, "Doing"),
            _run_stack(9, "Done", cards=[eligible]),
        ]
        activity = [
            activity_entry(319, "2020-01-01T00:00:00+00:00", board="4", stack="9"),
            # Present in the activity feed but not a move-to-Done record for
            # this stack — must count toward `activity` but not `done_at`.
            activity_entry(1, "2020-01-01T00:00:00+00:00", board="4", stack="22"),
        ]
        deck = _FakeRunDeck(stacks=stacks, activity=activity)
        mailbox = _FakeRunMailbox(messages=[])
        self._patch_deps(monkeypatch, deck, mailbox)

        config = _run_config(archive_done_after_days=7)
        with caplog.at_level(logging.INFO):
            rc = run(config)

        assert rc == 0
        archive_messages = [
            rec.getMessage() for rec in caplog.records if "Archive pass" in rec.getMessage()
        ]
        assert archive_messages == [
            "Archive pass: 1 of 1 card(s) in Done eligible "
            "(2 activity entries, 1 with a move-to-Done record)"
        ]

    def test_archive_actions_do_not_displace_make_plan_actions(self, monkeypatch):
        eligible = done_card(319, label_ids=(ARCHIVE_EMAIL_LABEL_ID,), stack_id=9)
        stacks = [
            _run_stack(7, "Todo"), _run_stack(8, "Doing"),
            _run_stack(9, "Done", cards=[eligible]),
        ]
        activity = [activity_entry(319, "2020-01-01T00:00:00+00:00", board="4", stack="9")]
        deck = _FakeRunDeck(stacks=stacks, activity=activity)
        mailbox = _FakeRunMailbox(messages=[_imap_msg(uid="1", message_id="<new@x>")])
        self._patch_deps(monkeypatch, deck, mailbox)

        config = _run_config(archive_done_after_days=7)
        rc = run(config)

        assert rc == 0
        assert deck.created  # make_plan's CreateCardAction reached execute_plan
        assert deck.archived == [{"stack_id": 9, "card_id": 319}]


# --- done-tick <-> Done stack sync pass ---

def make_card(id, title="", stack_id=None, done=None, archived=False,
             deleted_at=0, labels=()):
    """Minimal stand-in for ncdeck.Card covering the fields plan_done_sync
    and execute_plan's tick/move-by-tick branches need."""
    return SimpleNamespace(
        id=id, title=title, stack_id=stack_id, done=done,
        archived=archived, deleted_at=deleted_at, labels=list(labels),
    )


DONE_SYNC_NOW_ISO = "2026-08-04T15:30:00+00:00"


class TestPlanDoneSync:
    def test_ticks_an_unticked_card_in_done(self):
        card = make_card(id=110, title="Vanne planter", stack_id=9, done=None)
        actions = plan_done_sync([card], [], done_stack_id=9, now_iso=DONE_SYNC_NOW_ISO)
        assert actions == [
            TickDoneAction(card=card, card_title="Vanne planter", done_at=DONE_SYNC_NOW_ISO)
        ]

    def test_leaves_an_already_ticked_card_in_done_alone(self):
        card = make_card(id=323, title="Timeliste", stack_id=9,
                         done="2026-08-03T17:08:04+00:00")
        assert plan_done_sync([card], [], done_stack_id=9, now_iso=DONE_SYNC_NOW_ISO) == []

    def test_moves_a_ticked_card_out_of_todo_into_done(self):
        card = make_card(id=1, title="Chore", stack_id=22,
                         done="2026-08-04T09:00:00+00:00")
        actions = plan_done_sync([], [card], done_stack_id=9, now_iso=DONE_SYNC_NOW_ISO)
        assert actions == [
            MoveToDoneByTickAction(card=card, card_title="Chore", target_stack_id=9)
        ]

    def test_leaves_an_unticked_card_outside_done_alone(self):
        card = make_card(id=1, title="Chore", stack_id=22, done=None)
        assert plan_done_sync([], [card], done_stack_id=9, now_iso=DONE_SYNC_NOW_ISO) == []

    def test_done_sync_skips_archived_cards_in_both_directions(self):
        in_done = make_card(id=1, title="A", stack_id=9, done=None, archived=True)
        outside = make_card(id=2, title="B", stack_id=22, done=DONE_SYNC_NOW_ISO, archived=True)
        assert plan_done_sync([in_done], [outside], done_stack_id=9,
                              now_iso=DONE_SYNC_NOW_ISO) == []

    def test_done_sync_skips_trashed_cards_in_both_directions(self):
        in_done = make_card(id=1, title="A", stack_id=9, done=None, deleted_at=17543)
        outside = make_card(id=2, title="B", stack_id=22, done=DONE_SYNC_NOW_ISO, deleted_at=17543)
        assert plan_done_sync([in_done], [outside], done_stack_id=9,
                              now_iso=DONE_SYNC_NOW_ISO) == []

    def test_done_sync_converges_after_one_pass(self):
        """Applying the plan and re-planning must yield nothing to do."""
        unticked_in_done = make_card(id=1, title="A", stack_id=9, done=None)
        ticked_in_todo = make_card(id=2, title="B", stack_id=22, done=DONE_SYNC_NOW_ISO)

        first = plan_done_sync([unticked_in_done], [ticked_in_todo],
                               done_stack_id=9, now_iso=DONE_SYNC_NOW_ISO)
        assert len(first) == 2

        # Apply: the Done card gains a tick; the Todo card moves into Done.
        unticked_in_done.done = DONE_SYNC_NOW_ISO
        ticked_in_todo.stack_id = 9

        second = plan_done_sync([unticked_in_done, ticked_in_todo], [],
                                done_stack_id=9, now_iso=DONE_SYNC_NOW_ISO)
        assert second == []


class TestPlanDoneSyncSkipCardIds:
    """A card another pass already moves into Done must not be moved twice."""

    def test_skips_an_open_card_already_targeted_by_another_move(self):
        card = make_card(id=1, title="Chore", stack_id=22, done=DONE_SYNC_NOW_ISO)
        assert plan_done_sync([], [card], done_stack_id=9,
                              now_iso=DONE_SYNC_NOW_ISO, skip_card_ids={1}) == []

    def test_only_the_named_card_is_skipped(self):
        skipped = make_card(id=1, title="A", stack_id=22, done=DONE_SYNC_NOW_ISO)
        other = make_card(id=2, title="B", stack_id=22, done=DONE_SYNC_NOW_ISO)
        actions = plan_done_sync([], [skipped, other], done_stack_id=9,
                                 now_iso=DONE_SYNC_NOW_ISO, skip_card_ids={1})
        assert actions == [
            MoveToDoneByTickAction(card=other, card_title="B", target_stack_id=9)
        ]

    def test_default_skips_nothing(self):
        card = make_card(id=1, title="Chore", stack_id=22, done=DONE_SYNC_NOW_ISO)
        assert len(plan_done_sync([], [card], done_stack_id=9,
                                  now_iso=DONE_SYNC_NOW_ISO)) == 1


class TestExecuteDoneSyncActions:
    def test_execute_tick_calls_patch_card_with_the_timestamp(self):
        card = make_card(id=110, title="Vanne planter", stack_id=9, done=None)
        deck = _FakeDeck()
        summary = execute_plan(
            [TickDoneAction(card=card, card_title="Vanne planter", done_at=DONE_SYNC_NOW_ISO)],
            mailbox=_FakeMailbox(), deck=deck,
        )
        assert deck.patched == [(110, {"done": DONE_SYNC_NOW_ISO})]
        assert summary.ticked == 1

    def test_execute_move_by_tick_calls_move_card_at_order_zero(self):
        card = make_card(id=1, title="Chore", stack_id=22, done=DONE_SYNC_NOW_ISO)
        deck = _FakeDeck()
        summary = execute_plan(
            [MoveToDoneByTickAction(card=card, card_title="Chore", target_stack_id=9)],
            mailbox=_FakeMailbox(), deck=deck,
        )
        assert deck.moved == [
            {"card_id": 1, "target": 9, "order": 0, "clear_done": False}
        ]
        assert summary.moved_by_tick == 1

    def test_move_by_tick_never_clears_the_tick_that_caused_the_move(self):
        # clear_done must stay False here: if it ever flipped to True, the
        # move would erase the tick that triggered it, the card would land in
        # Done unticked, plan_done_sync's Done-side clause would re-tick it
        # next run, and the tick/move pair would repeat forever on the
        # 5-minute timer. (The recurrence feature intentionally passes
        # clear_done=True elsewhere — deck_recur.py — and is covered there;
        # this sync must never do that.)
        card = make_card(id=1, title="Chore", stack_id=22, done=DONE_SYNC_NOW_ISO)
        deck = _FakeDeck()
        execute_plan(
            [MoveToDoneByTickAction(card=card, card_title="Chore", target_stack_id=9)],
            mailbox=_FakeMailbox(), deck=deck,
        )
        assert deck.moved[0]["clear_done"] is False

    def test_execute_done_sync_actions_do_nothing_under_dry_run(self):
        card = make_card(id=110, title="Vanne planter", stack_id=9, done=None)
        deck = _FakeDeck()
        summary = execute_plan(
            [TickDoneAction(card=card, card_title="Vanne planter", done_at=DONE_SYNC_NOW_ISO)],
            mailbox=_FakeMailbox(), deck=deck, dry_run=True,
        )
        assert deck.patched == []
        assert summary.ticked == 1


# --- run() wiring: the done-tick pass ---


def _managed_run_card(card_id, message_id, stack_id, title="Managed card",
                      done=None, label_ids=(ARCHIVE_EMAIL_LABEL_ID,),
                      label_titles=()):
    """A card carrying the imap-sync marker, shaped for the run() fakes."""
    labels = [SimpleNamespace(id=lid, title="Email") for lid in label_ids]
    labels += [SimpleNamespace(id=800 + i, title=t)
               for i, t in enumerate(label_titles)]
    return SimpleNamespace(
        id=card_id, title=title, stack_id=stack_id,
        description=f"{build_marker(message_id)}\n",
        done=done, archived=False, deleted_at=0, labels=labels,
    )


def _capture_plan(monkeypatch):
    """Record the plan run() hands to execute_plan, then execute it for real."""
    import deck_sync

    captured = {}
    real_execute = deck_sync.execute_plan

    def spy(plan, mailbox, deck, dry_run=False):
        captured["plan"] = list(plan)
        return real_execute(plan=plan, mailbox=mailbox, deck=deck, dry_run=dry_run)

    monkeypatch.setattr("deck_sync.execute_plan", spy)
    return captured


class TestRunDoneSyncWiring:
    """The done-tick pass writes to a live board every five minutes.

    Without these, deleting `+ done_sync_actions` from run() leaves the whole
    suite green while the pass silently stops running.
    """

    def _patch_deps(self, monkeypatch, deck, mailbox):
        monkeypatch.setattr("ncdeck.DeckClient", lambda *a, **kw: deck)
        monkeypatch.setattr("imap_tools.MailBox", _fake_mailbox_class(mailbox))

    def test_done_sync_runs_by_default_and_its_actions_reach_the_plan(self, monkeypatch):
        unticked = make_card(id=110, title="Vanne planter", stack_id=9, done=None)
        stacks = [
            _run_stack(7, "Todo"), _run_stack(8, "Doing"),
            _run_stack(9, "Done", cards=[unticked]),
        ]
        deck = _FakeRunDeck(stacks=stacks)
        mailbox = _FakeRunMailbox(messages=[])
        self._patch_deps(monkeypatch, deck, mailbox)
        captured = _capture_plan(monkeypatch)

        rc = run(_run_config(archive_done_after_days=0))

        assert rc == 0
        assert [type(a).__name__ for a in captured["plan"]] == ["TickDoneAction"]
        assert [card_id for card_id, _ in deck.patched] == [110]

    def test_done_sync_false_suppresses_the_pass_entirely(self, monkeypatch):
        unticked = make_card(id=110, title="Vanne planter", stack_id=9, done=None)
        ticked_in_todo = make_card(id=111, title="Chore", stack_id=7,
                                   done=DONE_SYNC_NOW_ISO)
        stacks = [
            _run_stack(7, "Todo", cards=[ticked_in_todo]), _run_stack(8, "Doing"),
            _run_stack(9, "Done", cards=[unticked]),
        ]
        deck = _FakeRunDeck(stacks=stacks)
        mailbox = _FakeRunMailbox(messages=[])
        self._patch_deps(monkeypatch, deck, mailbox)
        captured = _capture_plan(monkeypatch)

        rc = run(_run_config(archive_done_after_days=0, done_sync=False))

        assert rc == 0
        assert captured["plan"] == []
        assert deck.patched == []
        assert deck.moved == []

    def test_open_cards_are_drawn_from_both_todo_and_doing(self, monkeypatch):
        # Assembling open_cards from Todo alone would leave a card hand-ticked
        # in Doing sitting there forever.
        in_todo = make_card(id=1, title="From Todo", stack_id=7, done=DONE_SYNC_NOW_ISO)
        in_doing = make_card(id=2, title="From Doing", stack_id=8, done=DONE_SYNC_NOW_ISO)
        stacks = [
            _run_stack(7, "Todo", cards=[in_todo]),
            _run_stack(8, "Doing", cards=[in_doing]),
            _run_stack(9, "Done"),
        ]
        deck = _FakeRunDeck(stacks=stacks)
        mailbox = _FakeRunMailbox(messages=[])
        self._patch_deps(monkeypatch, deck, mailbox)

        rc = run(_run_config(archive_done_after_days=0))

        assert rc == 0
        assert sorted(m["card_id"] for m in deck.moved) == [1, 2]
        assert {m["target"] for m in deck.moved} == {9}

    def test_plan_order_is_imap_then_done_sync_then_archive(self, monkeypatch):
        # Positional, not incidental: the IMAP pass must be able to unstar a
        # message before anything else touches its card, and the archive pass
        # runs last so it sees the day's moves. Nothing else pins this down.
        managed_in_todo = _managed_run_card(500, "<m@x>", stack_id=7)
        ticked_in_doing = make_card(id=600, title="Hand-ticked", stack_id=8,
                                    done=DONE_SYNC_NOW_ISO)
        stale_in_done = done_card(319, label_ids=(ARCHIVE_EMAIL_LABEL_ID,), stack_id=9)
        stacks = [
            _run_stack(7, "Todo", cards=[managed_in_todo]),
            _run_stack(8, "Doing", cards=[ticked_in_doing]),
            _run_stack(9, "Done", cards=[stale_in_done]),
        ]
        activity = [activity_entry(319, "2020-01-01T00:00:00+00:00", board="4", stack="9")]
        deck = _FakeRunDeck(stacks=stacks, activity=activity)
        mailbox = _FakeRunMailbox(messages=[])
        self._patch_deps(monkeypatch, deck, mailbox)
        captured = _capture_plan(monkeypatch)

        rc = run(_run_config(archive_done_after_days=7))

        assert rc == 0
        plan = captured["plan"]
        assert [type(a).__name__ for a in plan] == [
            "MoveToDoneAction",         # IMAP pass (Pass B)
            "TickDoneAction",           # done-sync, Done side
            "MoveToDoneByTickAction",   # done-sync, open side
            "ArchiveAction",            # archive pass
        ]
        assert plan[0].card.id == 500
        assert plan[1].card.id == 319
        assert plan[2].card.id == 600
        assert plan[3].card_id == 319

    def test_unstarred_and_hand_ticked_card_is_moved_once(self, monkeypatch):
        # Pass B and the done-sync open-side clause both want to move this
        # card into Done. Two PUTs, two activity entries, one logical move.
        card = _managed_run_card(500, "<m@x>", stack_id=7, done=DONE_SYNC_NOW_ISO)
        stacks = [
            _run_stack(7, "Todo", cards=[card]),
            _run_stack(8, "Doing"), _run_stack(9, "Done"),
        ]
        deck = _FakeRunDeck(stacks=stacks)
        mailbox = _FakeRunMailbox(messages=[])
        self._patch_deps(monkeypatch, deck, mailbox)
        captured = _capture_plan(monkeypatch)

        rc = run(_run_config(archive_done_after_days=0))

        assert rc == 0
        assert [type(a).__name__ for a in captured["plan"]] == ["MoveToDoneAction"]
        assert deck.moved == [
            {"card_id": 500, "target": 9, "order": 0, "clear_done": False}
        ]

    def test_recurring_card_in_todo_survives_a_full_run(self, monkeypatch):
        # End-to-end guard for the recurrence regression: the card the daily
        # recurrence job just put back in Todo must still be there afterwards.
        card = _managed_run_card(501, "<m@x>", stack_id=7,
                                 title="Vask bilen", label_titles=("Repeat: 25",))
        stacks = [
            _run_stack(7, "Todo", cards=[card]),
            _run_stack(8, "Doing"), _run_stack(9, "Done"),
        ]
        deck = _FakeRunDeck(stacks=stacks)
        mailbox = _FakeRunMailbox(messages=[])
        self._patch_deps(monkeypatch, deck, mailbox)
        captured = _capture_plan(monkeypatch)

        rc = run(_run_config(archive_done_after_days=0))

        assert rc == 0
        assert captured["plan"] == []
        assert deck.moved == []
        assert card.stack_id == 7

    def test_configured_recur_prefix_reaches_both_passes(self, monkeypatch):
        # One source of truth: a non-default prefix must be honoured by Pass B
        # and by the archive pass, or recurring cards get archived after a week
        # and never come back.
        in_todo = _managed_run_card(502, "<a@x>", stack_id=7,
                                    label_titles=("Gjenta: 25",))
        in_done = make_card(id=503, title="Recurring", stack_id=9,
                            done=DONE_SYNC_NOW_ISO,
                            labels=[_FakeLabel(ARCHIVE_EMAIL_LABEL_ID, "Email"),
                                    _FakeLabel(9, "Gjenta: 25")])
        stacks = [
            _run_stack(7, "Todo", cards=[in_todo]), _run_stack(8, "Doing"),
            _run_stack(9, "Done", cards=[in_done]),
        ]
        activity = [activity_entry(503, "2020-01-01T00:00:00+00:00", board="4", stack="9")]
        deck = _FakeRunDeck(stacks=stacks, activity=activity)
        mailbox = _FakeRunMailbox(messages=[])
        self._patch_deps(monkeypatch, deck, mailbox)
        captured = _capture_plan(monkeypatch)

        rc = run(_run_config(archive_done_after_days=7,
                             recur_label_prefix="Gjenta:"))

        assert rc == 0
        assert captured["plan"] == []
        assert deck.moved == []
        assert deck.archived == []


# --- CLI entrypoint: argument validation ---
#
# nextcloud-deck-sync.py has a dash in its name and imports the private `olen`
# logging package at module scope, so it is loaded by path and skipped where
# that package is absent. The logger/alerting side effects are stubbed out —
# these tests must not write to the real ~/bin/logs or reach Discord.


def _load_sync_cli(monkeypatch):
    import importlib.util
    import pytest

    pytest.importorskip("olen")
    path = Path(__file__).resolve().parent.parent / "nextcloud-deck-sync.py"
    spec = importlib.util.spec_from_file_location("nextcloud_deck_sync_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class _StubAppLog:
        DEBUG = 10
        INFO = 20

        def __init__(self):
            self.silent = False

        def set_level(self, level):
            pass

        def start_logger(self, config):
            pass

    monkeypatch.setattr(mod, "APP_CONFIG", SimpleNamespace(set=lambda *a, **kw: None))
    monkeypatch.setattr(mod, "get_logger", _StubAppLog)
    monkeypatch.setattr(mod, "RemoteLogger", lambda config: SimpleNamespace())
    monkeypatch.setattr(mod.logging, "basicConfig", lambda **kw: None)
    return mod


CLI_REQUIRED_ARGS = [
    "--url", "https://cloud.example.com", "-u", "u", "-p", "p", "-b", "4",
    "--imap-user", "u", "--imap-password", "p",
]


class TestSyncCliRecurLabelPrefix:
    def _spy_run(self, monkeypatch, mod):
        calls = []

        def spy(config):
            calls.append(config)
            return 0

        monkeypatch.setattr(mod, "run", spy)
        return calls

    def test_empty_prefix_is_rejected_with_exit_code_2(self, monkeypatch, capsys):
        mod = _load_sync_cli(monkeypatch)
        calls = self._spy_run(monkeypatch, mod)
        monkeypatch.setattr(
            "sys.argv",
            ["nextcloud-deck-sync.py", *CLI_REQUIRED_ARGS, "--recur-label-prefix", ""],
        )

        assert mod.main() == 2
        assert calls == []  # never reached the board
        assert "--recur-label-prefix" in capsys.readouterr().err

    def test_whitespace_only_prefix_is_rejected(self, monkeypatch):
        mod = _load_sync_cli(monkeypatch)
        calls = self._spy_run(monkeypatch, mod)
        monkeypatch.setattr(
            "sys.argv",
            ["nextcloud-deck-sync.py", *CLI_REQUIRED_ARGS, "--recur-label-prefix", "  "],
        )

        assert mod.main() == 2
        assert calls == []

    def test_prefix_flag_reaches_the_config(self, monkeypatch):
        mod = _load_sync_cli(monkeypatch)
        calls = self._spy_run(monkeypatch, mod)
        monkeypatch.setattr(
            "sys.argv",
            ["nextcloud-deck-sync.py", *CLI_REQUIRED_ARGS,
             "--recur-label-prefix", "Gjenta:"],
        )

        assert mod.main() == 0
        assert calls[0].recur_label_prefix == "Gjenta:"

    def test_prefix_defaults_to_the_shared_constant(self, monkeypatch):
        mod = _load_sync_cli(monkeypatch)
        calls = self._spy_run(monkeypatch, mod)
        monkeypatch.delenv("RECUR_LABEL_PREFIX", raising=False)
        monkeypatch.setattr("sys.argv", ["nextcloud-deck-sync.py", *CLI_REQUIRED_ARGS])

        assert mod.main() == 0
        assert calls[0].recur_label_prefix == DEFAULT_LABEL_PREFIX

    def test_prefix_reads_the_env_var(self, monkeypatch):
        # Same env var nextcloud-deck-recur.py reads, so exporting it once in a
        # wrapper configures both halves.
        mod = _load_sync_cli(monkeypatch)
        calls = self._spy_run(monkeypatch, mod)
        monkeypatch.setenv("RECUR_LABEL_PREFIX", "Gjenta:")
        monkeypatch.setattr("sys.argv", ["nextcloud-deck-sync.py", *CLI_REQUIRED_ARGS])

        assert mod.main() == 0
        assert calls[0].recur_label_prefix == "Gjenta:"
