from __future__ import annotations

import json
import os
import stat
import sys
import time
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .exceptions import AuthRequiredError, RetryableError
from .msal_cache import (
    EXPIRY_BUFFER, audience, claims, credentials, extract_tokens, newest_access,
    number, refresh_scopes, select_account,
)

from .constants import (
    BROWSER_STATE_FILE,
    CACHE_DIR,
    CHATSVC_BASE,
    TEAMS_CLIENT_ID,
    TEAMS_URL,
    TOKENS_FILE,
    USER_AGENT,
    USER_PROFILE_FILE,
)

TOKEN_KEYS = ("ic3", "graph", "presence", "csa", "substrate")
_refresh_lock = threading.RLock()


def get_tokens(required: tuple[str, ...] = ("ic3",)) -> dict[str, str]:
    """Use valid cache or bounded silent refresh; never open a browser."""
    # 1. Environment variable (IC3 token only)
    env_token = os.environ.get("TEAMS_IC3_TOKEN")
    if env_token:
        if _decode_exp(env_token) <= time.time() + EXPIRY_BUFFER:
            raise AuthRequiredError("TEAMS_IC3_TOKEN is expired or has no usable expiry. Replace it or unset it and run: teams login")
        region = os.environ.get("TEAMS_REGION", "emea")
        user_id = _decode_user_id(env_token)
        return {
            "ic3": env_token,
            "region": region,
            "user_id": user_id,
        }

    # 2. Cached tokens
    cached = _load_cached_tokens()
    if cached and all(token_is_fresh(cached, key) for key in required):
        return cached
    return refresh_tokens(cached or _coerce_token_bundle(_load_cached_tokens_raw() or {}), required=required)


def token_is_fresh(tokens: dict, key: str, buffer: float = EXPIRY_BUFFER) -> bool:
    token = tokens.get(key, "")
    if not isinstance(token, str) or not token:
        return False
    bounds = [_decode_exp(token), number(tokens.get(key + "_exp"))]
    known = [value for value in bounds if value > 0]
    return bool(known) and min(known) > time.time() + buffer


