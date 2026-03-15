"""Search commands: search, user-search."""

from __future__ import annotations

import click

from ..formatter import print_error, print_messages, print_users
from ..serialization import to_json
from ._common import _get_client, _handle_api_error, should_json


def register(cli: click.Group) -> None:
    cli.add_command(search)
    cli.add_command(user_search)


@click.command()
@click.argument("query")
@click.option("--max", "-n", "max_count", default=25, type=int, help="Max results")
@click.option("--offset", default=0, type=int, help="Skip first N results")
@click.option("--chat", "chat_num", default=None, help="Search within a specific chat")
@click.option("--from", "from_filter", default=None, help="Filter by sender name")
@click.option("--after", default=None, help="After date (YYYY-MM-DD)")
@click.option("--before", default=None, help="Before date (YYYY-MM-DD)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@_handle_api_error
def search(query: str, max_count: int, offset: int, chat_num: str | None, from_filter: str | None, after: str | None, before: str | None, as_json: bool):
    """Search messages across chats."""
    client = _get_client()
    messages = client.search_messages(
        query, top=max_count + offset, chat_num=chat_num,
        from_filter=from_filter, after=after, before=before,
    )
    if offset:
        messages = messages[offset:]

    if should_json(as_json):
        click.echo(to_json(messages))
    else:
        if not messages:
            print_error("No results found.")
        else:
            print_messages(messages, chat_title=f"Search: {query}")


@click.command(name="user-search")
@click.argument("query")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@_handle_api_error
def user_search(query: str, as_json: bool):
    """Search for users by name or email."""
    client = _get_client()
    users = client.search_users(query)

    if should_json(as_json):
        click.echo(to_json(users))
    else:
        if not users:
            print_error(f"No users found matching '{query}'")
        else:
            print_users(users)
