"""Modern MSAL extraction and noninteractive auth, with no real account calls."""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import respx

import teams_cli.auth as auth
import teams_cli.commands.auth as auth_commands
import teams_cli.cli as cli
from teams_cli.constants import TEAMS_CLIENT_ID
from teams_cli.exceptions import AuthRequiredError, RetryableError, TokenExpiredError
from teams_cli.msal_cache import TOKEN_AUDIENCES, claims, credentials, extract_tokens, refresh_scopes


def _bundle(token_factory, *, exp=None, oid="user-123", tid="tenant-123"):
    result = {key: token_factory(aud="https://" + host, oid=oid, tid=tid,
              exp=int(time.time() + 3600) if exp is None else exp)
              for key, host in TOKEN_AUDIENCES.items()}
    return {**result, "region": "emea", "user_id": oid}


def _state(bundle, *, legacy=False, refresh=True):
    local = []
    for key, host in TOKEN_AUDIENCES.items():
        token = bundle.get(key)
        if not token:
            continue
        payload = claims(token)
        name = f"user-env-accesstoken-{host}" if legacy else f"msal.2|user|env|accesstoken|client|tenant|{host}|"
        local.append({"name": name, "value": json.dumps({
            "credentialType": "AccessToken", "clientId": TEAMS_CLIENT_ID,
            "secret": token, "target": f"https://{host}/user_impersonation https://{host}/.default",
            "homeAccountId": payload.get("oid", "") + "." + payload.get("tid", ""),
            "realm": payload.get("tid", ""), "expiresOn": str(payload.get("exp", 0)),
        })})
    payload = claims(bundle["ic3"])
    if refresh:
        local.append({"name": "msal.2|user|env|refreshtoken|client|", "value": json.dumps({
            "credentialType": "RefreshToken", "clientId": TEAMS_CLIENT_ID,
            "secret": "private-refresh-secret", "homeAccountId": payload["oid"] + "." + payload["tid"],
            "realm": payload["tid"], "cachedAt": str(time.time()),
        })})
    local.append({"name": "Discover.DISCOVER-REGION-GTM", "value": json.dumps({
        "regionGtms": json.dumps({"chatService": "https://teams.cloud.microsoft/api/chatsvc/emea/v1"})})})
    return {"cookies": [], "origins": [{"origin": "https://teams.cloud.microsoft", "localStorage": local}]}


def _save_state(bundle, **kwargs):
    auth.BROWSER_STATE_FILE.write_text(json.dumps(_state(bundle, **kwargs)))


@pytest.mark.parametrize("legacy", [False, True])
def test_extract_modern_and_legacy_cache(token_factory, legacy):
    bundle = _bundle(token_factory)
    result = extract_tokens(_state(bundle, legacy=legacy))
    assert all(result[key] == bundle[key] for key in TOKEN_AUDIENCES)
    assert result["region"] == "emea"
    assert result["tenant_id"] == "tenant-123"


def test_extract_uses_jwt_audience_when_target_is_unqualified(token_factory):
    bundle = _bundle(token_factory)
    state = _state(bundle)
    entry = state["origins"][0]["localStorage"][1]
    value = json.loads(entry["value"])
    value["target"] = "User.ReadBasic.All"
    entry["name"] = "msal.2|unqualified|accesstoken|"
    entry["value"] = json.dumps(value)
    assert extract_tokens(state)["graph"] == bundle["graph"]


def test_extract_selects_newest_token_regardless_of_iteration_order(token_factory):
    new = _bundle(token_factory)
    old = _bundle(token_factory, exp=int(time.time()) - 1)
    state = _state(new)
    state["origins"][0]["localStorage"] += _state(old)["origins"][0]["localStorage"]
    assert extract_tokens(state)["ic3"] == new["ic3"]


def test_extract_excludes_expired_resource_only(token_factory):
    bundle = _bundle(token_factory)
    bundle["graph"] = token_factory(aud="https://graph.microsoft.com", exp=int(time.time()) - 1)
    result = extract_tokens(_state(bundle))
    assert result.get("ic3")
    assert "graph" not in result


