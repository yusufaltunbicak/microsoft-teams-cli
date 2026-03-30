"""Send commands: chat-send, reply, send (to user), send-file."""

from __future__ import annotations

import click

from ..exceptions import ResourceNotFoundError
from ..formatter import console, print_success
from ..serialization import to_json
from ._common import (
    _format_size,
    _get_client,
    _handle_api_error,
    ensure_interactive_allowed,
    emit_dry_run,
    require_confirmation,
    should_skip_confirmation,
)


def register(cli: click.Group) -> None:
    cli.add_command(chat_send)
    cli.add_command(reply)
    cli.add_command(send)
    cli.add_command(send_file)


@click.command(name="chat-send")
@click.argument("chat_num")
@click.argument("message")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@_handle_api_error
def chat_send(chat_num: str, message: str, yes: bool):
    """Send a message to an existing chat."""
    if emit_dry_run(
        "send message to chat",
        {"chat": f"#{chat_num}", "message": message},
    ):
        return

    if not should_skip_confirmation(yes):
        ensure_interactive_allowed("send a message to a chat", local_force=yes)
        console.print(f"  [bold]Chat:[/bold] #{chat_num}")
        console.print(f"  [bold]Message:[/bold] {message[:100]}{'...' if len(message) > 100 else ''}")
        require_confirmation("Send this message?", "send a message to a chat", local_force=yes)

    client = _get_client()
    client.send_message_to_chat(chat_num, message)
    print_success(f"Message sent to chat #{chat_num}")


@click.command()
@click.argument("msg_num")
@click.argument("message")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@_handle_api_error
def reply(msg_num: str, message: str, yes: bool):
    """Reply to an existing message by its number."""
    if emit_dry_run(
        "reply to message",
        {"message_num": f"#{msg_num}", "message": message},
    ):
        return

    if not should_skip_confirmation(yes):
        ensure_interactive_allowed("reply to a message", local_force=yes)

    client = _get_client()
    if not should_skip_confirmation(yes):
        original = client.get_message_detail(msg_num)
        preview = (original.text_content or "").strip() or (original.attachments[0].name if original.attachments else "")
        console.print(f"  [bold]Reply To:[/bold] #{msg_num} {original.sender}")
        if preview:
            console.print(f"  [bold]Original:[/bold] {preview[:100]}{'...' if len(preview) > 100 else ''}")
        console.print(f"  [bold]Message:[/bold] {message[:100]}{'...' if len(message) > 100 else ''}")
        require_confirmation("Send this reply?", "reply to a message", local_force=yes)

    client.reply_to_message(msg_num, message)
    print_success(f"Reply sent to message #{msg_num}")


@click.command()
@click.argument("user_query")
@click.argument("message")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.option("--html", is_flag=True, help="Send as HTML")
@_handle_api_error
def send(user_query: str, message: str, yes: bool, html: bool):
    """Send a message to a person. Resolves name/email to 1:1 chat."""
    if emit_dry_run(
        "send direct message",
        {"user_query": user_query, "message": message, "html": html},
    ):
        return

    client = _get_client()
    users = client.search_users(user_query, top=10)

    if not users:
        raise ResourceNotFoundError(f"No user found matching '{user_query}'")

    # Filter: keep only users whose name matches ALL query words
    query_words = user_query.lower().split()
    strong_matches = [
        u for u in users
        if all(w in u.display_name.lower() or w in u.email.lower() for w in query_words)
    ]
    # Fall back to full list if no strong matches
    candidates = strong_matches or users

    target = candidates[0]
    if len(candidates) > 1 and not should_skip_confirmation(yes):
        console.print("[bold]Multiple users found:[/bold]")
        for i, u in enumerate(candidates[:10], 1):
            console.print(f"  {i}. {u.display_name} ({u.email})")
        if not should_skip_confirmation(yes):
            from ._common import get_runtime_options
            if get_runtime_options().no_input:
                raise click.UsageError(
                    "Refusing to select a user interactively (--no-input set; use --force/--yes with an unambiguous query)."
                )
        choice = click.prompt("Select user", type=int, default=1)
        target = candidates[choice - 1]

    # Safety: even with -y, if the best match doesn't contain all query words, warn
    target_searchable = target.display_name.lower() + " " + target.email.lower()
    if not all(w in target_searchable for w in query_words):
        if yes:
            raise click.ClickException(
                f"No exact match for '{user_query}'. Best match: {target.display_name} ({target.email}). "
                "Refusing to send with -y when match is uncertain. Remove -y to select manually."
            )
        require_confirmation(
            f"Send to {target.display_name} ({target.email})?",
            "send a direct message to the selected user",
            local_force=yes,
        )

    if not should_skip_confirmation(yes):
        console.print(f"  [bold]To:[/bold] {target.display_name} ({target.email})")
        console.print(f"  [bold]Message:[/bold] {message[:100]}{'...' if len(message) > 100 else ''}")
        require_confirmation("Send this message?", "send a direct message", local_force=yes)

    client.send_message_to_user(target.id, message, html=html)
    print_success(f"Message sent to {target.display_name}")


@click.command(name="send-file")
@click.argument("chat_num")
@click.argument("file", type=click.Path(exists=True))
@click.option("--message", "-m", default="", help="Optional message with the file")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@_handle_api_error
def send_file(chat_num: str, file: str, message: str, yes: bool):
    """Send a file to a chat. Uploads to OneDrive then sends as attachment."""
    from pathlib import Path

    if emit_dry_run(
        "send file to chat",
        {"chat": f"#{chat_num}", "file": file, "message": message},
    ):
        return

    path = Path(file)
    if not should_skip_confirmation(yes):
        console.print(f"  [bold]Chat:[/bold] #{chat_num}")
        console.print(f"  [bold]File:[/bold] {path.name} ({_format_size(path.stat().st_size)})")
        if message:
            console.print(f"  [bold]Message:[/bold] {message[:100]}")
        require_confirmation("Send this file?", "send a file to a chat", local_force=yes)

    client = _get_client()
    client.send_file_to_chat(chat_num, file, message=message)
    print_success(f"File '{path.name}' sent to chat #{chat_num}")
