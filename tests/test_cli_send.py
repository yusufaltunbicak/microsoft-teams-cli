from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import teams_cli.cli as cli_mod
import teams_cli.commands.presence as cmd_presence
import teams_cli.commands.reactions as cmd_reactions
import teams_cli.commands.send as cmd_send


def test_chat_send_with_yes_calls_client(runner, mocker):
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def send_message_to_chat(self, chat_num: str, message: str):
            self.calls.append((chat_num, message))

    fake_client = FakeClient()
    mocker.patch.object(cmd_send, "_get_client", return_value=fake_client)

    result = runner.invoke(cli_mod.cli, ["chat-send", "5", "hello", "-y"])

    assert result.exit_code == 0
    assert fake_client.calls == [("5", "hello")]


def test_reply_with_yes_calls_client(runner, mocker):
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def reply_to_message(self, msg_num: str, message: str):
            self.calls.append((msg_num, message))

    fake_client = FakeClient()
    mocker.patch.object(cmd_send, "_get_client", return_value=fake_client)

    result = runner.invoke(cli_mod.cli, ["reply", "12", "sounds good", "-y"])

    assert result.exit_code == 0
    assert fake_client.calls == [("12", "sounds good")]


def test_send_multiple_users_prompts_and_sends_selected_user(
    runner,
    mocker,
    make_user,
):
    users = [
        make_user(user_id="user-a", display_name="Alice Smith", email="alice.smith@example.com"),
        make_user(user_id="user-b", display_name="Alice Brown", email="alice.brown@example.com"),
    ]

    class FakeClient:
        def __init__(self) -> None:
            self.sent: list[tuple[str, str, bool]] = []

        def search_users(self, query: str, top: int = 10):
            return users

        def send_message_to_user(self, user_id: str, message: str, html: bool = True):
            self.sent.append((user_id, message, html))

    fake_client = FakeClient()
    mocker.patch.object(cmd_send, "_get_client", return_value=fake_client)
    mocker.patch.object(cmd_send.click, "prompt", return_value=2)
    mocker.patch.object(cmd_send.click, "confirm", return_value=True)

    result = runner.invoke(cli_mod.cli, ["send", "alice", "Hello Alice Brown"])

    assert result.exit_code == 0
    assert fake_client.sent == [("user-b", "Hello Alice Brown", False)]


def test_send_no_user_prints_error(runner, console_capture, mocker):
    class FakeClient:
        def search_users(self, query: str, top: int = 5):
            return []

    mocker.patch.object(cmd_send, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["send", "nobody", "Hello"])

    assert result.exit_code == 0
    assert "No user found matching 'nobody'" in console_capture.getvalue()


def test_send_file_calls_client(runner, mocker, tmp_path: Path):
    file_path = tmp_path / "report.txt"
    file_path.write_text("content")

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        def send_file_to_chat(self, chat_num: str, file: str, message: str = ""):
            self.calls.append((chat_num, file, message))

    fake_client = FakeClient()
    mocker.patch.object(cmd_send, "_get_client", return_value=fake_client)

    result = runner.invoke(
        cli_mod.cli,
        ["send-file", "9", str(file_path), "--message", "Quarterly report", "-y"],
    )

    assert result.exit_code == 0
    assert fake_client.calls == [("9", str(file_path), "Quarterly report")]


@pytest.mark.parametrize(
    ("command_name", "method_name"),
    [("react", "add_reaction"), ("unreact", "remove_reaction")],
)
def test_reaction_commands_call_client(runner, mocker, command_name: str, method_name: str):
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def add_reaction(self, msg_num: str, emoji: str):
            self.calls.append((msg_num, emoji))

        def remove_reaction(self, msg_num: str, emoji: str):
            self.calls.append((msg_num, emoji))

    fake_client = FakeClient()
    mocker.patch.object(cmd_reactions, "_get_client", return_value=fake_client)

    result = runner.invoke(cli_mod.cli, [command_name, "like", "12", "-y"])

    assert result.exit_code == 0
    assert fake_client.calls == [("12", "like")]


def test_status_json_outputs_graph_payload(runner, mocker):
    class FakeClient:
        def get_presence(self):
            return {"availability": "Available", "activity": "Available"}

    mocker.patch.object(cmd_presence, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["status", "--json"])

    assert result.exit_code == 0
    assert '"availability": "Available"' in result.output


def test_set_status_builds_expiration_duration(runner, mocker, monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, object] = {}
    fixed_now = datetime(2026, 3, 11, 10, 0, tzinfo=timezone.utc)
    expires_at = datetime(2026, 3, 11, 11, 30, tzinfo=timezone.utc)

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    class FakeClient:
        def _ups_put(self, path: str, payload: dict):
            captured["path"] = path
            captured["payload"] = payload
            return {}

    monkeypatch.setattr(cmd_presence, "datetime", FakeDateTime)
    mocker.patch.object(cmd_presence, "_parse_schedule_time", return_value=expires_at)
    mocker.patch.object(cmd_presence, "_get_client", return_value=FakeClient())

    result = runner.invoke(
        cli_mod.cli,
        ["set-status", "Busy", "--expiry", "+90m", "-y"],
    )

    assert result.exit_code == 0
    assert captured["path"] == "/me/forceavailability/"
    assert captured["payload"] == {
        "availability": "Busy",
        "desiredExpirationTime": "2026-03-11T11:30:00.000Z",
    }
