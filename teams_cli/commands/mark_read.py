"""Mark-read command: mark messages or chats as read or unread."""

from __future__ import annotations

import click

from ..formatter import print_error, print_success
from ._common import _get_client, _handle_api_error


def register(cli: click.Group) -> None:
    cli.add_command(mark_read)


@click.command(name="mark-read")
@click.argument("nums", nargs=-1, required=True)
@click.option("--chat", "is_chat", is_flag=True, help="Treat NUMs as chat numbers instead of message numbers")
@click.option("--unread", is_flag=True, help="Mark as unread instead of read")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@_handle_api_error
def mark_read(nums: tuple[str, ...], is_chat: bool, unread: bool, yes: bool):
    """Mark one or more messages (or chats) as read/unread.

    NUMs are message numbers by default, or chat numbers with --chat.

    Example: teams mark-read 1 2 3
    Example: teams mark-read --chat 9 10 11 -y
    Example: teams mark-read 1 --unread
    """
    items = list(nums)
    action = "unread" if unread else "read"
    kind = "chat" if is_chat else "message"

    if is_chat and unread:
        print_error("--unread with --chat is not supported. Use message numbers for --unread.")
        return

    if not yes:
        ids_str = ", ".join(f"#{n}" for n in items)
        click.confirm(f"Mark {kind}s {ids_str} as {action}?", abort=True)

    client = _get_client()
    for n in items:
        try:
            if is_chat:
                client.mark_chat_read(n)
                print_success(f"Marked chat #{n} as read")
            elif unread:
                client.mark_message_unread(n)
                print_success(f"Marked message #{n} as {action}")
            else:
                client.mark_message_read(n)
                print_success(f"Marked message #{n} as {action}")
        except Exception as e:
            print_error(f"Failed to mark {kind} #{n} as {action}: {e}")