def refresh_tokens(tokens: dict[str, str], required: tuple[str, ...] = ("ic3",), force: bool = False) -> dict[str, str]:
    """Refresh only requested resources from the same saved MSAL account.

    Each resource gets one refresh grant, with a 20-second total deadline. This
    does not execute Teams or change chat read state. Conditional Access/MFA is
    reported once with an explicit login instruction; there is no retry loop.
    """
    if any(key not in TOKEN_KEYS for key in required):
        raise ValueError("Unknown token resource requested.")
    if os.environ.get("TEAMS_IC3_TOKEN"):
        raise AuthRequiredError("Cannot silently refresh TEAMS_IC3_TOKEN. Replace it or unset it and run: teams login")
    if not force and all(token_is_fresh(tokens, key) for key in required):
        return tokens
    import httpx

    with _locked_auth_cache():
        cached = _load_cached_tokens_raw() or {}
        if cached and not _same_identity(tokens, cached):
            raise AuthRequiredError("Teams account changed during refresh. Run the command again for the active account.")
        # Another process may have already refreshed the rejected token.
        changed = any(cached.get(key) != tokens.get(key) for key in required)
        if cached and all(token_is_fresh(cached, key) for key in required) and (not force or changed):
            return _coerce_token_bundle(cached)
        state = _load_browser_state()
        records = credentials(state)
        expected = tokens if tokens.get("ic3") else cached
        account = select_account(records, expected)
        selected = newest_access(records, account)
        result = dict(tokens)
        result.update(extract_tokens(state, expected, include_expired=True))
        for key, record in selected.items():
            result[key + "_exp"] = str(record.expires)
        if tokens.get("region"):
            result["region"] = tokens["region"]
        for key in TOKEN_KEYS:
            # A newer cache token must not be replaced by older saved state.
            if token_is_fresh(tokens, key) and _decode_exp(tokens[key]) > _decode_exp(result.get(key, "")):
                result[key] = tokens[key]
                result[key + "_exp"] = str(_decode_exp(tokens[key]))
        wanted = [key for key in required if force or not token_is_fresh(result, key)]
        if not wanted:
            _save_tokens(result)
            return result
        refresh_records = [record for record in records if record.kind == "refreshtoken"
                           and record.account[0] == account[0]
                           and record.account[1] in ("", account[1])
                           and record.value.get("clientId", TEAMS_CLIENT_ID) == TEAMS_CLIENT_ID]
        if not refresh_records:
            raise AuthRequiredError("No saved Teams refresh token is available. Run: teams login")
        refresh_record = max(refresh_records, key=lambda record: number(record.value.get("cachedAt")))
        realm = account[1]
        if not realm or not all(char.isalnum() or char in "-_" for char in realm):
            raise AuthRequiredError("Saved Teams session has no usable tenant. Run: teams login")
        deadline = time.monotonic() + 20
        with httpx.Client(timeout=10) as client:
            for key in wanted:
                record = selected.get(key)
                if record is None:
                    raise AuthRequiredError(f"No saved Teams scope for {key}. Run: teams login")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RetryableError("Silent Teams token refresh timed out. Try the command again.")
                try:
                    response = client.post(
                        f"https://login.microsoftonline.com/{realm}/oauth2/v2.0/token",
                        data={"grant_type": "refresh_token", "client_id": TEAMS_CLIENT_ID,
                              "refresh_token": refresh_record.token, "scope": refresh_scopes(record)},
                        headers={"Origin": record.origin, "User-Agent": USER_AGENT},
                        timeout=min(10, remaining),
                    )
                    payload = response.json()
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    raise RetryableError("Silent Teams token refresh could not reach Microsoft. Try the command again.") from exc
                except (ValueError, TypeError) as exc:
                    raise AuthRequiredError("Microsoft returned an unreadable token refresh response. Run: teams login") from exc
                if response.status_code == 429 or response.status_code >= 500:
                    raise RetryableError("Microsoft token refresh is temporarily unavailable. Try the command again.")
                if response.status_code != 200 or not isinstance(payload, dict) or not payload.get("access_token"):
                    # OAuth descriptions can contain account details; never echo them.
                    raise AuthRequiredError("Silent Teams refresh requires sign-in or MFA. Run: teams login")
                access_token = payload["access_token"]
                if not isinstance(access_token, str):
                    raise AuthRequiredError("Microsoft returned an invalid token refresh response. Run: teams login")
                refreshed_claims = claims(access_token)
                expected_claims = claims(result.get("ic3", ""))
                if (audience(access_token) != key
                        or refreshed_claims.get("oid") != expected_claims.get("oid")
                        or refreshed_claims.get("tid") != expected_claims.get("tid")):
                    raise AuthRequiredError("Microsoft returned a token for a different Teams account or resource. Run: teams login --force")
                result[key] = access_token
                result[key + "_exp"] = str(_decode_exp(access_token))
                record.value.update({"secret": access_token, "expiresOn": str(_decode_exp(access_token)),
                                     "cachedAt": str(int(time.time()))})
                record.entry["value"] = json.dumps(record.value)
                if payload.get("refresh_token"):
                    refresh_record.value["secret"] = payload["refresh_token"]
                    refresh_record.entry["value"] = json.dumps(refresh_record.value)
                # Preserve successful progress even if a later audience needs MFA.
                _atomic_json_write(BROWSER_STATE_FILE, state)
                _save_tokens(result)
        return result


def _same_identity(left: dict, right: dict) -> bool:
    if not left.get("ic3"):
        return True
    first, second = claims(left.get("ic3", "")), claims(right.get("ic3", ""))
    return (first.get("oid"), first.get("tid")) == (second.get("oid"), second.get("tid"))


def _load_browser_state() -> dict:
    try:
        state = json.loads(BROWSER_STATE_FILE.read_text())
        if isinstance(state, dict):
            _chmod_600(BROWSER_STATE_FILE)
            return state
    except (ValueError, OSError):
        pass
    raise AuthRequiredError("No usable saved Teams browser session. Run: teams login")


