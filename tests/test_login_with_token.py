"""Tests for --with-token login functionality."""

from __future__ import annotations

import json

import pytest
import respx
from httpx import Response

import teams_cli.auth as auth
import teams_cli.cli as cli_mod
import teams_cli.commands.auth as commands_auth_mod
from teams_cli.constants import CHATSVC_BASE


class TestLoginWithTokenFunction:
    """Unit tests for auth.login_with_token()."""

    def test_rejects_empty_input(self):
        with pytest.raises(ValueError, match="No token provided"):
            auth.login_with_token("")

    def test_rejects_whitespace_only_input(self):
        with pytest.raises(ValueError, match="No token provided"):
            auth.login_with_token("   \n  ")

    def test_rejects_invalid_jwt_format_plain(self):
        with pytest.raises(ValueError, match="Invalid token format"):
            auth.login_with_token("not_a_jwt")

    def test_rejects_two_part_jwt(self):
        with pytest.raises(ValueError, match="Invalid token format"):
            auth.login_with_token("only.two")

    @respx.mock
    def test_plain_token_builds_correct_dict(self, fake_tokens, isolated_paths):
        ic3 = fake_tokens["ic3"]
        respx.get(f"{CHATSVC_BASE.format(region='amer')}/users/ME/properties").mock(
            return_value=Response(200, json={"ok": True})
        )
        result = auth.login_with_token(ic3, region="amer")
        assert result["ic3"] == ic3
        assert result["region"] == "amer"
        assert result["user_id"] == "user-123"
        assert result["graph"] == ""
        assert result["presence"] == ""
        assert result["csa"] == ""
        assert result["substrate"] == ""

    @respx.mock
    def test_json_bundle_extracts_all_tokens(self, fake_tokens, isolated_paths):
        bundle = json.dumps({
            "ic3": fake_tokens["ic3"],
            "graph": fake_tokens["graph"],
            "presence": fake_tokens["presence"],
            "csa": fake_tokens["csa"],
            "substrate": fake_tokens["substrate"],
            "region": "emea",
        })
        respx.get(f"{CHATSVC_BASE.format(region='emea')}/users/ME/properties").mock(
            return_value=Response(200, json={"ok": True})
        )
        result = auth.login_with_token(bundle)
        assert result["ic3"] == fake_tokens["ic3"]
        assert result["graph"] == fake_tokens["graph"]
        assert result["presence"] == fake_tokens["presence"]
        assert result["csa"] == fake_tokens["csa"]
        assert result["substrate"] == fake_tokens["substrate"]
        assert result["region"] == "emea"
        assert result["user_id"] == "user-123"

    def test_json_bundle_requires_ic3_key(self):
        bundle = json.dumps({"graph": "a.b.c", "region": "emea"})
        with pytest.raises(ValueError, match="must include an 'ic3' token"):
            auth.login_with_token(bundle)

    def test_json_bundle_region_overrides_parameter(self, fake_tokens, isolated_paths, monkeypatch):
        bundle = json.dumps({"ic3": fake_tokens["ic3"], "region": "apac"})
        monkeypatch.setattr(auth, "verify_tokens", lambda t: True)
        result = auth.login_with_token(bundle, region="emea")
        assert result["region"] == "apac"

    def test_json_bundle_falls_back_to_param_region(self, fake_tokens, isolated_paths, monkeypatch):
        bundle = json.dumps({"ic3": fake_tokens["ic3"]})
        monkeypatch.setattr(auth, "verify_tokens", lambda t: True)
        result = auth.login_with_token(bundle, region="amer")
        assert result["region"] == "amer"

    def test_json_invalid_optional_jwt_rejected(self, fake_tokens):
        bundle = json.dumps({"ic3": fake_tokens["ic3"], "graph": "not_valid_jwt"})
        with pytest.raises(ValueError, match="Invalid JWT format for 'graph'"):
            auth.login_with_token(bundle)

    @respx.mock
    def test_validation_failure_raises_runtime_error(self, fake_tokens, isolated_paths):
        respx.get(f"{CHATSVC_BASE.format(region='emea')}/users/ME/properties").mock(
            return_value=Response(401, json={"error": "unauthorized"})
        )
        with pytest.raises(RuntimeError, match="Token validation failed"):
            auth.login_with_token(fake_tokens["ic3"])

    @respx.mock
    def test_tokens_are_persisted(self, fake_tokens, isolated_paths):
        respx.get(f"{CHATSVC_BASE.format(region='emea')}/users/ME/properties").mock(
            return_value=Response(200, json={"ok": True})
        )
        auth.login_with_token(fake_tokens["ic3"])
        saved = json.loads(auth.TOKENS_FILE.read_text())
        assert saved["ic3"] == fake_tokens["ic3"]
        assert saved["ic3_exp"] > 0

    @respx.mock
    def test_plain_token_with_trailing_newline(self, fake_tokens, isolated_paths):
        respx.get(f"{CHATSVC_BASE.format(region='emea')}/users/ME/properties").mock(
            return_value=Response(200, json={"ok": True})
        )
        result = auth.login_with_token(fake_tokens["ic3"] + "\n")
        assert result["ic3"] == fake_tokens["ic3"]


