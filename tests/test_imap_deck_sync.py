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
