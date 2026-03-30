from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone
from pathlib import Path

import click
import pytest

import teams_cli.anti_detection as anti_detection
import teams_cli.cli as cli_mod
import teams_cli.commands.attachments as cmd_attachments
import teams_cli.commands.auth as cmd_auth
import teams_cli.commands.mark_read as cmd_mark_read
import teams_cli.commands.presence as cmd_presence
import teams_cli.config as config_mod
import teams_cli.formatter as formatter
import teams_cli.serialization as serialization_mod
from teams_cli.models import User


def test_to_json_serializes_dataclasses_and_datetime_sentinels(make_user):
    user_payload = serialization_mod.to_json([make_user(user_id="user-1", display_name="Alice")], pretty=False)
    dt_payload = serialization_mod.to_json(
        {"when": datetime.min.replace(tzinfo=timezone.utc)},
        pretty=False,
    )

    user_data = json.loads(user_payload)
    dt_data = json.loads(dt_payload)
    assert "\n" not in user_payload
    assert user_data["ok"] is True
    assert user_data["data"][0]["display_name"] == "Alice"
    assert dt_data["data"]["when"] is None


def test_to_json_error_and_save_json_round_trip(tmp_path: Path):
    error_payload = json.loads(serialization_mod.to_json_error("boom"))
    path = tmp_path / "users.json"
    serialization_mod.save_json([User(id="user-1", display_name="Alice", email="a@example.com")], str(path))

    assert error_payload == {
        "ok": False,
        "schema_version": serialization_mod.SCHEMA_VERSION,
        "error": "boom",
    }
    assert json.loads(path.read_text()) == [
        {
            "id": "user-1",
            "display_name": "Alice",
            "email": "a@example.com",
            "user_type": "",
        }
    ]


@pytest.mark.parametrize(("isatty", "expected"), [(True, False), (False, True)])
def test_is_piped_follows_stdout_tty(monkeypatch: pytest.MonkeyPatch, isatty: bool, expected: bool):
    class FakeStdout:
        def isatty(self) -> bool:
            return isatty

    monkeypatch.setattr(serialization_mod.sys, "stdout", FakeStdout())
    importlib.reload(serialization_mod)

    assert serialization_mod.is_piped() is expected


