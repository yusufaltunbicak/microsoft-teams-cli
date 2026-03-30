from __future__ import annotations

import json

import pytest

import teams_cli.cli as cli_mod
import teams_cli.commands.auth as cmd_auth
import teams_cli.commands.chat as cmd_chat
import teams_cli.commands.send as cmd_send
import teams_cli.commands._common as commands_common
import teams_cli.exceptions as exc_mod


def test_no_input_refusal_returns_usage_exit_code_and_human_error(runner, console_capture):
    result = runner.invoke(cli_mod.cli, ["--no-input", "chat-send", "5", "hello"])

    assert result.exit_code == 2
    assert "Refusing to send a message to a chat without confirmation" in result.output


def test_json_errors_use_envelope_and_not_found_exit_code(runner, mocker):
    class FakeClient:
        def get_message_detail(self, msg_num: str):
            raise exc_mod.ResourceNotFoundError(f"Unknown message #{msg_num}")

    mocker.patch.object(cmd_chat, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["read", "99", "--json"])

    assert result.exit_code == 5
    payload = json.loads(result.output)
    assert payload == {
        "ok": False,
        "schema_version": "1.0",
        "error": "Unknown message #99",
    }


def test_auth_errors_return_exit_code_4(runner, mocker, console_capture):
    mocker.patch.object(cmd_auth, "_get_client", side_effect=exc_mod.AuthRequiredError("Run teams login"))

    result = runner.invoke(cli_mod.cli, ["whoami"])

    assert result.exit_code == 4
    assert "Run teams login" in result.output


def test_rate_limit_errors_return_exit_code_7(runner, mocker):
    class FakeClient:
        def get_chats(self, top: int = 25, unread_only: bool = False):
            raise exc_mod.RateLimitError("Slow down")

    mocker.patch.object(cmd_chat, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["chats"])

    assert result.exit_code == 7


def test_config_errors_return_exit_code_10(runner, isolated_paths, mocker):
    isolated_paths["config_file"].write_text("browser: [broken")
    mocker.patch.object(cmd_chat, "_get_client", return_value=object())
    proxy = commands_common._ConfigProxy()
    mocker.patch.object(commands_common, "cfg", proxy)
    mocker.patch.object(cmd_chat, "cfg", proxy)

    result = runner.invoke(cli_mod.cli, ["chats"])

    assert result.exit_code == 10
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "while parsing a flow sequence" in payload["error"]
