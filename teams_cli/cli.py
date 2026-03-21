"""Thin CLI entry point — commands live in teams_cli/commands/."""

from __future__ import annotations

import click

from .commands import register_all
from .commands._common import (  # noqa: F401 — re-exported for tests
    _client_cache,
    _get_client,
    _handle_api_error,
    _parse_schedule_time,
    cfg,
)
from .formatter import console  # noqa: F401 — re-exported for tests

BANNER = r"""
 ╔╦╗┌─┐┌─┐┌┬┐┌─┐  ╔═╗╦  ╦
  ║ ├┤ ├─┤│││└─┐  ║  ║  ║
  ╩ └─┘┴ ┴┴ ┴└─┘  ╚═╝╩═╝╩
"""


class TeamsGroup(click.Group):
    """Custom group that shows ASCII banner in help."""

    def format_help(self, ctx, formatter):
        console.print(f"[bold cyan]{BANNER}[/bold cyan]", highlight=False)
        console.print("  [dim]Microsoft Teams from your terminal[/dim]")
        console.print()
        super().format_help(ctx, formatter)


@click.group(cls=TeamsGroup)
@click.version_option(package_name="microsoft-teams-cli")
def cli():
    """Chat, send, search, and manage Microsoft Teams — no API keys required."""
    pass


register_all(cli)
