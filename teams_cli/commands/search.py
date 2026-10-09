"""Search commands: search, user-search."""

from __future__ import annotations

import click
from dataclasses import asdict

from ..formatter import print_error, print_messages, print_users
from ..serialization import to_json
from ..history import HistoryIndex, account_key, saved_tokens, number_messages
from ..context import context_metadata
from ._common import _get_client, _handle_api_error, should_json


def register(cli: click.Group) -> None:
    cli.add_command(search)
    cli.add_command(user_search)


@click.command()
@click.argument("query")
@click.option("--max", "-n", "max_count", default=25, type=click.IntRange(1), help="Max results")
@click.option("--offset", default=0, type=click.IntRange(0), help="Skip first N results")
@click.option("--chat", "chat_num", default=None, help="Search within a specific chat")
@click.option("--from", "from_filter", default=None, help="Filter by sender name")
@click.option("--after", default=None, help="After date (YYYY-MM-DD)")
@click.option("--before", default=None, help="Before date (YYYY-MM-DD)")
@click.option("--local", is_flag=True, help="Search the private index offline; run sync first.")
@click.option("--context", default=0, type=click.IntRange(0, 10), help="Surrounding messages on each side of a hit.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@_handle_api_error
def search(query: str, max_count: int, offset: int, chat_num: str | None, from_filter: str | None, after: str | None, before: str | None, local: bool, context: int, as_json: bool):
    """Search messages across chats."""
    from ..history import date_bound
    try:
        for value in (after, before):
            if value:
                date_bound(value)
    except ValueError as exc:
        raise click.BadParameter("Dates must be YYYY-MM-DD or ISO timestamps.") from exc
    if after and before and date_bound(after) > date_bound(before, end=True):
        raise click.BadParameter("--after must precede --before.")
    if local:
        tokens = saved_tokens()
        with HistoryIndex(account_key(tokens)) as index:
            messages = index.search(query, top=max_count, offset=offset, chat=chat_num,
                                    sender=from_filter, after=after, before=before)
            data = []
            contexts = [index.context(msg, context) if context else [] for msg in messages]
            number_messages(messages + [m for surrounding in contexts for m in surrounding], tokens=tokens)
            for msg, surrounding in zip(messages, contexts):
                item = asdict(msg)
                if context:
                    item["context"] = [asdict(m) for m in surrounding]
                    item["context_meta"] = context_metadata(msg, surrounding, context, context)
                data.append(item)
            coverage = index.status()
        if should_json(as_json):
            click.echo(to_json(data, meta={"source": "local", "coverage": coverage}))
        else:
            from ..formatter import console
            console.print(f"[dim]Local index: {coverage['messages']} messages, {coverage['chats']} chats; cache status shows coverage.[/dim]")
            if not messages:
                print_error("No results in the local index. Use live search for broader coverage.")
            for i, msg in enumerate(messages):
                print_messages(index_messages(data[i], msg) if context else [msg], chat_title=msg.chat_title)
        return
    client = _get_client()
    messages = client.search_messages(
        query, top=max_count + offset, chat_num=chat_num,
        from_filter=from_filter, after=after, before=before,
    )
    if offset:
        messages = messages[offset:]

    if context:
        enriched = []
        for msg in messages:
            surrounding = client.get_message_context(str(msg.display_num), before=context, after=context)
            item = asdict(msg)
            item["context"] = [asdict(m) for m in surrounding]
            item["context_meta"] = context_metadata(msg, surrounding, context, context)
            enriched.append(item)
        if should_json(as_json):
            click.echo(to_json(enriched))
        else:
            for msg, item in zip(messages, enriched):
                print_messages(index_messages(item, msg), chat_title=msg.chat_title)
    elif should_json(as_json):
        click.echo(to_json(messages))
    else:
        if not messages:
            print_error("No results found.")
        else:
            print_messages(messages, chat_title=f"Search: {query}")


def index_messages(item: dict, fallback):
    """Convert additive context data back to messages for the shared renderer."""
    from ..models import Message
    return [Message(**data) for data in item.get("context", [])] or [fallback]


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
