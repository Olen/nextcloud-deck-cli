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
