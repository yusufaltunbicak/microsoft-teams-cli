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


@click.group()
@click.version_option(package_name="microsoft-teams-cli")
def cli():
    """Microsoft Teams CLI - chat, send, and manage Teams from the terminal."""
    pass


register_all(cli)
