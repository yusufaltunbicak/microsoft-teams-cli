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