class TestLoginWithTokenCLI:
    """Integration tests for the login --with-token command."""

    def test_with_token_rejects_empty_stdin(self, runner, console_capture):
        result = runner.invoke(cli_mod.cli, ["login", "--with-token"], input="")
        assert result.exit_code == 2
        assert "No token provided" in result.output

    def test_with_token_rejects_invalid_jwt(self, runner, console_capture):
        result = runner.invoke(cli_mod.cli, ["login", "--with-token"], input="bad_token\n")
        assert result.exit_code == 2
        assert "Invalid token format" in result.output

    def test_with_token_plain_token_succeeds(self, runner, console_capture, mocker, fake_tokens):
        mocker.patch.object(commands_auth_mod, "login_with_token", return_value=fake_tokens)
        mocker.patch.object(commands_auth_mod, "verify_tokens", return_value=True)
        result = runner.invoke(
            cli_mod.cli, ["login", "--with-token"], input=fake_tokens["ic3"]
        )
        assert result.exit_code == 0
        rendered = console_capture.getvalue()
        assert "Logged in successfully" in rendered
        assert "Region: emea" in rendered

    def test_with_token_json_bundle_succeeds(self, runner, console_capture, mocker, fake_tokens):
        bundle = json.dumps({
            "ic3": fake_tokens["ic3"],
            "graph": fake_tokens["graph"],
            "region": "amer",
        })
        mocker.patch.object(commands_auth_mod, "login_with_token", return_value={
            **fake_tokens, "region": "amer"
        })
        mocker.patch.object(commands_auth_mod, "verify_tokens", return_value=True)
        result = runner.invoke(cli_mod.cli, ["login", "--with-token"], input=bundle)
        assert result.exit_code == 0
        rendered = console_capture.getvalue()
        assert "Logged in successfully" in rendered

    def test_with_token_region_flag_passed_through(self, runner, mocker, fake_tokens):
        captured = {}

        def capture_login(raw_input, region="emea"):
            captured["region"] = region
            return fake_tokens

        mocker.patch.object(commands_auth_mod, "login_with_token", side_effect=capture_login)
        mocker.patch.object(commands_auth_mod, "verify_tokens", return_value=True)
        runner.invoke(
            cli_mod.cli, ["login", "--with-token", "--region", "amer"],
            input=fake_tokens["ic3"],
        )
        assert captured["region"] == "amer"

    def test_region_without_with_token_errors(self, runner, console_capture):
        result = runner.invoke(cli_mod.cli, ["login", "--region", "amer"])
        assert result.exit_code == 2
        assert "--region is only used with --with-token" in result.output

    def test_with_token_shows_optional_token_count_all(self, runner, console_capture, mocker, fake_tokens):
        mocker.patch.object(commands_auth_mod, "login_with_token", return_value=fake_tokens)
        mocker.patch.object(commands_auth_mod, "verify_tokens", return_value=True)
        result = runner.invoke(
            cli_mod.cli, ["login", "--with-token"], input=fake_tokens["ic3"]
        )
        assert result.exit_code == 0
        assert "Tokens: ic3 + 4/4 optional" in console_capture.getvalue()

    def test_with_token_shows_optional_token_count_none(self, runner, console_capture, mocker, fake_tokens):
        ic3_only = {
            "ic3": fake_tokens["ic3"],
            "graph": "",
            "presence": "",
            "csa": "",
            "substrate": "",
            "region": "emea",
            "user_id": "user-123",
        }
        mocker.patch.object(commands_auth_mod, "login_with_token", return_value=ic3_only)
        mocker.patch.object(commands_auth_mod, "verify_tokens", return_value=True)
        result = runner.invoke(
            cli_mod.cli, ["login", "--with-token"], input=fake_tokens["ic3"]
        )
        assert result.exit_code == 0
        assert "Tokens: ic3 + 0/4 optional" in console_capture.getvalue()
