"""Mark-read command: mark messages as read or unread."""

from __future__ import annotations

import click

from ..formatter import print_error, print_success
from ._common import _get_client, _handle_api_error


def register(cli: click.Group) -> None:
    cli.add_command(mark_read)


@click.command(name="mark-read")
@click.argument("msg_nums", nargs=-1, required=True)
@click.option("--unread", is_flag=True, help="Mark as unread instead of read")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@_handle_api_error
def mark_read(msg_nums: tuple[str, ...], unread: bool, yes: bool):
    """Mark one or more messages as read (or unread with --unread).

    MSG_NUMS are the message numbers to mark.

    Example: teams mark-read 1 2 3
    Example: teams mark-read 1 --unread
    """
    nums = list(msg_nums)
    action = "unread" if unread else "read"
    if not yes:
        ids_str = ", ".join(f"#{n}" for n in nums)
        click.confirm(f"Mark messages {ids_str} as {action}?", abort=True)
    client = _get_client()
    for n in nums:
        try:
            if unread:
                client.mark_message_unread(n)
            else:
                client.mark_message_read(n)
            print_success(f"Marked message #{n} as {action}")
        except Exception as e:
            print_error(f"Failed to mark message #{n} as {action}: {e}")