def test_extract_refuses_ambiguous_accounts_and_matches_expected_tenant(token_factory):
    first = _bundle(token_factory)
    second = _bundle(token_factory, tid="different-tenant")
    state = _state(first)
    state["origins"][0]["localStorage"] += _state(second)["origins"][0]["localStorage"]
    with pytest.raises(AuthRequiredError, match="multiple accounts"):
        extract_tokens(state)
    assert extract_tokens(state, first)["ic3"] == first["ic3"]


def test_extract_does_not_mix_other_account_optional_tokens(token_factory):
    first = _bundle(token_factory)
    del first["graph"]
    second = _bundle(token_factory, oid="other-user")
    state = _state(first)
    state["origins"][0]["localStorage"] += _state(second)["origins"][0]["localStorage"]
    assert "graph" not in extract_tokens(state, first)


def test_extract_ignores_untrusted_origins_and_client_ids(token_factory):
    bundle = _bundle(token_factory)
    state = _state(bundle)
    state["origins"][0]["origin"] = "https://untrusted.example"
    assert credentials(state) == []
    state = _state(bundle)
    for entry in state["origins"][0]["localStorage"]:
        value = json.loads(entry["value"])
        value["clientId"] = "other-application"
        entry["value"] = json.dumps(value)
    assert credentials(state) == []


def test_refresh_scopes_excludes_default_when_explicit_scopes_exist(token_factory):
    record = credentials(_state(_bundle(token_factory)))[0]
    assert refresh_scopes(record) == "https://ic3.teams.office.com/user_impersonation"
    record.value["target"] = "https://ic3.teams.office.com/.default"
    assert refresh_scopes(record).endswith("/.default")


def test_missing_credentials_never_open_browser(mocker):
    login = mocker.patch.object(auth, "login", side_effect=AssertionError("interactive login"))
    with pytest.raises(AuthRequiredError, match="teams login"):
        auth.get_tokens()
    login.assert_not_called()


def test_get_tokens_recovers_valid_saved_access_tokens_without_network(token_factory, mocker):
    bundle = _bundle(token_factory)
    _save_state(bundle)
    post = mocker.patch("httpx.Client.post", side_effect=AssertionError("network"))
    assert auth.get_tokens()["ic3"] == bundle["ic3"]
    post.assert_not_called()
    assert auth.TOKENS_FILE.stat().st_mode & 0o777 == 0o600


def test_expired_env_token_never_falls_back_to_another_account(token_factory, monkeypatch, mocker):
    monkeypatch.setenv("TEAMS_IC3_TOKEN", token_factory(exp=int(time.time()) - 1))
    refresh = mocker.patch.object(auth, "refresh_tokens")
    with pytest.raises(AuthRequiredError, match="TEAMS_IC3_TOKEN"):
        auth.get_tokens()
    refresh.assert_not_called()


def test_unknown_token_expiry_is_not_assumed_valid(monkeypatch):
    monkeypatch.setenv("TEAMS_IC3_TOKEN", "opaque-token")
    with pytest.raises(AuthRequiredError, match="no usable expiry"):
        auth.get_tokens()


@respx.mock
def test_silent_refresh_updates_only_requested_audience_and_private_caches(token_factory):
    bundle = _bundle(token_factory)
    bundle["graph"] = token_factory(aud="https://graph.microsoft.com", exp=int(time.time()) - 1)
    _save_state(bundle)
    auth._save_tokens(bundle)
    fresh = token_factory(aud="https://graph.microsoft.com", exp=int(time.time()) + 7200)
    route = respx.post("https://login.microsoftonline.com/tenant-123/oauth2/v2.0/token").mock(
        return_value=httpx.Response(200, json={"access_token": fresh, "refresh_token": "rotated-private-secret"}))
    result = auth.refresh_tokens(bundle, required=("graph",))
    assert result["graph"] == fresh
    assert result["ic3"] == bundle["ic3"]
    assert route.call_count == 1
    request = route.calls[0].request
    assert request.headers["Origin"] == "https://teams.cloud.microsoft"
    assert b".default" not in request.content
    saved = json.loads(auth.TOKENS_FILE.read_text())
    assert saved["graph_exp"] > time.time()
    state = json.loads(auth.BROWSER_STATE_FILE.read_text())
    refresh = next(record for record in credentials(state) if record.kind == "refreshtoken")
    assert refresh.token == "rotated-private-secret"
    for path in (auth.TOKENS_FILE, auth.BROWSER_STATE_FILE, auth.USER_PROFILE_FILE, auth.CACHE_DIR / "auth.lock"):
        assert path.stat().st_mode & 0o777 == 0o600
    assert not list(auth.CACHE_DIR.glob(".tokens.json.*"))


