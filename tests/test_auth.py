from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from types import ModuleType

import pytest
import respx
from httpx import Response

import teams_cli.auth as auth
import teams_cli.cli as cli_mod
import teams_cli.commands.auth as commands_auth_mod
from teams_cli.constants import CHATSVC_BASE


def test_get_tokens_prefers_env_token(monkeypatch: pytest.MonkeyPatch, token_factory):
    token = token_factory(oid="env-user")
    monkeypatch.setenv("TEAMS_IC3_TOKEN", token)
    monkeypatch.setenv("TEAMS_REGION", "amer")
    monkeypatch.setattr(auth, "_load_cached_tokens", lambda: pytest.fail("cache should not be used"))

    tokens = auth.get_tokens()

    assert tokens == {
        "ic3": token,
        "region": "amer",
        "user_id": "env-user",
    }


def test_load_cached_tokens_returns_none_when_expired(isolated_paths, token_factory):
    auth.TOKENS_FILE.write_text(
        json.dumps(
            {
                "ic3": token_factory(),
                "ic3_exp": int((datetime.now(timezone.utc) - timedelta(minutes=1)).timestamp()),
            }
        )
    )

    assert auth._load_cached_tokens() is None


def test_save_tokens_persists_metadata_and_profile(fake_tokens: dict[str, str]):
    auth._save_tokens(fake_tokens)

    saved = json.loads(auth.TOKENS_FILE.read_text())
    profile = json.loads(auth.USER_PROFILE_FILE.read_text())

    assert saved["region"] == "emea"
    assert saved["user_id"] == "user-123"
    assert saved["ic3_exp"] > 0
    assert saved["csa"] == fake_tokens["csa"]
    assert profile == {"display_name": "Test User", "user_id": "user-123"}


def test_get_auth_status_prefers_env_token(monkeypatch: pytest.MonkeyPatch, token_factory):
    token = token_factory(oid="env-user", name="Env User")
    monkeypatch.setenv("TEAMS_IC3_TOKEN", token)
    monkeypatch.setenv("TEAMS_REGION", "amer")
    monkeypatch.setattr(auth, "_load_cached_tokens", lambda: pytest.fail("cache should not be used"))

    status = auth.get_auth_status()

    assert status["auth_source"] == "env"
    assert status["identity"] == {
        "region": "amer",
        "user_id": "env-user",
        "display_name": "Env User",
    }
    assert status["tokens"] == {
        "ic3": True,
        "graph": False,
        "presence": False,
        "csa": False,
        "substrate": False,
    }
    assert status["ic3"]["expires_at"]
    assert status["ic3"]["expires_in_human"]


