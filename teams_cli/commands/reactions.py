"""Reaction commands: react, unreact."""

from __future__ import annotations

import click

from ..formatter import print_error, print_success
from ._common import VALID_REACTIONS, _get_client, _handle_api_error


def register(cli: click.Group) -> None:
    cli.add_command(react)
    cli.add_command(unreact)


@click.command()
@click.argument("emoji", type=click.Choice(sorted(VALID_REACTIONS)))
@click.argument("msg_nums", nargs=-1, required=True)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@_handle_api_error
def react(emoji: str, msg_nums: tuple[str, ...], yes: bool):
    """Add a reaction to one or more messages.

    EMOJI is the reaction type (like, heart, laugh, surprised, sad, angry).
    MSG_NUMS are the message numbers to react to.

    Example: teams react like 1 2 3
    """
    nums = list(msg_nums)
    if not yes:
        ids_str = ", ".join(f"#{n}" for n in nums)
        click.confirm(f"React with {emoji} on messages {ids_str}?", abort=True)
    client = _get_client()
    for n in nums:
        try:
            client.add_reaction(n, emoji)
            print_success(f"Reacted with {emoji} on message #{n}")
        except Exception as e:
            print_error(f"Failed to react on message #{n}: {e}")


@click.command()
@click.argument("emoji", type=click.Choice(sorted(VALID_REACTIONS)))
@click.argument("msg_nums", nargs=-1, required=True)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@_handle_api_error
def unreact(emoji: str, msg_nums: tuple[str, ...], yes: bool):
    """Remove a reaction from one or more messages.

    EMOJI is the reaction type (like, heart, laugh, surprised, sad, angry).
    MSG_NUMS are the message numbers to remove the reaction from.

    Example: teams unreact like 1 2 3
    """
    nums = list(msg_nums)
    if not yes:
        ids_str = ", ".join(f"#{n}" for n in nums)
        click.confirm(f"Remove {emoji} reaction from messages {ids_str}?", abort=True)
    client = _get_client()
    for n in nums:
        try:
            client.remove_reaction(n, emoji)
            print_success(f"Removed {emoji} from message #{n}")
        except Exception as e:
            print_error(f"Failed to remove reaction from message #{n}: {e}")
