"""Chat commands: chats, chat, read, unread."""

from __future__ import annotations

import click

from ..formatter import console, print_chats, print_message_detail, print_messages, print_success
from ..serialization import to_json
from ._common import _get_client, _handle_api_error, cfg, should_json


def register(cli: click.Group) -> None:
    cli.add_command(chats)
    cli.add_command(chat)
    cli.add_command(read)
    cli.add_command(unread)


@click.command()
@click.option("--max", "-n", "max_count", default=None, type=int, help="Number of chats")
@click.option("--offset", default=0, type=int, help="Skip first N items")
@click.option("--unread", is_flag=True, help="Show only unread chats")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@_handle_api_error
def chats(max_count: int | None, offset: int, unread: bool, as_json: bool):
    """List recent chats."""
    client = _get_client()
    top = max_count or cfg["max_chats"]
    chat_list = client.get_chats(top=top + offset, unread_only=unread)
    if offset:
        chat_list = chat_list[offset:]

    if should_json(as_json):
        click.echo(to_json(chat_list))
    else:
        if not chat_list:
            print_success("No chats found.")
        else:
            print_chats(chat_list)


@click.command()
@click.argument("chat_num")
@click.option("--max", "-n", "max_count", default=None, type=int, help="Number of messages")
@click.option("--offset", default=0, type=int, help="Skip first N messages")
@click.option("--after", default=None, help="Only messages after date (YYYY-MM-DD)")
@click.option("--before", default=None, help="Only messages before date (YYYY-MM-DD)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@_handle_api_error
def chat(chat_num: str, max_count: int | None, offset: int, after: str | None, before: str | None, as_json: bool):
    """Read messages from a chat by its number."""
    client = _get_client()
    top = max_count or cfg["max_messages"]
    messages = client.get_chat_messages(chat_num, top=top + offset, after=after, before=before)
    if offset:
        messages = messages[offset:]

    if should_json(as_json):
        click.echo(to_json(messages))
    else:
        try:
            client._resolve_chat_id(chat_num)
            title = f"Chat #{chat_num}"
        except ValueError:
            title = f"Chat #{chat_num}"
        if not messages:
            print_success(f"No messages in chat #{chat_num}.")
        else:
            print_messages(messages, chat_title=title)


@click.command()
@click.argument("msg_num")
@click.option("--raw", is_flag=True, help="Show raw HTML content")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@_handle_api_error
def read(msg_num: str, raw: bool, as_json: bool):
    """Read full message detail by its number."""
    client = _get_client()
    msg = client.get_message_detail(msg_num)

    if should_json(as_json):
        click.echo(to_json(msg))
    elif raw:
        console.print(msg.content)
    else:
        print_message_detail(msg)


@click.command()
@click.option("--max", "-n", "max_count", default=None, type=int, help="Number of chats")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@_handle_api_error
def unread(max_count: int | None, as_json: bool):
    """Show chats with unread messages."""
    client = _get_client()
    top = max_count or cfg["max_chats"]
    chat_list = client.get_chats(top=top, unread_only=True)

    if should_json(as_json):
        click.echo(to_json(chat_list))
    else:
        if not chat_list:
            print_success("No unread chats.")
        else:
            console.print(f"[bold cyan]{len(chat_list)} unread chat(s)[/bold cyan]")
            print_chats(chat_list)
