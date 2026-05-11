from types import SimpleNamespace

from imap_deck_sync import (
    parse_marker,
    build_marker,
    format_card_title,
    StarredMessage,
    ManagedCard,
    UnstarAction,
    MoveToDoneAction,
    CreateCardAction,
    AssignLabelAction,
    StackIds,
    make_plan,
    fetch_managed,
    fetch_starred,
    execute_plan,
    ExecutionSummary,
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