@respx.mock
@pytest.mark.parametrize("payload", [
    {"error": "interaction_required", "error_description": "private-refresh-secret MFA"},
    {"error": "invalid_grant", "error_description": "private-refresh-secret expired"},
])
def test_mfa_and_expired_refresh_fail_once_without_secrets(token_factory, payload, mocker):
    bundle = _bundle(token_factory, exp=int(time.time()) - 1)
    _save_state(bundle)
    route = respx.post("https://login.microsoftonline.com/tenant-123/oauth2/v2.0/token").mock(
        return_value=httpx.Response(400, json=payload))
    login = mocker.patch.object(auth, "login")
    with pytest.raises(AuthRequiredError, match="sign-in or MFA") as error:
        auth.refresh_tokens(bundle)
    assert "private-refresh-secret" not in str(error.value)
    assert route.call_count == 1
    login.assert_not_called()


@respx.mock
@pytest.mark.parametrize("change", [{"oid": "other-user"}, {"tid": "other-tenant"}, {"aud": "https://graph.microsoft.com"}])
def test_refresh_rejects_wrong_account_tenant_or_audience(token_factory, change):
    bundle = _bundle(token_factory, exp=int(time.time()) - 1)
    _save_state(bundle)
    fresh_claims = {"aud": "https://ic3.teams.office.com", **change}
    respx.post("https://login.microsoftonline.com/tenant-123/oauth2/v2.0/token").mock(
        return_value=httpx.Response(200, json={"access_token": token_factory(**fresh_claims)}))
    with pytest.raises(AuthRequiredError, match="different Teams account or resource"):
        auth.refresh_tokens(bundle)
    assert not auth.TOKENS_FILE.exists()


@respx.mock
@pytest.mark.parametrize("status", [429, 500])
def test_refresh_transient_failures_are_bounded_and_retryable(token_factory, status):
    bundle = _bundle(token_factory, exp=int(time.time()) - 1)
    _save_state(bundle)
    route = respx.post("https://login.microsoftonline.com/tenant-123/oauth2/v2.0/token").mock(
        return_value=httpx.Response(status, json={}))
    with pytest.raises(RetryableError):
        auth.refresh_tokens(bundle)
    assert route.call_count == 1


@respx.mock
def test_concurrent_refreshes_use_one_network_grant(token_factory):
    bundle = _bundle(token_factory, exp=int(time.time()) - 1)
    _save_state(bundle)
    fresh = token_factory(aud="https://ic3.teams.office.com")
    route = respx.post("https://login.microsoftonline.com/tenant-123/oauth2/v2.0/token").mock(
        return_value=httpx.Response(200, json={"access_token": fresh}))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: auth.refresh_tokens(bundle), range(2)))
    assert [result["ic3"] for result in results] == [fresh, fresh]
    assert route.call_count == 1


def test_cache_account_switch_does_not_silently_adopt_new_account(token_factory):
    bundle = _bundle(token_factory)
    auth._save_tokens(_bundle(token_factory, oid="other-user"))
    with pytest.raises(AuthRequiredError, match="account changed"):
        auth.refresh_tokens(bundle, force=True)


