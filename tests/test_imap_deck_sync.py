from types import SimpleNamespace
from unittest.mock import MagicMock

from imap_deck_sync import (
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
    StackIds,
    make_plan,
    latest_done_at,
    plan_archive,
    fetch_managed,
    fetch_starred,
    fetch_deck_activity,
    execute_plan,
    ExecutionSummary,
    Config,
    validate_archive_days,
    run,
)


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


def _managed(msgid, stack_id, card_id=42, label_ids=()):
    # Card is opaque to the planner — we only need an identity for downstream actions.
    # We pass a SimpleNamespace so production code can attach a real Card here.
    from types import SimpleNamespace
    card = SimpleNamespace(id=card_id, title="(irrelevant)")
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


class _FakeLabel:
    """Minimal stand-in for olen_deck.Label."""
    def __init__(self, id):
        self.id = id


class _FakeCard:
    """Minimal stand-in for olen_deck.Card."""
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

    def move_card(self, card, target_stack_id):
        if card.id in self.fail_moves_for_card_ids:
            raise RuntimeError(f"simulated move failure for card {card.id}")
        self.moved.append({"card_id": card.id, "target": target_stack_id})
        card.stack_id = target_stack_id
        return card

    def assign_label(self, stack_id, card_id, label_id, **kwargs):
        if label_id in self.fail_assigns_for_label_ids:
            raise RuntimeError(f"simulated label failure for label {label_id}")
        self.assigned.append({"stack_id": stack_id, "card_id": card_id, "label_id": label_id})


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

    def create_label(self, board_id=None, title="", color="808080"):
        self.create_calls.append({"board_id": board_id, "title": title, "color": color})
        return self.create_returns


class TestFindOrCreateEmailLabel:
    def test_returns_existing_label_matched_by_title(self):
        from imap_deck_sync import _find_or_create_email_label
        existing = SimpleNamespace(id=7, title="Email", color="123456")
        other = SimpleNamespace(id=8, title="Other", color="123456")
        deck = _FakeDeckLabels(existing_labels=[other, existing])
        result = _find_or_create_email_label(deck, board_id=4, name="Email", color="808080")
        assert result is existing
        assert deck.create_calls == []

    def test_match_is_by_title_only_not_color(self):
        # A same-color label with a different title must not match.
        from imap_deck_sync import _find_or_create_email_label
        existing_other = SimpleNamespace(id=8, title="Spam", color="808080")
        created = SimpleNamespace(id=99, title="Email", color="808080")
        deck = _FakeDeckLabels(existing_labels=[existing_other], create_returns=created)
        result = _find_or_create_email_label(deck, board_id=4, name="Email", color="808080")
        assert result is created
        assert deck.create_calls == [{"board_id": 4, "title": "Email", "color": "808080"}]

    def test_creates_label_when_absent(self):
        from imap_deck_sync import _find_or_create_email_label
        created = SimpleNamespace(id=42, title="Email", color="abcdef")
        deck = _FakeDeckLabels(existing_labels=[], create_returns=created)
        result = _find_or_create_email_label(deck, board_id=4, name="Email", color="abcdef")
        assert result is created
        assert deck.create_calls == [{"board_id": 4, "title": "Email", "color": "abcdef"}]

    def test_raises_runtime_error_if_create_returns_none(self):
        import pytest
        from imap_deck_sync import _find_or_create_email_label
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
# (`from imap_tools import MailBox`, `from olen_deck import DeckClient`), so
# tests must patch the attribute on the source module — patching
# `imap_deck_sync.MailBox` etc. would have no effect, since that name is
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
        self._next_card_id = 5000

    def fetch_stacks(self, include_archived=False):
        return self._stacks

    def get_board_labels(self, board_id=None):
        return [self._email_label]

    def create_label(self, board_id=None, title="", color="808080"):
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

    def move_card(self, card, target_stack_id):
        self.moved.append({"card_id": card.id, "target": target_stack_id})
        card.stack_id = target_stack_id
        return card

    def assign_label(self, stack_id, card_id, label_id, **kwargs):
        self.assigned.append({"stack_id": stack_id, "card_id": card_id, "label_id": label_id})

    def archive_card(self, stack_id, card_id, board_id=None):
        self.archived.append({"stack_id": stack_id, "card_id": card_id})


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
        monkeypatch.setattr("olen_deck.DeckClient", lambda *a, **kw: deck)
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
