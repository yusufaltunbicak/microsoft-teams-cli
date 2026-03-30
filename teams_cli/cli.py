"""Thin CLI entry point — commands live in teams_cli/commands/."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Iterable

import click
import httpx
import yaml

from .commands import register_all
from .commands._common import (  # noqa: F401 — re-exported for tests
    _client_cache,
    _get_client,
    _handle_api_error,
    _parse_schedule_time,
    cfg,
)
from .exceptions import (
    ApiError,
    AuthRequiredError,
    ConfigurationError,
    RateLimitError,
    ResourceNotFoundError,
    RetryableError,
    TokenExpiredError,
)
from .formatter import console, print_error  # noqa: F401 — re-exported for tests
from .serialization import is_piped, to_json_error

BANNER = r"""
 ╔╦╗┌─┐┌─┐┌┬┐┌─┐  ╔═╗╦  ╦
  ║ ├┤ ├─┤│││└─┐  ║  ║  ║
  ╩ └─┘┴ ┴┴ ┴└─┘  ╚═╝╩═╝╩
"""


@dataclass(frozen=True)
class RuntimeOptions:
    dry_run: bool = False
    no_input: bool = False
    force: bool = False
    enable_commands: str = ""


class TeamsGroup(click.Group):
    """Custom group that shows ASCII banner in help."""

    def format_help(self, ctx, formatter):
        console.print(f"[bold cyan]{BANNER}[/bold cyan]", highlight=False)
        console.print("  [dim]Microsoft Teams from your terminal[/dim]")
        console.print()
        super().format_help(ctx, formatter)

    def main(
        self,
        args: list[str] | tuple[str, ...] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra,
    ):
        try:
            return super().main(
                args=args,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=False,
                windows_expand_args=windows_expand_args,
                **extra,
            )
        except KeyboardInterrupt:
            _emit_cli_error("Interrupted.", 130, args)
        except click.Abort:
            _emit_cli_error("Aborted.", 1, args)
        except click.ClickException as exc:
            _emit_cli_error(exc.format_message(), _click_error_exit_code(exc), args)
        except Exception as exc:  # pragma: no cover - exercised via wrapper tests/subprocess
            _emit_cli_error(_format_cli_error(exc), _exception_exit_code(exc), args)


@click.group(cls=TeamsGroup)
@click.option("--dry-run", is_flag=True, help="Print the intended mutation and exit without changing state")
@click.option("--no-input", is_flag=True, help="Never prompt for interactive input; fail instead")
@click.option("--force", is_flag=True, help="Skip confirmation prompts for mutating commands")
@click.option(
    "--enable-commands",
    default="",
    envvar="TEAMS_ENABLE_COMMANDS",
    help="Comma-separated allowlist of top-level commands for this invocation",
)
@click.version_option(package_name="microsoft-teams-cli")
@click.pass_context
def cli(ctx: click.Context, dry_run: bool, no_input: bool, force: bool, enable_commands: str):
    """Chat, send, search, and manage Microsoft Teams — no API keys required."""
    ctx.obj = RuntimeOptions(
        dry_run=dry_run,
        no_input=no_input,
        force=force,
        enable_commands=enable_commands,
    )
    _enforce_enabled_commands(enable_commands, ctx.invoked_subcommand)


register_all(cli)


def main(args: list[str] | None = None):
    """Console-script entrypoint."""
    return cli.main(args=args, prog_name="teams")


def _emit_cli_error(message: str, exit_code: int, args: Iterable[str] | None) -> None:
    if _wants_json_output(args):
        click.echo(to_json_error(message))
    else:
        print_error(message)
    raise SystemExit(exit_code)


def _wants_json_output(args: Iterable[str] | None) -> bool:
    arg_list = list(args or [])
    return "--json" in arg_list or is_piped()


def _click_error_exit_code(exc: click.ClickException) -> int:
    if isinstance(exc, (click.UsageError, click.BadParameter)):
        return 2
    return exc.exit_code


def _exception_exit_code(exc: Exception) -> int:
    if isinstance(exc, (AuthRequiredError, TokenExpiredError)):
        return 4
    if isinstance(exc, ResourceNotFoundError):
        return 5
    if isinstance(exc, RateLimitError):
        return 7
    if isinstance(exc, (RetryableError, httpx.TimeoutException, httpx.NetworkError)):
        return 8
    if isinstance(exc, ConfigurationError):
        return 10
    if isinstance(exc, ApiError):
        if exc.status_code in (401, 403):
            return 4
        if exc.status_code == 429:
            return 7
        if exc.status_code >= 500:
            return 8
    if isinstance(exc, (json.JSONDecodeError, OSError, yaml.YAMLError)):
        return 10
    return 1


def _format_cli_error(exc: Exception) -> str:
    if isinstance(exc, click.ClickException):
        return exc.format_message()
    if isinstance(exc, (AuthRequiredError, TokenExpiredError, ResourceNotFoundError, RateLimitError, RetryableError, ConfigurationError)):
        return str(exc)
    if isinstance(exc, ApiError):
        if exc.status_code:
            return f"{exc} (status {exc.status_code})"
        return str(exc)
    if isinstance(exc, yaml.YAMLError):
        return f"Configuration error: {exc}"
    if isinstance(exc, json.JSONDecodeError):
        return f"Cache/config JSON error: {exc}"
    if isinstance(exc, OSError):
        return f"Local file error: {exc}"
    return f"Error: {exc}"


def _enforce_enabled_commands(enabled: str, command: str | None) -> None:
    enabled = (enabled or "").strip()
    if not enabled or not command:
        return

    allow = {part.strip().lower() for part in enabled.split(",") if part.strip()}
    if not allow or "*" in allow or "all" in allow:
        return

    if command.lower() not in allow:
        raise click.UsageError(
            f'Command "{command}" is not enabled (set --enable-commands to allow it).'
        )


if __name__ == "__main__":  # pragma: no cover - module execution smoke only
    main()
