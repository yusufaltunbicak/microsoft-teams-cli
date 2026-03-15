"""Tests for multi-ID react/unreact and mark-read commands."""

from __future__ import annotations

import pytest

import teams_cli.cli as cli_mod
import teams_cli.commands.mark_read as cmd_mark_read
import teams_cli.commands.reactions as cmd_reactions


# ------------------------------------------------------------------
# react: multiple message IDs
# ------------------------------------------------------------------


def test_react_multiple_ids(runner, mocker):
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def add_reaction(self, msg_num: str, emoji: str):
            self.calls.append((msg_num, emoji))

    fake_client = FakeClient()
    mocker.patch.object(cmd_reactions, "_get_client", return_value=fake_client)

    result = runner.invoke(cli_mod.cli, ["react", "like", "1", "2", "3", "-y"])

    assert result.exit_code == 0
    assert fake_client.calls == [("1", "like"), ("2", "like"), ("3", "like")]


def test_react_single_id(runner, mocker):
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def add_reaction(self, msg_num: str, emoji: str):
            self.calls.append((msg_num, emoji))

    fake_client = FakeClient()
    mocker.patch.object(cmd_reactions, "_get_client", return_value=fake_client)

    result = runner.invoke(cli_mod.cli, ["react", "heart", "7", "-y"])

    assert result.exit_code == 0
    assert fake_client.calls == [("7", "heart")]


def test_react_partial_failure(runner, console_capture, mocker):
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def add_reaction(self, msg_num: str, emoji: str):
            if msg_num == "2":
                raise ValueError("Unknown message #2")
            self.calls.append((msg_num, emoji))

    fake_client = FakeClient()
    mocker.patch.object(cmd_reactions, "_get_client", return_value=fake_client)

    result = runner.invoke(cli_mod.cli, ["react", "like", "1", "2", "3", "-y"])

    assert result.exit_code == 0
    assert fake_client.calls == [("1", "like"), ("3", "like")]
    output = console_capture.getvalue()
    assert "Failed to react on message #2" in output


# ------------------------------------------------------------------
# unreact: multiple message IDs
# ------------------------------------------------------------------


def test_unreact_multiple_ids(runner, mocker):
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def remove_reaction(self, msg_num: str, emoji: str):
            self.calls.append((msg_num, emoji))

    fake_client = FakeClient()
    mocker.patch.object(cmd_reactions, "_get_client", return_value=fake_client)

    result = runner.invoke(cli_mod.cli, ["unreact", "sad", "4", "5", "-y"])

    assert result.exit_code == 0
    assert fake_client.calls == [("4", "sad"), ("5", "sad")]


def test_unreact_single_id(runner, mocker):
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def remove_reaction(self, msg_num: str, emoji: str):
            self.calls.append((msg_num, emoji))

    fake_client = FakeClient()
    mocker.patch.object(cmd_reactions, "_get_client", return_value=fake_client)

    result = runner.invoke(cli_mod.cli, ["unreact", "angry", "10", "-y"])

    assert result.exit_code == 0
    assert fake_client.calls == [("10", "angry")]


def test_unreact_partial_failure(runner, console_capture, mocker):
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def remove_reaction(self, msg_num: str, emoji: str):
            if msg_num == "5":
                raise ValueError("Unknown message #5")
            self.calls.append((msg_num, emoji))

    fake_client = FakeClient()
    mocker.patch.object(cmd_reactions, "_get_client", return_value=fake_client)

    result = runner.invoke(cli_mod.cli, ["unreact", "sad", "4", "5", "6", "-y"])

    assert result.exit_code == 0
    assert fake_client.calls == [("4", "sad"), ("6", "sad")]
    output = console_capture.getvalue()
    assert "Failed to remove reaction from message #5" in output


# ------------------------------------------------------------------
# mark-read: single and multiple message IDs
# ------------------------------------------------------------------


def test_mark_read_single_id(runner, mocker):
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def mark_message_read(self, msg_num: str):
            self.calls.append(msg_num)

    fake_client = FakeClient()
    mocker.patch.object(cmd_mark_read, "_get_client", return_value=fake_client)

    result = runner.invoke(cli_mod.cli, ["mark-read", "3", "-y"])

    assert result.exit_code == 0
    assert fake_client.calls == ["3"]


def test_mark_read_multiple_ids(runner, mocker):
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def mark_message_read(self, msg_num: str):
            self.calls.append(msg_num)

    fake_client = FakeClient()
    mocker.patch.object(cmd_mark_read, "_get_client", return_value=fake_client)

    result = runner.invoke(cli_mod.cli, ["mark-read", "1", "2", "3", "-y"])

    assert result.exit_code == 0
    assert fake_client.calls == ["1", "2", "3"]


def test_mark_read_partial_failure(runner, console_capture, mocker):
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def mark_message_read(self, msg_num: str):
            if msg_num == "2":
                raise ValueError("Unknown message #2")
            self.calls.append(msg_num)

    fake_client = FakeClient()
    mocker.patch.object(cmd_mark_read, "_get_client", return_value=fake_client)

    result = runner.invoke(cli_mod.cli, ["mark-read", "1", "2", "3", "-y"])

    assert result.exit_code == 0
    assert fake_client.calls == ["1", "3"]
    output = console_capture.getvalue()
    assert "Failed to mark message #2 as read" in output


def test_mark_read_no_args_shows_error(runner):
    result = runner.invoke(cli_mod.cli, ["mark-read", "-y"])

    assert result.exit_code != 0
    assert "Missing argument" in result.output