@contextmanager
def _locked_auth_cache():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with _refresh_lock:
        lock_path = CACHE_DIR / "auth.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            try:
                import fcntl
            except ImportError:  # Windows still has the in-process lock.
                fcntl = None
            if fcntl:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            if 'fcntl' in locals() and fcntl:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def login(force: bool = False, debug: bool = False) -> dict[str, str]:
    """Launch Playwright browser to extract MSAL tokens from Teams localStorage."""
    from playwright.sync_api import sync_playwright

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        launch_args: dict = {}
        if BROWSER_STATE_FILE.exists() and not force:
            launch_args["storage_state"] = str(BROWSER_STATE_FILE)

        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent=USER_AGENT,
            **launch_args,
        )
        page = context.new_page()

        _print_stderr("Opening Teams... Log in and wait for the app to fully load.")
        _print_stderr("The browser will close automatically once tokens are captured.")
        page.goto(TEAMS_URL, wait_until="domcontentloaded")

        # Poll until Teams fully settles so secondary tokens are also captured.
        tokens: dict[str, str] = {}
        deadline = time.time() + 120
        grace_deadline: float | None = None
        while time.time() < deadline:
            try:
                page.wait_for_timeout(3000)
            except Exception:
                break

            try:
                current = _extract_tokens_from_page(page, debug=debug)
                for key, value in current.items():
                    if value:
                        tokens[key] = value
            except Exception as e:
                if debug:
                    _print_stderr(f"  [debug] Token extraction unavailable: {type(e).__name__}")

            if tokens.get("ic3"):
                if grace_deadline is None:
                    grace_deadline = time.time() + 15
                if all(tokens.get(name) for name in ("graph", "substrate", "presence", "csa")):
                    break
                if time.time() >= grace_deadline:
                    break

        # Save browser state for future SSO
        try:
            _save_browser_state(context)
        except Exception:
            pass

        try:
            browser.close()
        except Exception:
            pass

    if not tokens.get("ic3"):
        raise RuntimeError(
            "Could not capture IC3 token from Teams.\n"
            "Make sure you logged in and Teams fully loaded.\n"
            "Tip: Try 'teams login --debug' to see extraction details."
        )

    _save_tokens(tokens)
    return tokens


def login_with_token(raw_input: str, region: str = "emea") -> dict[str, str]:
    """Validate and cache tokens provided via stdin.

    Args:
        raw_input: Either a plain IC3 token string or a JSON object with token keys.
        region: Region override (emea/amer/apac). Used when input is a plain token.

    Returns:
        Tokens dict with keys: ic3, graph, presence, csa, substrate, region, user_id

    Raises:
        ValueError: If input is empty, JWT format is invalid, or JSON is malformed.
        RuntimeError: If IC3 token fails live validation.
    """
    raw_input = raw_input.strip()
    if not raw_input:
        raise ValueError("No token provided via stdin.")

    # Detect format: JSON bundle or plain token
    parsed = None
    try:
        candidate = json.loads(raw_input)
        if isinstance(candidate, dict):
            parsed = candidate
    except (json.JSONDecodeError, ValueError):
        pass

    if parsed is not None:
        # JSON bundle path
        ic3 = parsed.get("ic3", "")
        if not ic3:
            raise ValueError("JSON input must include an 'ic3' token.")

        parts = ic3.split(".")
        if len(parts) != 3:
            raise ValueError("Invalid token format for 'ic3'. Expected JWT with 3 parts (header.payload.signature).")

        # Validate optional tokens
        optional_keys = ("graph", "presence", "csa", "substrate")
        for key in optional_keys:
            val = parsed.get(key, "")
            if val and len(val.split(".")) != 3:
                raise ValueError(f"Invalid JWT format for '{key}' token.")
            if val and not _same_identity({"ic3": ic3}, {"ic3": val}):
                raise ValueError(f"The '{key}' token belongs to a different account or tenant.")

        tokens = {
            "ic3": ic3,
            "graph": parsed.get("graph", ""),
            "presence": parsed.get("presence", ""),
            "csa": parsed.get("csa", ""),
            "substrate": parsed.get("substrate", ""),
            "region": parsed.get("region", region),
            "user_id": _decode_user_id(ic3),
        }
    else:
        # Plain token path
        parts = raw_input.split(".")
        if len(parts) != 3:
            raise ValueError("Invalid token format. Expected JWT with 3 parts (header.payload.signature).")

        tokens = {
            "ic3": raw_input,
            "graph": "",
            "presence": "",
            "csa": "",
            "substrate": "",
            "region": region,
            "user_id": _decode_user_id(raw_input),
        }

    # Live validation
    if not verify_tokens(tokens):
        raise RuntimeError("Token validation failed. The IC3 token may be expired or invalid.")

    _save_tokens(tokens)
    return tokens