def test_get_auth_status_reports_missing_with_cache_metadata(fake_tokens: dict[str, str], isolated_paths):
    auth._save_tokens(fake_tokens)
    saved = json.loads(auth.TOKENS_FILE.read_text())
    saved["ic3_exp"] = int((datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp())
    auth.TOKENS_FILE.write_text(json.dumps(saved))

    status = auth.get_auth_status()

    assert status["auth_source"] == "cache"
    assert status["cache"]["token_cache_exists"] is True
    assert status["tokens"]["ic3"] is True
    assert status["identity"]["display_name"] == "Test User"
    assert status["ic3"]["expires_in_human"].endswith("ago")


def test_get_auth_status_check_only_verifies_active_ic3(mocker, fake_tokens: dict[str, str], isolated_paths):
    auth._save_tokens(fake_tokens)
    verify = mocker.patch.object(auth, "verify_tokens", return_value=True)

    status = auth.get_auth_status(check=True)

    assert status["auth_source"] == "cache"
    assert status["ic3"]["valid"] is True
    verify.assert_called_once()


@respx.mock
def test_verify_tokens_returns_true_on_200(fake_tokens: dict[str, str]):
    respx.get(f"{CHATSVC_BASE.format(region='emea')}/users/ME/properties").mock(
        return_value=Response(200, json={"ok": True})
    )

    assert auth.verify_tokens(fake_tokens) is True


def test_extract_tokens_from_page_classifies_tokens(fake_tokens: dict[str, str]):
    class FakePage:
        def evaluate(self, _script: str) -> dict[str, str]:
            return {
                "ic3": fake_tokens["ic3"],
                "graph": fake_tokens["graph"],
                "presence": fake_tokens["presence"],
                "csa": fake_tokens["csa"],
            }

    result = auth._extract_tokens_from_page(FakePage())

    assert result["ic3"] == fake_tokens["ic3"]
    assert result["graph"] == fake_tokens["graph"]
    assert result["presence"] == fake_tokens["presence"]
    assert result["csa"] == fake_tokens["csa"]
    assert result["user_id"] == "user-123"
    assert result["region"] == "emea"


def test_login_uses_mocked_playwright_and_saves_tokens(
    monkeypatch: pytest.MonkeyPatch,
    fake_tokens: dict[str, str],
    isolated_paths,
):
    captured: dict[str, object] = {}
    extraction_calls = {"count": 0}
    auth.BROWSER_STATE_FILE.write_text("{}")

    class FakePage:
        def goto(self, url: str, wait_until: str | None = None) -> None:
            captured["goto"] = (url, wait_until)

        def wait_for_timeout(self, _ms: int) -> None:
            captured["wait_count"] = captured.get("wait_count", 0) + 1

    class FakeContext:
        def new_page(self) -> FakePage:
            return FakePage()

        def storage_state(self, path: str) -> None:
            captured["storage_state"] = path
            auth.BROWSER_STATE_FILE.write_text("{}")

    class FakeBrowser:
        def new_context(self, **kwargs):
            captured["new_context"] = kwargs
            return FakeContext()

        def close(self) -> None:
            captured["closed"] = True

    class FakePlaywright:
        chromium = type("Chromium", (), {"launch": lambda self, headless=False: FakeBrowser()})()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    sync_api = ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: FakePlaywright()
    monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)

    def fake_extract(_page, debug: bool = False):
        extraction_calls["count"] += 1
        if extraction_calls["count"] == 1:
            return {"ic3": fake_tokens["ic3"], "region": "emea", "user_id": "user-123"}
        return fake_tokens

    monkeypatch.setattr(auth, "_extract_tokens_from_page", fake_extract)
    now = {"value": 0.0}
    monkeypatch.setattr(auth.time, "time", lambda: now["value"])

    def advance_time(self, _ms: int) -> None:
        captured["wait_count"] = captured.get("wait_count", 0) + 1
        now["value"] += 6.0

    FakePage.wait_for_timeout = advance_time  # type: ignore[method-assign]

    tokens = auth.login(force=False, debug=False)

    assert tokens["user_id"] == "user-123"
    assert tokens["csa"] == fake_tokens["csa"]
    assert captured["goto"] == ("https://teams.cloud.microsoft", "domcontentloaded")
    assert captured["new_context"] == {
        "user_agent": auth.USER_AGENT,
        "storage_state": str(auth.BROWSER_STATE_FILE),
    }
    assert captured["storage_state"] == str(auth.BROWSER_STATE_FILE)
    assert extraction_calls["count"] >= 2
    assert json.loads(auth.TOKENS_FILE.read_text())["user_id"] == "user-123"


def test_login_command_reports_success(runner, console_capture, mocker, fake_tokens: dict[str, str]):
    mocker.patch.object(commands_auth_mod, "do_login", return_value=fake_tokens)
    mocker.patch.object(commands_auth_mod, "verify_tokens", return_value=True)

    result = runner.invoke(cli_mod.cli, ["login"])

    assert result.exit_code == 0
    rendered = console_capture.getvalue()
    assert "Logged in successfully. Tokens cached." in rendered
    assert "Region: emea" in rendered
    assert "User ID: user-123" in rendered


def test_auth_status_command_json_output(runner, mocker):
    mocker.patch.object(commands_auth_mod, "get_auth_status", return_value={
        "auth_source": "cache",
        "cache": {
            "token_cache_exists": True,
            "token_cache_path": "/tmp/tokens.json",
            "browser_state_exists": True,
            "browser_state_path": "/tmp/browser-state.json",
        },
        "identity": {
            "region": "emea",
            "user_id": "user-123",
            "display_name": "Test User",
        },
        "tokens": {
            "ic3": True,
            "graph": True,
            "presence": True,
            "csa": False,
            "substrate": True,
        },
        "ic3": {
            "expires_at": "2026-03-31T12:00:00+00:00",
            "expires_in_seconds": 3600,
            "expires_in_human": "1h",
            "valid": True,
        },
    })

    result = runner.invoke(cli_mod.cli, ["auth-status", "--check", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["data"]["auth_source"] == "cache"
    assert payload["data"]["ic3"]["valid"] is True


def test_auth_status_command_text_output(runner, console_capture, mocker):
    mocker.patch.object(commands_auth_mod, "get_auth_status", return_value={
        "auth_source": "missing",
        "cache": {
            "token_cache_exists": False,
            "token_cache_path": "/tmp/tokens.json",
            "browser_state_exists": True,
            "browser_state_path": "/tmp/browser-state.json",
        },
        "identity": {
            "region": "emea",
            "user_id": "",
            "display_name": "",
        },
        "tokens": {
            "ic3": False,
            "graph": False,
            "presence": False,
            "csa": False,
            "substrate": False,
        },
        "ic3": {
            "expires_at": None,
            "expires_in_seconds": None,
            "expires_in_human": None,
            "valid": None,
        },
    })

    result = runner.invoke(cli_mod.cli, ["auth-status", "--check"])

    assert result.exit_code == 0
    rendered = console_capture.getvalue()
    assert "Auth Source:" in rendered
    assert "missing" in rendered
    assert "IC3 Check:" in rendered
    assert "unavailable" in rendered
