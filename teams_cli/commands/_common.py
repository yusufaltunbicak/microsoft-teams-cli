"""Shared helpers for CLI commands."""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone

import click

from ..auth import get_tokens, login as do_login, _decode_exp
from ..client import TeamsClient
from ..config import load_config
from ..exceptions import TokenExpiredError
from ..formatter import console, print_error, print_success
from ..serialization import is_piped

cfg = load_config()

_client_cache: dict[str, TeamsClient] = {}


def _check_token_expiry(tokens: dict[str, str]) -> dict[str, str]:
    """Check if IC3 token is expired and re-login if needed."""
    import time

    ic3 = tokens.get("ic3", "")
    if not ic3:
        return tokens

    exp = _decode_exp(ic3)
    # 60-second buffer — proactively refresh before actual expiry
    if time.time() > exp - 60:
        print_error("Token expiring soon. Re-authenticating...")
        try:
            tokens = do_login()
            print_success("Re-login successful.")
        except Exception:
            print_error("Auto re-login failed. Run: teams login --force")
            sys.exit(1)
    return tokens


def _get_client() -> TeamsClient:
    if "c" not in _client_cache:
        try:
            tokens = get_tokens()
        except RuntimeError as e:
            print_error(str(e))
            sys.exit(1)
        tokens = _check_token_expiry(tokens)
        _client_cache["c"] = TeamsClient(tokens)
    return _client_cache["c"]


def _handle_api_error(fn):
    """Decorator to catch API errors. Auto re-login on 401."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except TokenExpiredError:
            print_error("Token expired. Attempting re-login...")
            try:
                do_login()
                print_success("Re-login successful. Retrying...")
                _client_cache.clear()
                return fn(*args, **kwargs)
            except Exception:
                print_error("Auto re-login failed. Run: teams login --force")
                sys.exit(1)
        except ValueError as e:
            print_error(str(e))
            sys.exit(1)
        except Exception as e:
            print_error(f"Error: {e}")
            sys.exit(1)

    return wrapper


def _parse_schedule_time(s: str) -> datetime:
    """Parse schedule time: +30m, +1h, tomorrow 09:00, 2024-03-15T10:00."""
    now = datetime.now(timezone.utc)

    # Relative offset: +30m, +1h, +2h30m
    offset_match = re.match(r'^\+(?:(\d+)h)?(?:(\d+)m)?$', s)
    if offset_match:
        hours = int(offset_match.group(1) or 0)
        minutes = int(offset_match.group(2) or 0)
        if hours == 0 and minutes == 0:
            raise click.BadParameter(f"Invalid offset: {s}")
        return now + timedelta(hours=hours, minutes=minutes)

    # today/tomorrow HH:MM
    day_match = re.match(r'^(today|tomorrow)\s+(\d{1,2}:\d{2})$', s, re.IGNORECASE)
    if day_match:
        day_word, time_str = day_match.groups()
        local_now = datetime.now().astimezone()
        h, m = map(int, time_str.split(":"))
        target = local_now.replace(hour=h, minute=m, second=0, microsecond=0)
        if day_word.lower() == "tomorrow":
            target += timedelta(days=1)
        return target.astimezone(timezone.utc)

    # ISO-like
    try:
        s = s.replace(" ", "T", 1) if " " in s and "T" not in s else s
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass

    raise click.BadParameter(
        f"Cannot parse '{s}'. Use: +30m, +1h, tomorrow 09:00, or 2024-03-15T10:00"
    )


def _format_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.0f}{unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f}TB"


VALID_REACTIONS = {"like", "heart", "laugh", "surprised", "sad", "angry"}


def should_json(as_json: bool) -> bool:
    """Return True if output should be JSON (explicit flag or piped stdout)."""
    return as_json or is_piped()