def _extract_tokens_from_page(page, debug: bool = False) -> dict[str, str]:
    """Parse modern and legacy MSAL records with the same offline selector."""
    snapshot = page.evaluate("""() => ({origins: [{origin: location.origin,
        localStorage: Object.keys(localStorage).map(name => ({name, value: localStorage.getItem(name)}))
    }]})""")
    result = extract_tokens(snapshot)
    if debug:
        for key in TOKEN_KEYS:
            _print_stderr(f"  [debug] {key}: {'available' if result.get(key) else 'missing or expired'}")
    return result


def verify_tokens(tokens: dict[str, str]) -> bool:
    """Check if IC3 token is valid by calling /users/ME/properties."""
    import httpx

    ic3 = tokens.get("ic3")
    if not ic3:
        return False

    region = tokens.get("region", "emea")
    base = CHATSVC_BASE.format(region=region)

    try:
        resp = httpx.get(
            f"{base}/users/ME/properties",
            headers={
                "Authorization": f"Bearer {ic3}",
                "User-Agent": USER_AGENT,
            },
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False


def get_auth_status(check: bool = False) -> dict[str, object]:
    """Inspect the current auth state without triggering interactive login."""
    source = "missing"
    active_tokens: dict[str, str] | None = None
    raw_cache = _load_cached_tokens_raw()

    env_token = os.environ.get("TEAMS_IC3_TOKEN")
    if env_token:
        source = "env"
        active_tokens = {
            "ic3": env_token,
            "graph": "",
            "presence": "",
            "csa": "",
            "substrate": "",
            "region": os.environ.get("TEAMS_REGION", "emea"),
            "user_id": _decode_user_id(env_token),
        }
    elif raw_cache and raw_cache.get("ic3"):
        source = "cache"
        cached_tokens = _load_cached_tokens()
        if cached_tokens:
            active_tokens = cached_tokens

    token_snapshot = active_tokens or raw_cache or {}
    ic3 = token_snapshot.get("ic3", "")
    display_name = _decode_display_name(ic3) if ic3 else ""
    if not display_name and USER_PROFILE_FILE.exists():
        try:
            display_name = json.loads(USER_PROFILE_FILE.read_text()).get("display_name", "")
        except (json.JSONDecodeError, OSError, AttributeError):
            display_name = ""

    exp = token_snapshot.get("ic3_exp") or (_decode_exp(ic3) if ic3 else 0)
    expires_at = None
    expires_in_seconds = None
    expires_in_human = None
    if exp:
        expires_at = datetime.fromtimestamp(float(exp), tz=timezone.utc).isoformat()
        expires_in_seconds = int(float(exp) - time.time())
        expires_in_human = _format_expires_in(expires_in_seconds)

    ic3_valid = None
    if check and token_snapshot.get("ic3"):
        ic3_valid = verify_tokens(_coerce_token_bundle(token_snapshot))

    resource_status = {}
    for key in TOKEN_KEYS:
        token = token_snapshot.get(key, "")
        expiry = number(token_snapshot.get(key + "_exp")) or _decode_exp(token)
        remaining = int(expiry - time.time()) if expiry else None
        resource_status[key] = {
            "present": bool(token), "fresh": token_is_fresh(token_snapshot, key),
            "expires_at": datetime.fromtimestamp(expiry, tz=timezone.utc).isoformat() if expiry else None,
            "expires_in_seconds": remaining,
        }

    return {
        "auth_source": source,
        "cache": {
            "token_cache_exists": TOKENS_FILE.exists(),
            "token_cache_path": str(TOKENS_FILE),
            "browser_state_exists": BROWSER_STATE_FILE.exists(),
            "browser_state_path": str(BROWSER_STATE_FILE),
        },
        "identity": {
            "region": token_snapshot.get("region", os.environ.get("TEAMS_REGION", "emea")),
            "user_id": token_snapshot.get("user_id", _decode_user_id(ic3) if ic3 else ""),
            "display_name": display_name,
        },
        "tokens": {key: bool(token_snapshot.get(key, "")) for key in TOKEN_KEYS},
        "resources": resource_status,
        "ic3": {
            "expires_at": expires_at,
            "expires_in_seconds": expires_in_seconds,
            "expires_in_human": expires_in_human,
            "valid": ic3_valid,
        },
    }


def _decode_user_id(token: str) -> str:
    """Extract oid from a JWT without treating its contents as trusted data."""
    value = claims(token).get("oid", "")
    return value if isinstance(value, str) else ""


def _decode_exp(token: str) -> float:
    """Unknown expiry fails closed instead of assuming an extra hour."""
    return number(claims(token).get("exp"))


def _decode_display_name(token: str) -> str:
    value = claims(token).get("name", "")
    return value if isinstance(value, str) else ""


def _load_cached_tokens() -> dict[str, str] | None:
    data = _load_cached_tokens_raw()
    if not data or not token_is_fresh(data, "ic3"):
        return None
    return _coerce_token_bundle(data)


def _load_cached_tokens_raw() -> dict[str, object] | None:
    if not TOKENS_FILE.exists():
        return None
    try:
        data = json.loads(TOKENS_FILE.read_text())
        if isinstance(data, dict):
            _chmod_600(TOKENS_FILE)
            return data
        return None
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def _save_tokens(tokens: dict[str, str]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ic3 = tokens.get("ic3", "")
    data = {
        "ic3": ic3,
        "ic3_exp": _decode_exp(ic3) if ic3 else 0,
        "graph": tokens.get("graph", ""),
        "presence": tokens.get("presence", ""),
        "csa": tokens.get("csa", ""),
        "substrate": tokens.get("substrate", ""),
        "region": tokens.get("region", "emea"),
        "user_id": tokens.get("user_id", ""),
    }
    for key in TOKEN_KEYS:
        data[key + "_exp"] = _decode_exp(tokens.get(key, "")) if tokens.get(key) else 0
    for key in ("tenant_id", "home_account_id"):
        if tokens.get(key):
            data[key] = tokens[key]
    _atomic_json_write(TOKENS_FILE, data)

    # Cache user profile
    if ic3:
        name = _decode_display_name(ic3)
        if name:
            profile = {"display_name": name, "user_id": tokens.get("user_id", "")}
            _atomic_json_write(USER_PROFILE_FILE, profile)


def _chmod_600(path: Path) -> None:
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _format_expires_in(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    if seconds < 0:
        seconds = abs(seconds)
        suffix = "ago"
    else:
        suffix = ""

    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    rendered = " ".join(parts)
    return f"{rendered} {suffix}".strip()


def _print_stderr(message: str) -> None:
    print(message, file=sys.stderr)


def _coerce_token_bundle(data: dict[str, object]) -> dict[str, str]:
    result = {key: str(data.get(key, "") or "") for key in TOKEN_KEYS}
    result.update({"region": str(data.get("region", "emea") or "emea"),
                   "user_id": str(data.get("user_id") or _decode_user_id(result["ic3"]))})
    for key in ("tenant_id", "home_account_id", *(name + "_exp" for name in TOKEN_KEYS)):
        if data.get(key):
            result[key] = str(data[key])
    return result


def _atomic_json_write(path: Path, data: dict) -> None:
    """Create private files before writing secrets, then atomically replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _save_browser_state(context) -> None:
    descriptor, temp_name = tempfile.mkstemp(prefix=".browser-state.", dir=BROWSER_STATE_FILE.parent)
    os.close(descriptor)
    try:
        context.storage_state(path=temp_name)
        _chmod_600(Path(temp_name))
        os.replace(temp_name, BROWSER_STATE_FILE)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
