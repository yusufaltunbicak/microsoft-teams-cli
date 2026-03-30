"""Shared helpers for CLI commands."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import sys
from datetime import datetime, timedelta, timezone

import click
import httpx
import yaml

from ..auth import get_tokens, login as do_login, _decode_exp
from ..client import TeamsClient
from ..config import load_config
from ..exceptions import (
    ApiError,
    AuthRequiredError,
    ConfigurationError,
    RateLimitError,
    ResourceNotFoundError,
    RetryableError,
    TokenExpiredError,
)
from ..formatter import console, print_error, print_success
from ..serialization import is_piped, to_json

_client_cache: dict[str, TeamsClient] = {}


@dataclass(frozen=True)
class RuntimeOptions:
    dry_run: bool = False
    no_input: bool = False
    force: bool = False


class _ConfigProxy(dict):
    """Load config lazily so CLI wrappers can classify config errors."""

    def __init__(self) -> None:
        super().__init__()
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self.clear()
        self.update(load_config())
        self._loaded = True

    def __getitem__(self, key):
        self._ensure_loaded()
        return super().__getitem__(key)

    def get(self, key, default=None):
        self._ensure_loaded()
        return super().get(key, default)


cfg = _ConfigProxy()


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
        except Exception as exc:
            raise AuthRequiredError("Auto re-login failed. Run: teams login --force") from exc
    return tokens


def _get_client() -> TeamsClient:
    if "c" not in _client_cache:
        try:
            tokens = get_tokens()
        except RuntimeError as exc:
            raise AuthRequiredError(str(exc)) from exc
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
        except click.ClickException:
            raise
        except TokenExpiredError:
            try:
                do_login()
                _client_cache.clear()
                return fn(*args, **kwargs)
            except Exception as exc:
                raise AuthRequiredError("Auto re-login failed. Run: teams login --force") from exc
        except RateLimitError:
            raise
        except ResourceNotFoundError:
            raise
        except (AuthRequiredError, RetryableError, ConfigurationError):
            raise
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            raise ApiError(str(exc), status_code=status_code) from exc
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise RetryableError(str(exc)) from exc
        except (json.JSONDecodeError, OSError, yaml.YAMLError) as exc:
            raise ConfigurationError(str(exc)) from exc
        except ValueError as exc:
            classified = _classify_value_error(exc)
            if classified is not exc:
                raise classified from exc
            raise
        except Exception:
            raise

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


def get_runtime_options() -> RuntimeOptions:
    """Return the root runtime flags for the current CLI invocation."""
    ctx = click.get_current_context(silent=True)
    if ctx is None:
        return RuntimeOptions()

    # Avoid importing teams_cli.cli at module import time to prevent circular imports.
    from ..cli import RuntimeOptions as RootRuntimeOptions

    opts = ctx.find_object(RootRuntimeOptions)
    if opts is None:
        return RuntimeOptions()
    return RuntimeOptions(
        dry_run=opts.dry_run,
        no_input=opts.no_input,
        force=opts.force,
    )


def should_skip_confirmation(local_force: bool = False) -> bool:
    """Return True when confirmation prompts should be bypassed."""
    opts = get_runtime_options()
    return local_force or opts.force


def ensure_interactive_allowed(action: str, local_force: bool = False) -> None:
    """Fail fast when a command would need interactive input under --no-input."""
    if should_skip_confirmation(local_force):
        return

    if get_runtime_options().no_input:
        raise click.UsageError(
            f"Refusing to {action} without confirmation (--no-input set; use --force or --yes)."
        )


def require_confirmation(prompt: str, action: str, local_force: bool = False) -> None:
    """Confirm a mutation, or refuse if --no-input was requested."""
    ensure_interactive_allowed(action, local_force=local_force)

    click.confirm(prompt, abort=True)


def emit_dry_run(op: str, request: dict, as_json: bool = False) -> bool:
    """Emit a dry-run summary and stop command execution when --dry-run is enabled."""
    if not get_runtime_options().dry_run:
        return False

    payload = {
        "dry_run": True,
        "op": op,
        "request": request,
    }
    if should_json(as_json):
        click.echo(to_json(payload))
    else:
        console.print(f"[yellow]Dry run:[/yellow] would {op}")
        for key, value in request.items():
            if value in (None, "", [], {}):
                continue
            if isinstance(value, list):
                rendered = ", ".join(str(item) for item in value)
            else:
                rendered = str(value)
            console.print(f"  [bold]{key}:[/bold] {rendered}")
    return True


def _classify_value_error(exc: ValueError):
    message = str(exc)
    if (
        message.startswith("Unknown chat #")
        or message.startswith("Unknown message #")
        or message.startswith("Message #")
        or message.startswith("File not found:")
    ):
        return ResourceNotFoundError(message)
    return exc