def test_should_json_prefers_flag_or_piped(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("teams_cli.commands._common.is_piped", lambda: True)

    from teams_cli.commands._common import should_json

    assert should_json(False) is True
    assert should_json(True) is True


def test_load_config_returns_defaults_and_deep_merges(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "browser:\n"
        "  headless: true\n"
        "jitter:\n"
        "  write_base: 1.5\n"
        "timeout: 99\n"
    )

    loaded = config_mod.load_config(path)

    assert loaded["browser"] == {"headless": True, "timeout": 120}
    assert loaded["jitter"] == {"read_base": 0.3, "write_base": 1.5}
    assert loaded["timeout"] == 99


def test_load_config_missing_file_returns_defaults(tmp_path: Path):
    path = tmp_path / "missing.yaml"

    loaded = config_mod.load_config(path)

    assert loaded["region"] == "emea"
    assert loaded["browser"]["timeout"] == 120


def test_load_config_invalid_yaml_bubbles_parser_error(tmp_path: Path):
    path = tmp_path / "broken.yaml"
    path.write_text("browser: [broken")

    with pytest.raises(Exception):
        config_mod.load_config(path)


def test_browser_session_uses_env_proxy_timeout_and_close(monkeypatch: pytest.MonkeyPatch, mocker):
    monkeypatch.setenv("TEAMS_PROXY", "http://proxy.local")
    monkeypatch.setenv("TEAMS_TIMEOUT", "45")
    client = mocker.Mock()
    httpx_client = mocker.patch.object(anti_detection.httpx, "Client", return_value=client)

    session = anti_detection.BrowserSession(timeout=5)
    session.close()

    httpx_client.assert_called_once_with(timeout=45, follow_redirects=True, proxy="http://proxy.local")
    client.close.assert_called_once_with()


def test_browser_session_jitter_waits_for_minimum_gap(monkeypatch: pytest.MonkeyPatch, mocker):
    session = anti_detection.BrowserSession()
    monkeypatch.setattr(anti_detection.random, "uniform", lambda _a, _b: 1.0)
    clock = iter([1.0, 2.0])
    monkeypatch.setattr(anti_detection.time, "time", lambda: next(clock))
    sleep = mocker.patch.object(anti_detection.time, "sleep")

    session.jitter(is_write=True)

    sleep.assert_called_once_with(1.0)
    assert session._last_request_time == 2.0


def test_browser_headers_include_auth_and_extra_fields():
    session = anti_detection.BrowserSession()

    headers = session.browser_headers("token-123", extra={"X-Test": "1"})

    assert headers["Authorization"] == "Bearer token-123"
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Test"] == "1"


def test_attachments_command_handles_empty_state(runner, console_capture, mocker):
    class FakeClient:
        def get_attachments(self, msg_num: str):
            assert msg_num == "7"
            return []

    mocker.patch.object(cmd_attachments, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["attachments", "7"])

    assert result.exit_code == 0
    assert "No attachments." in console_capture.getvalue()


def test_attachments_command_can_emit_json_and_download(
    runner,
    mocker,
    make_attachment,
    tmp_path: Path,
):
    attachment = make_attachment(name="report.txt", content_type="text/plain")

    class FakeClient:
        def get_attachments(self, msg_num: str):
            return [attachment]

        def download_attachment(self, att):
            assert att is attachment
            return b"report-data"

    mocker.patch.object(cmd_attachments, "_get_client", return_value=FakeClient())

    result = runner.invoke(
        cli_mod.cli,
        ["attachments", "4", "--json", "--download", "--save-to", str(tmp_path)],
    )

    assert result.exit_code == 0
    decoder = json.JSONDecoder()
    payload, _ = decoder.raw_decode(result.output)
    assert payload["data"][0]["name"] == "report.txt"
    assert (tmp_path / "report.txt").read_bytes() == b"report-data"


def test_auth_login_reports_verification_failure(runner, console_capture, mocker, fake_tokens: dict[str, str]):
    mocker.patch.object(cmd_auth, "do_login", return_value=fake_tokens)
    mocker.patch.object(cmd_auth, "verify_tokens", return_value=False)

    result = runner.invoke(cli_mod.cli, ["login"])

    assert result.exit_code == 1
    assert "Login completed but token verification failed." in result.output


def test_auth_login_runtime_error_exits(runner, console_capture, mocker):
    mocker.patch.object(cmd_auth, "do_login", side_effect=RuntimeError("no browser"))

    result = runner.invoke(cli_mod.cli, ["login"])

    assert result.exit_code == 1
    assert "no browser" in result.output


def test_whoami_non_json_uses_formatter(runner, mocker):
    class FakeClient:
        _region = "emea"
        _user_id = "user-123"

        def get_me(self):
            return {"displayName": "Alice"}

    print_whoami = mocker.patch.object(formatter, "print_whoami")
    mocker.patch.object(cmd_auth, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["whoami"])

    assert result.exit_code == 0
    print_whoami.assert_called_once_with(
        {"displayName": "Alice", "region": "emea", "user_id": "user-123"}
    )


def test_mark_read_unread_calls_unread_client_path(runner, mocker):
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def mark_message_unread(self, msg_num: str):
            self.calls.append(msg_num)

        def mark_message_read(self, msg_num: str):
            raise AssertionError("read path should not be used")

    fake_client = FakeClient()
    mocker.patch.object(cmd_mark_read, "_get_client", return_value=fake_client)

    result = runner.invoke(cli_mod.cli, ["mark-read", "8", "--unread", "-y"])

    assert result.exit_code == 0
    assert fake_client.calls == ["8"]


def test_set_status_abort_stops_before_network_call(runner, mocker):
    get_client = mocker.patch.object(cmd_presence, "_get_client")

    result = runner.invoke(cli_mod.cli, ["set-status", "Busy"], input="n\n")

    assert result.exit_code != 0
    get_client.assert_not_called()


def test_set_status_invalid_expiry_exits_with_error(runner, console_capture, mocker):
    mocker.patch.object(cmd_presence, "_parse_schedule_time", side_effect=click.BadParameter("bad time"))
    mocker.patch.object(cmd_presence, "_get_client")

    result = runner.invoke(cli_mod.cli, ["set-status", "Busy", "--expiry", "never", "-y"])

    assert result.exit_code == 2
    assert "bad time" in result.output
