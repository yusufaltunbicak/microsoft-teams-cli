"""Group chat creation and message forwarding commands."""

from __future__ import annotations

import click

from ..formatter import console, print_error, print_success, print_users
from ._common import _get_client, _handle_api_error


def register(cli: click.Group) -> None:
    cli.add_command(group_chat)
    cli.add_command(forward)


@click.command(name="group-chat")
@click.argument("users", nargs=-1, required=True)
@click.option("--topic", "-t", default="", help="Group chat topic")
@click.option("--message", "-m", default="", help="First message to send")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@_handle_api_error
def group_chat(users: tuple[str, ...], topic: str, message: str, yes: bool):
    """Create a group chat with multiple users. Users can be names or emails."""
    client = _get_client()

    # Resolve each user query to a User object
    resolved = []
    for query in users:
        found = client.search_users(query, top=5)
        if not found:
            print_error(f"No user found matching '{query}'")
            return
        if len(found) > 1 and not yes:
            console.print(f"[bold]Multiple users found for '{query}':[/bold]")
            for i, u in enumerate(found, 1):
                console.print(f"  {i}. {u.display_name} ({u.email})")
            choice = click.prompt("Select user", type=int, default=1)
            resolved.append(found[choice - 1])
        else:
            resolved.append(found[0])

    if not yes:
        console.print("[bold]Create group chat:[/bold]")
        if topic:
            console.print(f"  [bold]Topic:[/bold] {topic}")
        console.print(f"  [bold]Members:[/bold] {', '.join(u.display_name for u in resolved)}")
        if message:
            console.print(f"  [bold]Message:[/bold] {message[:100]}{'...' if len(message) > 100 else ''}")
        click.confirm("Create this group chat?", abort=True)

    user_ids = [u.id for u in resolved]
    result = client.create_group_chat(user_ids, topic)
    chat_id = result.get("id", "")
    print_success(f"Group chat created{' (' + topic + ')' if topic else ''}")

    if message and chat_id:
        client.send_message(chat_id, message)
        print_success("First message sent")


@click.command()
@click.argument("msg_num")
@click.argument("chat_num")
@click.option("--comment", "-c", default="", help="Add a comment to the forwarded message")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@_handle_api_error
def forward(msg_num: str, chat_num: str, comment: str, yes: bool):
    """Forward a message to another chat."""
    client = _get_client()

    if not yes:
        original = client.get_message_detail(msg_num)
        preview = (original.text_content or "").strip()
        console.print(f"  [bold]Message:[/bold] #{msg_num} from {original.sender}")
        if preview:
            console.print(f"  [bold]Content:[/bold] {preview[:100]}{'...' if len(preview) > 100 else ''}")
        console.print(f"  [bold]To Chat:[/bold] #{chat_num}")
        if comment:
            console.print(f"  [bold]Comment:[/bold] {comment[:100]}{'...' if len(comment) > 100 else ''}")
        click.confirm("Forward this message?", abort=True)

    client.forward_message(msg_num, chat_num, comment=comment)
    print_success(f"Message #{msg_num} forwarded to chat #{chat_num}")