def test_login_no_input_requires_explicit_silent_or_token_mode(runner, mocker):
    login = mocker.patch.object(auth_commands, "do_login")
    result = runner.invoke(cli.cli, ["--no-input", "login"])
    assert result.exit_code == 4
    login.assert_not_called()


def test_login_silent_uses_saved_resources_and_no_browser(runner, mocker, fake_tokens):
    mocker.patch.object(auth_commands, "get_tokens", return_value=fake_tokens)
    refresh = mocker.patch.object(auth_commands, "refresh_tokens", return_value=fake_tokens)
    mocker.patch.object(auth_commands, "verify_tokens", return_value=True)
    interactive = mocker.patch.object(auth_commands, "do_login")
    result = runner.invoke(cli.cli, ["--no-input", "login", "--silent"])
    assert result.exit_code == 0
    assert refresh.call_args.kwargs["force"] is True
    interactive.assert_not_called()


@pytest.mark.parametrize("args", [["--silent", "--force"], ["--silent", "--with-token"], ["--silent", "--region", "amer"]])
def test_silent_login_refuses_incompatible_modes(runner, args):
    result = runner.invoke(cli.cli, ["login", *args])
    assert result.exit_code == 2


@respx.mock
def test_client_refreshes_read_401_once_at_request_level(teams_client, token_factory, mocker):
    fresh = {**teams_client._tokens, "ic3": token_factory(exp=int(time.time()) + 10800)}
    refresh = mocker.patch.object(auth, "refresh_tokens", return_value=fresh)
    route = respx.get(teams_client._chatsvc + "/users/ME/properties").mock(side_effect=[
        httpx.Response(401, json={}), httpx.Response(200, json={"ok": True})])
    assert teams_client._ic3_get("/users/ME/properties") == {"ok": True}
    assert route.call_count == 2
    refresh.assert_called_once_with(mocker.ANY, required=("ic3",), force=True)
    assert route.calls[-1].request.headers["Authorization"] == "Bearer " + fresh["ic3"]


@respx.mock
def test_client_does_not_loop_after_refresh_401(teams_client, mocker):
    refresh = mocker.patch.object(auth, "refresh_tokens", return_value=teams_client._tokens)
    route = respx.get(teams_client._chatsvc + "/users/ME/properties").mock(return_value=httpx.Response(401, json={}))
    with pytest.raises(TokenExpiredError):
        teams_client._ic3_get("/users/ME/properties")
    assert route.call_count == 2
    assert refresh.call_count == 1


@respx.mock
def test_client_never_replays_mutating_401(teams_client, mocker):
    refresh = mocker.patch.object(auth, "refresh_tokens")
    route = respx.post(teams_client._chatsvc + "/mutation").mock(return_value=httpx.Response(401, json={}))
    with pytest.raises(TokenExpiredError):
        teams_client._ic3_post("/mutation", {"example": True}, is_write=True)
    assert route.call_count == 1
    refresh.assert_not_called()


@respx.mock
def test_client_preflights_only_expired_audience(teams_client, token_factory, mocker):
    teams_client._tokens["graph"] = token_factory(exp=int(time.time()) - 1)
    teams_client._graph = teams_client._tokens["graph"]
    fresh = {**teams_client._tokens, "graph": token_factory(exp=int(time.time()) + 10800)}
    refresh = mocker.patch.object(auth, "refresh_tokens", return_value=fresh)
    route = respx.get("https://graph.microsoft.com/v1.0/me").mock(return_value=httpx.Response(200, json={"displayName": "Test"}))
    assert teams_client._graph_get("/me")["displayName"] == "Test"
    assert route.call_count == 1
    assert refresh.call_args.kwargs == {"required": ("graph",), "force": False}


@respx.mock
def test_client_explicit_read_post_can_recover_auth(teams_client, mocker):
    refresh = mocker.patch.object(auth, "refresh_tokens", return_value=teams_client._tokens)
    route = respx.post(teams_client._ups + "/getpresence/").mock(side_effect=[httpx.Response(401), httpx.Response(200, json={})])
    assert teams_client._ups_post("/getpresence/", {"mris": []}) == {}
    assert route.call_count == 2
    assert refresh.call_args.kwargs == {"required": ("presence",), "force": True}


