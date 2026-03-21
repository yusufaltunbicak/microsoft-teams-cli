"""Summary command: quick dashboard with status, unreads, and recent activity."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

import click

from ..formatter import (
    STATUS_DOTS,
    STATUS_COLORS,
    console,
    print_error,
    _format_date,
    _truncate,
)
from ..serialization import to_json
from ._common import _get_client, _handle_api_error, cfg, should_json


def register(cli: click.Group) -> None:
    cli.add_command(summary)


def _fetch_presence(client):
    try:
        return client.get_presence()
    except Exception:
        return {"availability": "Unknown"}


def _fetch_chats(client, top):
    try:
        return client.get_chats(top=top)
    except Exception:
        return []


def _fetch_unread(client):
    try:
        return client.get_chats(top=200, unread_only=True)
    except Exception:
        return []


@click.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@_handle_api_error
def summary(as_json: bool):
    """Quick dashboard: status, unreads, and recent activity."""
    client = _get_client()
    top = cfg.get("max_chats", 25)

    # Parallel API calls for speed
    results = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(_fetch_presence, client): "presence",
            pool.submit(_fetch_chats, client, min(top, 5)): "recent",
            pool.submit(_fetch_unread, client): "unread",
        }
        for future in as_completed(futures):
            key = futures[future]
            results[key] = future.result()

    presence = results.get("presence", {})
    recent_chats = results.get("recent", [])
    unread_chats = results.get("unread", [])

    if should_json(as_json):
        data = {
            "presence": presence,
            "unread_count": len(unread_chats),
            "unread_messages": sum(c.unread_count for c in unread_chats),
            "recent": [
                {
                    "display_num": c.display_num,
                    "title": c.display_title,
                    "last_message": c.last_message_preview,
                    "sender": c.last_message_sender,
                    "unread": c.unread_count,
                }
                for c in recent_chats
            ],
        }
        click.echo(to_json(data))
        return

    # ── Status ──
    availability = presence.get("availability", "Unknown")
    dot = STATUS_DOTS.get(availability, STATUS_DOTS["Unknown"])
    color = STATUS_COLORS.get(availability, "dim")

    console.print()
    console.print(f"  {dot} [bold {color}]{availability}[/bold {color}]", highlight=False)

    # ── Unread summary ──
    total_unread_msgs = sum(c.unread_count for c in unread_chats)
    if unread_chats:
        console.print(
            f"  [bold cyan]{len(unread_chats)}[/bold cyan] unread chat(s)"
            f"  [dim]({total_unread_msgs} message(s))[/dim]"
        )
    else:
        console.print("  [dim]No unread messages[/dim]")

    # ── Recent activity ──
    console.print()
    console.print("  [bold]Recent[/bold]")
    if not recent_chats:
        console.print("  [dim]No recent chats[/dim]")
    else:
        for chat in recent_chats[:5]:
            num = f"[dim]#{chat.display_num}[/dim]" if chat.display_num else ""
            title = _truncate(chat.display_title, 20)
            preview = _truncate(chat.last_message_preview, 30)
            time_str = _format_date(chat.last_message_time)
            unread_mark = "[bold cyan]*[/bold cyan]" if chat.unread_count > 0 else " "

            console.print(
                f"  {unread_mark} {num} {title}  "
                f"[dim]{preview}[/dim]  "
                f"[dim]{time_str}[/dim]"
            )
    console.print()
