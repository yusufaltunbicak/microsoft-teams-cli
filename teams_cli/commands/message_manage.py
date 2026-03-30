"""Message management commands: edit, delete."""

from __future__ import annotations

import click

from ..formatter import console, print_success
from ._common import (
    _get_client,
    _handle_api_error,
    emit_dry_run,
    require_confirmation,
    should_skip_confirmation,
)


def register(cli: click.Group) -> None:
    cli.add_command(edit)
    cli.add_command(delete)


@click.command()
@click.argument("msg_num")
@click.argument("new_text")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@_handle_api_error
def edit(msg_num: str, new_text: str, yes: bool):
    """Edit a message by its number."""
    if emit_dry_run(
        "edit message",
        {"message_num": f"#{msg_num}", "new_text": new_text},
    ):
        return

    if not should_skip_confirmation(yes):
        console.print(f"  [bold]Message:[/bold] #{msg_num}")
        console.print(f"  [bold]New text:[/bold] {new_text[:100]}{'...' if len(new_text) > 100 else ''}")
        require_confirmation("Edit this message?", "edit a message", local_force=yes)

    client = _get_client()
    client.edit_message(msg_num, new_text)
    print_success(f"Message #{msg_num} edited")


@click.command()
@click.argument("msg_num")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@_handle_api_error
def delete(msg_num: str, yes: bool):
    """Delete a message by its number."""
    if emit_dry_run(
        "delete message",
        {"message_num": f"#{msg_num}"},
    ):
        return

    if not should_skip_confirmation(yes):
        console.print(f"  [bold]Message:[/bold] #{msg_num}")
        require_confirmation(
            "Delete this message? This cannot be undone.",
            "delete a message",
            local_force=yes,
        )

    client = _get_client()
    client.delete_message(msg_num)
    print_success(f"Message #{msg_num} deleted")
