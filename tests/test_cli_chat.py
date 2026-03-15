from __future__ import annotations

import json

import pytest

import teams_cli.cli as cli_mod
import teams_cli.commands.auth as cmd_auth
import teams_cli.commands.chat as cmd_chat


def test_whoami_json_includes_region_and_user_id(runner, mocker):
    class FakeClient:
        _region = "emea"
        _user_id = "user-123"

        def get_me(self) -> dict[str, str]:
            return {"displayName": "Test User", "mail": "test@example.com"}

    mocker.patch.object(cmd_auth, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["whoami", "--json"])

    assert result.exit_code == 0
    envelope = json.loads(result.output)
    assert envelope["ok"] is True
    payload = envelope["data"]
    assert payload["displayName"] == "Test User"
    assert payload["region"] == "emea"
    assert payload["user_id"] == "user-123"


def test_chats_nonempty_calls_formatter(runner, mocker, make_chat):
    chat = make_chat()

    class FakeClient:
        def get_chats(self, top: int, unread_only: bool = False):
            assert top == 25
            assert unread_only is False
            return [chat]

    print_chats = mocker.patch.object(cmd_chat, "print_chats")
    mocker.patch.object(cmd_chat, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["chats"])

    assert result.exit_code == 0
    print_chats.assert_called_once_with([chat])


def test_chats_empty_state_prints_message(runner, console_capture, mocker):
    class FakeClient:
        def get_chats(self, top: int, unread_only: bool = False):
            return []

    mocker.patch.object(cmd_chat, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["chats"])

    assert result.exit_code == 0
    assert "No chats found." in console_capture.getvalue()


def test_chat_command_passes_date_filters(runner, mocker):
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int, str | None, str | None]] = []

        def get_chat_messages(
            self,
            chat_num: str,
            top: int = 25,
            after: str | None = None,
            before: str | None = None,
        ) -> list:
            self.calls.append((chat_num, top, after, before))
            return []

        def _resolve_chat_id(self, chat_num: str) -> str:
            return f"conv-{chat_num}"

    fake_client = FakeClient()
    mocker.patch.object(cmd_chat, "_get_client", return_value=fake_client)

    result = runner.invoke(
        cli_mod.cli,
        ["chat", "3", "--after", "2026-03-10", "--before", "2026-03-11", "--json"],
    )

    assert result.exit_code == 0
    assert fake_client.calls == [("3", 25, "2026-03-10", "2026-03-11")]
    assert json.loads(result.output)["data"] == []


def test_chat_empty_state_prints_message(runner, console_capture, mocker):
    class FakeClient:
        def get_chat_messages(self, chat_num: str, top: int = 25, after=None, before=None):
            return []

        def _resolve_chat_id(self, chat_num: str) -> str:
            return f"conv-{chat_num}"

    mocker.patch.object(cmd_chat, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["chat", "2"])

    assert result.exit_code == 0
    assert "No messages in chat #2." in console_capture.getvalue()


def test_read_raw_prints_html(runner, console_capture, mocker, make_message):
    message = make_message(content="<p>Hello raw</p>")

    class FakeClient:
        def get_message_detail(self, msg_num: str):
            assert msg_num == "7"
            return message

    mocker.patch.object(cmd_chat, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["read", "7", "--raw"])

    assert result.exit_code == 0
    assert "<p>Hello raw</p>" in console_capture.getvalue()


def test_read_json_serializes_message(runner, mocker, make_message):
    message = make_message(msg_id="m9")

    class FakeClient:
        def get_message_detail(self, msg_num: str):
            return message

    mocker.patch.object(cmd_chat, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["read", "9", "--json"])

    assert result.exit_code == 0
    envelope = json.loads(result.output)
    assert envelope["ok"] is True
    payload = envelope["data"]
    assert payload["id"] == "m9"
    assert payload["content"] == "<p>content</p>"


def test_unread_uses_unread_filter(runner, console_capture, mocker, make_chat):
    chat = make_chat(unread_count=2)

    class FakeClient:
        def get_chats(self, top: int, unread_only: bool = False):
            assert top == 25
            assert unread_only is True
            return [chat]

    print_chats = mocker.patch.object(cmd_chat, "print_chats")
    mocker.patch.object(cmd_chat, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["unread"])

    assert result.exit_code == 0
    assert "1 unread chat(s)" in console_capture.getvalue()
    print_chats.assert_called_once_with([chat])
