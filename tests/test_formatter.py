from __future__ import annotations

from datetime import datetime, timezone

import pytest

import teams_cli.formatter as formatter


def test_print_chats_embeds_display_number_in_chat_column(console_capture, make_chat):
    chat = make_chat("chat-a", "John Smith")
    chat.display_num = 12

    formatter.print_chats([chat])

    rendered = console_capture.getvalue()
    assert "#12 John Smith" in rendered


def test_print_messages_shows_search_chat_title_and_reactions(
    console_capture,
    make_message,
    make_reaction,
):
    message = make_message(
        msg_id="m1",
        sender="Alice",
        content="<p>deploy succeeded</p>",
        text_content="deploy succeeded",
        reactions=[make_reaction("like", "Alice"), make_reaction("like", "Bob")],
    )
    message.display_num = 7
    message.chat_title = "#3 Release Chat"

    formatter.print_messages([message], chat_title="Search: deploy")

    rendered = console_capture.getvalue()
    assert "Search: deploy" in rendered
    assert "[#3 Release Chat]" in rendered
    assert "#7" in rendered
    assert "like(2)" in rendered


def test_print_message_detail_lists_reactions_and_attachments(
    console_capture,
    make_message,
    make_reaction,
    make_attachment,
):
    message = make_message(
        reactions=[make_reaction("heart", "Alice"), make_reaction("heart", "Bob")],
        attachments=[make_attachment(name="report.pdf", content_type="application/pdf")],
    )
    message.display_num = 3

    formatter.print_message_detail(message)

    rendered = console_capture.getvalue()
    assert "Message #3" in rendered
    assert "heart (2): Alice, Bob" in rendered
    assert "report.pdf (application/pdf)" in rendered


def test_format_date_handles_today_and_yesterday(monkeypatch: pytest.MonkeyPatch):
    fixed_now = datetime(2026, 3, 11, 15, 0, tzinfo=timezone.utc)

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr(formatter, "datetime", FakeDateTime)

    assert formatter._format_date(datetime(2026, 3, 11, 11, 0, tzinfo=timezone.utc)) == "14:00"
    assert formatter._format_date(datetime(2026, 3, 10, 11, 0, tzinfo=timezone.utc)) == "Yday"


def test_truncate_and_strip_html_helpers():
    assert formatter._truncate("abcdef", 4) == "abc…"
    assert formatter._strip_html_for_display("<p>Hello <strong>world</strong></p>") == "Hello world"