@respx.mock
@pytest.mark.parametrize("status", [500, 502])
def test_client_never_replays_uncertain_write_failures(teams_client, status):
    route = respx.post(teams_client._chatsvc + "/mutation").mock(return_value=httpx.Response(status, json={}))
    with pytest.raises(httpx.HTTPStatusError):
        teams_client._ic3_post("/mutation", {}, is_write=True)
    assert route.call_count == 1


def test_auth_status_reports_per_resource_expiry_without_refresh(token_factory, mocker):
    bundle = _bundle(token_factory)
    bundle["graph"] = token_factory(aud="https://graph.microsoft.com", exp=int(time.time()) - 1)
    auth._save_tokens(bundle)
    refresh = mocker.patch.object(auth, "refresh_tokens")
    status = auth.get_auth_status()
    assert status["resources"]["ic3"]["fresh"] is True
    assert status["resources"]["graph"]["fresh"] is False
    assert status["resources"]["graph"]["expires_in_seconds"] < 0
    assert "private-refresh-secret" not in json.dumps(status)
    refresh.assert_not_called()


def test_manual_bundle_rejects_mixed_account_tokens(token_factory, mocker):
    verify = mocker.patch.object(auth, "verify_tokens")
    with pytest.raises(ValueError, match="different account or tenant"):
        auth.login_with_token(json.dumps({"ic3": token_factory(), "graph": token_factory(oid="other-user")}))
    verify.assert_not_called()


def test_login_json_contains_only_auth_metadata(runner, mocker, fake_tokens):
    mocker.patch.object(auth_commands, "do_login", return_value=fake_tokens)
    mocker.patch.object(auth_commands, "verify_tokens", return_value=True)
    result = runner.invoke(cli.cli, ["login", "--json"])
    assert result.exit_code == 0
    output = json.loads(result.output)
    assert output["ok"] is True
    assert output["data"]["authenticated"] is True
    assert fake_tokens["ic3"] not in result.output


@respx.mock
def test_malformed_refresh_access_token_does_not_expose_response(token_factory):
    bundle = _bundle(token_factory, exp=int(time.time()) - 1)
    _save_state(bundle)
    respx.post("https://login.microsoftonline.com/tenant-123/oauth2/v2.0/token").mock(
        return_value=httpx.Response(200, json={"access_token": {"secret": "private-refresh-secret"}}))
    with pytest.raises(AuthRequiredError, match="invalid token refresh response") as error:
        auth.refresh_tokens(bundle)
    assert "private-refresh-secret" not in str(error.value)


@respx.mock
def test_client_does_not_replay_uncertain_write_timeout(teams_client, mocker):
    route = respx.post(teams_client._chatsvc + "/mutation").mock(side_effect=httpx.ReadTimeout("timeout"))
    with pytest.raises(RetryableError):
        teams_client._ic3_post("/mutation", {}, is_write=True)
    assert route.call_count == 1


def test_extraction_does_not_trust_inconsistent_optional_account_metadata(token_factory):
    bundle = _bundle(token_factory)
    state = _state(bundle)
    graph = state["origins"][0]["localStorage"][1]
    value = json.loads(graph["value"])
    value["secret"] = token_factory(aud="https://graph.microsoft.com", oid="different-user")
    graph["value"] = json.dumps(value)
    assert "graph" not in extract_tokens(state)


