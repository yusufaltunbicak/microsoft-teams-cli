"""Command modules — each file registers its commands on the CLI group."""

from __future__ import annotations

import click

from . import attachments, auth, chat, group_chat, mark_read, message_manage, presence, reactions, schedule, search, send, teams_channels

_MODULES = [
    auth,
    chat,
    send,
    search,
    reactions,
    mark_read,
    teams_channels,
    schedule,
    presence,
    attachments,
    message_manage,
    group_chat,
]


def register_all(cli: click.Group) -> None:
    """Register every command module on the CLI group."""
    for mod in _MODULES:
        mod.register(cli)
