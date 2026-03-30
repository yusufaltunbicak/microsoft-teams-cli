from __future__ import annotations

import json

import teams_cli.cli as cli_mod
import teams_cli.commands.chat as cmd_chat


def test_enable_commands_allows_selected_read_only_command(runner, mocker):
    class FakeClient:
        def get_chats(self, top: int = 25, unread_only: bool = False):
            return []

    mocker.patch.object(cmd_chat, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["--enable-commands", "chats,status", "chats"])

    assert result.exit_code == 0


def test_enable_commands_denies_non_listed_command_before_body_runs(runner, mocker):
    get_client = mocker.patch.object(
        cmd_chat,
        "_get_client",
        side_effect=AssertionError("command body should not execute"),
    )

    result = runner.invoke(cli_mod.cli, ["--enable-commands", "status", "chats"])

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert 'Command "chats" is not enabled' in payload["error"]
    get_client.assert_not_called()


def test_enable_commands_supports_all_keyword(runner, mocker):
    class FakeClient:
        def get_chats(self, top: int = 25, unread_only: bool = False):
            return []

    mocker.patch.object(cmd_chat, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["--enable-commands", "all", "chats"])

    assert result.exit_code == 0


def test_enable_commands_is_case_insensitive(runner, mocker):
    class FakeClient:
        def get_chats(self, top: int = 25, unread_only: bool = False):
            return []

    mocker.patch.object(cmd_chat, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["--enable-commands", "CHATS,STATUS", "chats"])

    assert result.exit_code == 0