@respx.mock
@pytest.mark.parametrize("browser_session", [False, True])
def test_explicit_login_cannot_be_clobbered_by_inflight_refresh(token_factory, mocker, browser_session):
    """A holds the refresh lock; B validates/captures, then persists last."""
    from contextlib import contextmanager
    import threading
    from pathlib import Path

    first = _bundle(token_factory, exp=int(time.time()) - 1)
    second = _bundle(token_factory, oid="second-user")
    _save_state(first)
    auth._save_tokens(first)
    refreshed = token_factory(aud="https://ic3.teams.office.com", exp=int(time.time()) + 7200)
    grant_started = threading.Event()
    release_grant = threading.Event()
    login_lock_requested = threading.Event()
    original_lock = auth._locked_auth_cache

    @contextmanager
    def observed_lock():
        if threading.current_thread().name == "explicit-login":
            login_lock_requested.set()
        with original_lock():
            yield

    mocker.patch.object(auth, "_locked_auth_cache", side_effect=observed_lock)

    def grant_response(request):
        grant_started.set()
        assert release_grant.wait(5), "test did not release the grant"
        return httpx.Response(200, json={"access_token": refreshed, "refresh_token": "refreshed-private-secret"})

    route = respx.post("https://login.microsoftonline.com/tenant-123/oauth2/v2.0/token").mock(side_effect=grant_response)
    mocker.patch.object(auth, "verify_tokens", return_value=True)
    saved = {}

    class CapturedBrowser:
        def storage_state(self, path):
            # Both pieces of the explicit browser session must share one lock.
            saved["context_written"] = True
            Path(path).write_text(json.dumps(_state(second)))

    def persist_second():
        threading.current_thread().name = "explicit-login"
        if browser_session:
            auth._persist_login_tokens(second, context=CapturedBrowser())
            return second
        return auth.login_with_token(json.dumps(second))

    with ThreadPoolExecutor(max_workers=2) as pool:
        refresh_future = pool.submit(auth.refresh_tokens, first)
        try:
            assert grant_started.wait(5)
            login_future = pool.submit(persist_second)
            assert login_lock_requested.wait(5)
            # Explicit login cannot replace the active bundle while A's grant
            # and resulting session rotation are still in flight.
            assert claims(json.loads(auth.TOKENS_FILE.read_text())["ic3"])["oid"] == "user-123"
            assert not saved
        finally:
            release_grant.set()
        assert refresh_future.result(timeout=5)["ic3"] == refreshed
        assert login_future.result(timeout=5)["ic3"] == second["ic3"]
    assert route.call_count == 1
    active = json.loads(auth.TOKENS_FILE.read_text())
    assert claims(active["ic3"])["oid"] == "second-user"
    if browser_session:
        state = json.loads(auth.BROWSER_STATE_FILE.read_text())
        assert extract_tokens(state, active)["ic3"] == second["ic3"]


@respx.mock
def test_refresh_rechecks_account_after_grant_before_persisting(token_factory):
    """An older/uncooperative writer cannot roll back a completed account switch."""
    first = _bundle(token_factory, exp=int(time.time()) - 1)
    second = _bundle(token_factory, oid="second-user")
    _save_state(first)
    auth._save_tokens(first)

    def grant_after_external_switch(request):
        _save_state(second)
        auth._save_tokens(second)
        return httpx.Response(200, json={"access_token": token_factory(aud="https://ic3.teams.office.com")})

    route = respx.post("https://login.microsoftonline.com/tenant-123/oauth2/v2.0/token").mock(side_effect=grant_after_external_switch)
    with pytest.raises(AuthRequiredError, match="account changed during refresh"):
        auth.refresh_tokens(first)
    active = json.loads(auth.TOKENS_FILE.read_text())
    assert claims(active["ic3"])["oid"] == "second-user"
    state = json.loads(auth.BROWSER_STATE_FILE.read_text())
    assert extract_tokens(state, active)["ic3"] == second["ic3"]
    assert route.call_count == 1


def test_explicit_login_persistence_rejects_mixed_account_secondary_tokens(token_factory, mocker):
    tokens = _bundle(token_factory)
    tokens["graph"] = token_factory(aud="https://graph.microsoft.com", oid="second-user")
    save_browser = mocker.patch.object(auth, "_save_browser_state")
    with pytest.raises(AuthRequiredError, match="different accounts"):
        auth._persist_login_tokens(tokens, context=object())
    save_browser.assert_not_called()
    assert not auth.TOKENS_FILE.exists()
