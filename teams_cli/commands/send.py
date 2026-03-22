"""Send commands: chat-send, reply, send (to user), send-file."""

from __future__ import annotations

import click

from ..formatter import console, print_error, print_success, print_users
from ..serialization import to_json
from ._common import _format_size, _get_client, _handle_api_error


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
    client = _get_client()
    if not yes:
        console.print(f"  [bold]Chat:[/bold] #{chat_num}")
        console.print(f"  [bold]Message:[/bold] {message[:100]}{'...' if len(message) > 100 else ''}")
        click.confirm("Send this message?", abort=True)

    client.send_message_to_chat(chat_num, message)
    print_success(f"Message sent to chat #{chat_num}")


@click.command()
@click.argument("msg_num")
@click.argument("message")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@_handle_api_error
def reply(msg_num: str, message: str, yes: bool):
    """Reply to an existing message by its number."""
    client = _get_client()
    if not yes:
        original = client.get_message_detail(msg_num)
        preview = (original.text_content or "").strip() or (original.attachments[0].name if original.attachments else "")
        console.print(f"  [bold]Reply To:[/bold] #{msg_num} {original.sender}")
        if preview:
            console.print(f"  [bold]Original:[/bold] {preview[:100]}{'...' if len(preview) > 100 else ''}")
        console.print(f"  [bold]Message:[/bold] {message[:100]}{'...' if len(message) > 100 else ''}")
        click.confirm("Send this reply?", abort=True)

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
    client = _get_client()
    users = client.search_users(user_query, top=10)

    if not users:
        print_error(f"No user found matching '{user_query}'")
        return

    # Filter: keep only users whose name matches ALL query words
    query_words = user_query.lower().split()
    strong_matches = [
        u for u in users
        if all(w in u.display_name.lower() or w in u.email.lower() for w in query_words)
    ]
    # Fall back to full list if no strong matches
    candidates = strong_matches or users

    target = candidates[0]
    if len(candidates) > 1 and not yes:
        console.print("[bold]Multiple users found:[/bold]")
        for i, u in enumerate(candidates[:10], 1):
            console.print(f"  {i}. {u.display_name} ({u.email})")
        choice = click.prompt("Select user", type=int, default=1)
        target = candidates[choice - 1]

    # Safety: even with -y, if the best match doesn't contain all query words, warn
    target_searchable = target.display_name.lower() + " " + target.email.lower()
    if not all(w in target_searchable for w in query_words):
        print_error(f"No exact match for '{user_query}'. Best match: {target.display_name} ({target.email})")
        if yes:
            print_error("Refusing to send with -y when match is uncertain. Remove -y to select manually.")
            return
        click.confirm(f"Send to {target.display_name} ({target.email})?", abort=True)

    if not yes:
        console.print(f"  [bold]To:[/bold] {target.display_name} ({target.email})")
        console.print(f"  [bold]Message:[/bold] {message[:100]}{'...' if len(message) > 100 else ''}")
        click.confirm("Send this message?", abort=True)

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
    path = Path(file)
    if not yes:
        console.print(f"  [bold]Chat:[/bold] #{chat_num}")
        console.print(f"  [bold]File:[/bold] {path.name} ({_format_size(path.stat().st_size)})")
        if message:
            console.print(f"  [bold]Message:[/bold] {message[:100]}")
        click.confirm("Send this file?", abort=True)

    client = _get_client()
    client.send_file_to_chat(chat_num, file, message=message)
    print_success(f"File '{path.name}' sent to chat #{chat_num}")
