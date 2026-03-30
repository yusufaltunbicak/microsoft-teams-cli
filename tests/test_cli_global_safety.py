from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import json

import pytest

import teams_cli.cli as cli_mod
import teams_cli.commands._common as commands_common
import teams_cli.commands.group_chat as cmd_group_chat
import teams_cli.commands.mark_read as cmd_mark_read
import teams_cli.commands.message_manage as cmd_message_manage
import teams_cli.commands.presence as cmd_presence
import teams_cli.commands.reactions as cmd_reactions
import teams_cli.commands.schedule as cmd_schedule
import teams_cli.commands.send as cmd_send
import teams_cli.scheduler as scheduler


def _assert_get_client_not_called(mocker, module):
    mocker.patch.object(module, "_get_client", side_effect=AssertionError("client should not be used during dry-run"))


def test_root_force_bypasses_confirmation_for_chat_send(runner, mocker):
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def send_message_to_chat(self, chat_num: str, message: str):
            self.calls.append((chat_num, message))

    fake_client = FakeClient()
    confirm = mocker.patch.object(cmd_send.click, "confirm", side_effect=AssertionError("confirm should be skipped"))
    mocker.patch.object(cmd_send, "_get_client", return_value=fake_client)

    result = runner.invoke(cli_mod.cli, ["--force", "chat-send", "5", "hello"])

    assert result.exit_code == 0
    assert fake_client.calls == [("5", "hello")]
    confirm.assert_not_called()


def test_root_no_input_refuses_confirmation_prompt(runner, mocker):
    get_client = mocker.patch.object(cmd_send, "_get_client", side_effect=AssertionError("client should not be used"))

    result = runner.invoke(cli_mod.cli, ["--no-input", "chat-send", "5", "hello"])

    assert result.exit_code == 2
    assert "Refusing to send a message to a chat without confirmation" in result.output
    get_client.assert_not_called()


def test_send_no_input_refuses_interactive_user_selection(runner, mocker, make_user):
    users = [
        make_user(user_id="user-a", display_name="Alice Smith", email="alice.smith@example.com"),
        make_user(user_id="user-b", display_name="Alice Brown", email="alice.brown@example.com"),
    ]

    class FakeClient:
        def search_users(self, query: str, top: int = 10):
            return users

    prompt = mocker.patch.object(cmd_send.click, "prompt", side_effect=AssertionError("prompt should be refused"))
    mocker.patch.object(cmd_send, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["--no-input", "send", "alice", "hello"])

    assert result.exit_code == 2
    assert "Refusing to select a user interactively" in result.output
    prompt.assert_not_called()


def test_chat_send_dry_run_json_payload(runner, mocker, monkeypatch: pytest.MonkeyPatch):
    _assert_get_client_not_called(mocker, cmd_send)
    monkeypatch.setattr(commands_common, "is_piped", lambda: True)

    result = runner.invoke(cli_mod.cli, ["--dry-run", "chat-send", "5", "hello"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["data"] == {
        "dry_run": True,
        "op": "send message to chat",
        "request": {"chat": "#5", "message": "hello"},
    }


@pytest.mark.parametrize(
    ("args", "prepare"),
    [
        (["chat-send", "5", "hello"], lambda mocker, tmp_path: _assert_get_client_not_called(mocker, cmd_send)),
        (["reply", "12", "hello"], lambda mocker, tmp_path: _assert_get_client_not_called(mocker, cmd_send)),
        (["send", "alice@example.com", "hello"], lambda mocker, tmp_path: _assert_get_client_not_called(mocker, cmd_send)),
        (
            ["send-file", "5", "{tmp}/report.txt", "--message", "hello"],
            lambda mocker, tmp_path: (
                _assert_get_client_not_called(mocker, cmd_send),
                (tmp_path / "report.txt").write_text("content", encoding="utf-8"),
            ),
        ),
        (["edit", "12", "updated"], lambda mocker, tmp_path: _assert_get_client_not_called(mocker, cmd_message_manage)),
        (["delete", "12"], lambda mocker, tmp_path: _assert_get_client_not_called(mocker, cmd_message_manage)),
        (["group-chat", "Alice", "Bob", "--topic", "Release"], lambda mocker, tmp_path: _assert_get_client_not_called(mocker, cmd_group_chat)),
        (["forward", "12", "4", "--comment", "FYI"], lambda mocker, tmp_path: _assert_get_client_not_called(mocker, cmd_group_chat)),
        (["mark-read", "1", "2", "--unread"], lambda mocker, tmp_path: _assert_get_client_not_called(mocker, cmd_mark_read)),
        (["react", "like", "12", "13"], lambda mocker, tmp_path: _assert_get_client_not_called(mocker, cmd_reactions)),
        (["unreact", "like", "12", "13"], lambda mocker, tmp_path: _assert_get_client_not_called(mocker, cmd_reactions)),
        (["schedule", "5", "hello", "+30m"], lambda mocker, tmp_path: _assert_get_client_not_called(mocker, cmd_schedule)),
        (
            ["schedule-cancel", "1"],
            lambda mocker, tmp_path: (
                scheduler.add_scheduled(
                    conv_id="19:chat@thread.v2",
                    content="hello",
                    send_at=(datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    chat_title="Chat #5",
                ),
                mocker.patch.object(scheduler, "cancel_scheduled", side_effect=AssertionError("cancel should not run")),
            ),
        ),
        (
            ["schedule-run"],
            lambda mocker, tmp_path: (
                mocker.patch.object(
                    scheduler,
                    "get_pending",
                    return_value=[{"conv_id": "19:chat@thread.v2", "content": "hello", "chat_title": "Chat #5"}],
                ),
                _assert_get_client_not_called(mocker, cmd_schedule),
            ),
        ),
        (["set-status", "Busy", "--expiry", "+30m"], lambda mocker, tmp_path: _assert_get_client_not_called(mocker, cmd_presence)),
    ],
)
def test_mutating_commands_support_dry_run(
    runner,
    console_capture,
    mocker,
    tmp_path: Path,
    args: list[str],
    prepare,
):
    prepare(mocker, tmp_path)
    rendered_args = [part.format(tmp=tmp_path) for part in args]

    result = runner.invoke(cli_mod.cli, ["--dry-run", *rendered_args])

    assert result.exit_code == 0
    assert "Dry run:" in console_capture.getvalue()
