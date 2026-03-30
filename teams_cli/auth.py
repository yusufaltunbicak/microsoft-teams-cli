from __future__ import annotations

import json
import os
import stat
import sys
import time
from datetime import datetime, timezone
from base64 import urlsafe_b64decode
from pathlib import Path

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


def get_tokens() -> dict[str, str]:
    """Return valid tokens dict, from env, cache, or interactive login.

    Returns dict with keys: ic3, graph, presence, csa, region, user_id
    """
    # 1. Environment variable (IC3 token only)
    env_token = os.environ.get("TEAMS_IC3_TOKEN")
    if env_token:
        region = os.environ.get("TEAMS_REGION", "emea")
        user_id = _decode_user_id(env_token)
        return {
            "ic3": env_token,
            "region": region,
            "user_id": user_id,
        }

    # 2. Cached tokens
    cached = _load_cached_tokens()
    if cached:
        return cached

    # 3. Interactive login
    return login()


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
                    _print_stderr(f"  [debug] Token extraction error: {e}")

            if tokens.get("ic3"):
                if grace_deadline is None:
                    grace_deadline = time.time() + 15
                if all(tokens.get(name) for name in ("graph", "substrate", "presence", "csa")):
                    break
                if time.time() >= grace_deadline:
                    break

        # Save browser state for future SSO
        try:
            context.storage_state(path=str(BROWSER_STATE_FILE))
            _chmod_600(BROWSER_STATE_FILE)
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
    """Extract MSAL tokens from Teams localStorage via JS evaluation."""
    result = page.evaluate("""() => {
        const tokens = {};
        const region_data = {};

        for (let i = 0; i < localStorage.length; i++) {
            const key = localStorage.key(i);
            const val = localStorage.getItem(key);

            // MSAL access tokens
            if (key.includes('-accesstoken-')) {
                try {
                    const obj = JSON.parse(val);
                    const secret = obj.secret || '';
                    const target = (obj.target || '').toLowerCase();
                    const env = obj.environment || '';

                    if (secret.length > 100) {
                        if (target.includes('ic3.teams.office.com') || key.includes('ic3.teams.office.com')) {
                            tokens['ic3'] = secret;
                        } else if (target.includes('graph.microsoft.com') || key.includes('graph.microsoft.com')) {
                            tokens['graph'] = secret;
                        } else if (target.includes('presence.teams.microsoft') || key.includes('presence.teams.microsoft')) {
                            tokens['presence'] = secret;
                        } else if (target.includes('chatsvcagg.teams.microsoft.com') || key.includes('chatsvcagg.teams.microsoft.com')) {
                            tokens['csa'] = secret;
                        } else if (target.includes('substrate.office.com') || key.includes('substrate.office.com')) {
                            tokens['substrate'] = secret;
                        }
                    }
                } catch(e) {}
            }

            // Region discovery
            if (key.includes('DISCOVER-REGION-GTM') || key.includes('Discover.DISCOVER-REGION-GTM')) {
                try {
                    const obj = JSON.parse(val);
                    if (obj.regionGtms) {
                        // regionGtms is a JSON string itself
                        const gtms = typeof obj.regionGtms === 'string' ? JSON.parse(obj.regionGtms) : obj.regionGtms;
                        if (gtms.chatService) {
                            // Extract region from chatService URL
                            const match = gtms.chatService.match(/chatsvc\\/([a-z]+)/);
                            if (match) tokens['region'] = match[1];
                        }
                    }
                    // Also check for direct region field
                    if (obj.region) tokens['region'] = obj.region;
                } catch(e) {
                    // Try plain value
                    try {
                        const obj2 = JSON.parse(val);
                        if (typeof obj2 === 'object') {
                            const chatSvc = obj2.chatService || '';
                            const match = chatSvc.match(/chatsvc\\/([a-z]+)/);
                            if (match) tokens['region'] = match[1];
                        }
                    } catch(e2) {}
                }
            }
        }

        return tokens;
    }""")

    if debug:
        for k, v in result.items():
            if k in ("ic3", "graph", "presence", "csa", "substrate"):
                _print_stderr(f"  [debug] {k} token: {len(v)} chars")
            else:
                _print_stderr(f"  [debug] {k}: {v}")

    # Extract user_id from IC3 token
    if result.get("ic3"):
        result["user_id"] = _decode_user_id(result["ic3"])
        if debug:
            _print_stderr(f"  [debug] user_id: {result.get('user_id', 'unknown')}")

    # Default region
    if "region" not in result:
        result["region"] = "emea"

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
        "ic3": {
            "expires_at": expires_at,
            "expires_in_seconds": expires_in_seconds,
            "expires_in_human": expires_in_human,
            "valid": ic3_valid,
        },
    }


def _decode_user_id(token: str) -> str:
    """Extract oid (object ID) from JWT claims."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return ""
        payload = parts[1]
        payload += "=" * (4 - len(payload) % 4)
        decoded = json.loads(urlsafe_b64decode(payload))
        return decoded.get("oid", "")
    except (ValueError, KeyError, IndexError, json.JSONDecodeError):
        return ""


def _decode_exp(token: str) -> float:
    """Extract exp claim from JWT."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return time.time() + 3600
        payload = parts[1]
        payload += "=" * (4 - len(payload) % 4)
        decoded = json.loads(urlsafe_b64decode(payload))
        return float(decoded.get("exp", time.time() + 3600))
    except (ValueError, KeyError, IndexError, json.JSONDecodeError):
        return time.time() + 3600


def _decode_display_name(token: str) -> str:
    """Extract name from JWT claims."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return ""
        payload = parts[1]
        payload += "=" * (4 - len(payload) % 4)
        decoded = json.loads(urlsafe_b64decode(payload))
        return decoded.get("name", "")
    except (ValueError, KeyError, IndexError, json.JSONDecodeError):
        return ""


def _load_cached_tokens() -> dict[str, str] | None:
    data = _load_cached_tokens_raw()
    if not data:
        return None

    ic3 = data.get("ic3")
    if not ic3:
        return None

    exp = data.get("ic3_exp", 0)
    # Check expiry with 5-minute buffer
    if time.time() > exp - 300:
        return None
    return {
        "ic3": ic3,
        "graph": data.get("graph", ""),
        "presence": data.get("presence", ""),
        "csa": data.get("csa", ""),
        "substrate": data.get("substrate", ""),
        "region": data.get("region", "emea"),
        "user_id": data.get("user_id", ""),
    }


def _load_cached_tokens_raw() -> dict[str, object] | None:
    if not TOKENS_FILE.exists():
        return None
    try:
        return json.loads(TOKENS_FILE.read_text())
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
    TOKENS_FILE.write_text(json.dumps(data))
    _chmod_600(TOKENS_FILE)

    # Cache user profile
    if ic3:
        name = _decode_display_name(ic3)
        if name:
            profile = {"display_name": name, "user_id": tokens.get("user_id", "")}
            USER_PROFILE_FILE.write_text(json.dumps(profile))


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
    return {
        "ic3": str(data.get("ic3", "") or ""),
        "graph": str(data.get("graph", "") or ""),
        "presence": str(data.get("presence", "") or ""),
        "csa": str(data.get("csa", "") or ""),
        "substrate": str(data.get("substrate", "") or ""),
        "region": str(data.get("region", "emea") or "emea"),
        "user_id": str(data.get("user_id", "") or ""),
    }
