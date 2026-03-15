from __future__ import annotations

from datetime import datetime, timezone

import pytest

import teams_cli.cli as cli_mod
import teams_cli.commands.group_chat as cmd_group_chat


def test_group_chat_resolves_users_and_creates_chat(runner, mocker, make_user):
    users_alice = [make_user(user_id="user-a", display_name="Alice", email="alice@example.com")]
    users_bob = [make_user(user_id="user-b", display_name="Bob", email="bob@example.com")]

    class FakeClient:
        def __init__(self):
            self.search_calls: list[str] = []
            self.create_calls: list[tuple[list[str], str]] = []
            self.send_calls: list[tuple[str, str]] = []

        def search_users(self, query: str, top: int = 5):
            self.search_calls.append(query)
            if "alice" in query.lower():
                return users_alice
            if "bob" in query.lower():
                return users_bob
            return []

        def create_group_chat(self, user_ids: list[str], topic: str = ""):
            self.create_calls.append((user_ids, topic))
            return {"id": "19:new-group@thread.v2"}

        def send_message(self, conv_id: str, content: str, html: bool = True):
            self.send_calls.append((conv_id, content))
            return {}

    fake_client = FakeClient()
    mocker.patch.object(cmd_group_chat, "_get_client", return_value=fake_client)

    result = runner.invoke(
        cli_mod.cli,
        ["group-chat", "alice", "bob", "--topic", "Project X", "--message", "Hello team!", "-y"],
    )

    assert result.exit_code == 0
    assert fake_client.search_calls == ["alice", "bob"]
    assert fake_client.create_calls == [(["user-a", "user-b"], "Project X")]
    assert fake_client.send_calls == [("19:new-group@thread.v2", "Hello team!")]


def test_group_chat_without_message_skips_send(runner, mocker, make_user):
    users_alice = [make_user(user_id="user-a", display_name="Alice", email="alice@example.com")]

    class FakeClient:
        def __init__(self):
            self.create_calls: list[tuple[list[str], str]] = []
            self.send_calls: list[tuple[str, str]] = []

        def search_users(self, query: str, top: int = 5):
            return users_alice

        def create_group_chat(self, user_ids: list[str], topic: str = ""):
            self.create_calls.append((user_ids, topic))
            return {"id": "19:new-group@thread.v2"}

        def send_message(self, conv_id: str, content: str, html: bool = True):
            self.send_calls.append((conv_id, content))
            return {}

    fake_client = FakeClient()
    mocker.patch.object(cmd_group_chat, "_get_client", return_value=fake_client)

    result = runner.invoke(cli_mod.cli, ["group-chat", "alice", "-y"])

    assert result.exit_code == 0
    assert fake_client.create_calls == [(["user-a"], "")]
    assert fake_client.send_calls == []


def test_group_chat_user_not_found_prints_error(runner, console_capture, mocker):
    class FakeClient:
        def search_users(self, query: str, top: int = 5):
            return []

    mocker.patch.object(cmd_group_chat, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["group-chat", "nobody", "-y"])

    assert result.exit_code == 0
    assert "No user found matching 'nobody'" in console_capture.getvalue()


def test_forward_reads_message_and_sends_to_chat(runner, mocker, make_message):
    original = make_message(
        msg_id="m1",
        sender="Alice",
        sender_id="user-a",
        content="<p>Hello world</p>",
        text_content="Hello world",
    )

    class FakeClient:
        def __init__(self):
            self.forward_calls: list[tuple[str, str, str]] = []

        def forward_message(self, msg_num: str, chat_num: str, comment: str = ""):
            self.forward_calls.append((msg_num, chat_num, comment))
            return {}

    fake_client = FakeClient()
    mocker.patch.object(cmd_group_chat, "_get_client", return_value=fake_client)

    result = runner.invoke(cli_mod.cli, ["forward", "5", "3", "-y"])

    assert result.exit_code == 0
    assert fake_client.forward_calls == [("5", "3", "")]


def test_forward_with_comment(runner, mocker, make_message):
    class FakeClient:
        def __init__(self):
            self.forward_calls: list[tuple[str, str, str]] = []

        def forward_message(self, msg_num: str, chat_num: str, comment: str = ""):
            self.forward_calls.append((msg_num, chat_num, comment))
            return {}

    fake_client = FakeClient()
    mocker.patch.object(cmd_group_chat, "_get_client", return_value=fake_client)

    result = runner.invoke(
        cli_mod.cli,
        ["forward", "5", "3", "--comment", "Check this out", "-y"],
    )

    assert result.exit_code == 0
    assert fake_client.forward_calls == [("5", "3", "Check this out")]
