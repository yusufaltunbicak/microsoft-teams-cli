"""Schedule commands: schedule, schedule-list, schedule-cancel, schedule-run."""

from __future__ import annotations

from datetime import datetime

import click

from ..exceptions import ResourceNotFoundError
from ..formatter import console, print_error, print_success
from ..serialization import to_json
from ._common import (
    _get_client,
    _handle_api_error,
    _parse_schedule_time,
    emit_dry_run,
    require_confirmation,
    should_json,
    should_skip_confirmation,
)


def register(cli: click.Group) -> None:
    cli.add_command(schedule)
    cli.add_command(schedule_list_cmd)
    cli.add_command(schedule_cancel)
    cli.add_command(schedule_run)


@click.command()
@click.argument("chat_num")
@click.argument("message")
@click.argument("at")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@_handle_api_error
def schedule(chat_num: str, message: str, at: str, yes: bool):
    """Schedule a message to be sent later.

    AT: +30m, +1h, tomorrow 09:00, 2024-03-15T10:00
    """
    from ..scheduler import add_scheduled

    send_at = _parse_schedule_time(at)
    if emit_dry_run(
        "schedule message",
        {"chat": f"#{chat_num}", "message": message, "at": at},
    ):
        return

    client = _get_client()
    conv_id = client._resolve_chat_id(chat_num)

    if not should_skip_confirmation(yes):
        local_send = send_at.astimezone(datetime.now().astimezone().tzinfo)
        console.print(f"  [bold]Chat:[/bold] #{chat_num}")
        console.print(f"  [bold]Message:[/bold] {message[:100]}{'...' if len(message) > 100 else ''}")
        console.print(f"  [bold]Scheduled:[/bold] {local_send.strftime('%Y-%m-%d %H:%M')}")
        require_confirmation("Schedule this message?", "schedule a message", local_force=yes)

    add_scheduled(
        conv_id=conv_id,
        content=message,
        send_at=send_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        chat_title=f"Chat #{chat_num}",
    )
    local_send = send_at.astimezone(datetime.now().astimezone().tzinfo)
    print_success(f"Message scheduled for {local_send.strftime('%Y-%m-%d %H:%M')}")


@click.command(name="schedule-list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def schedule_list_cmd(as_json: bool):
    """List scheduled messages."""
    from ..scheduler import load_scheduled

    entries = load_scheduled()
    pending = [e for e in entries if e.get("status") == "pending"]

    if should_json(as_json):
        click.echo(to_json(pending))
    else:
        if not pending:
            print_success("No scheduled messages.")
        else:
            from rich.table import Table
            table = Table(show_header=True, header_style="bold cyan", box=None, pad_edge=False)
            table.add_column("#", style="dim", width=4, justify="right")
            table.add_column("Chat", width=20, no_wrap=True)
            table.add_column("Message", ratio=1, no_wrap=True, overflow="ellipsis")
            table.add_column("Scheduled", width=16, no_wrap=True, justify="right")

            for i, entry in enumerate(pending, 1):
                sched = entry.get("send_at", "")
                try:
                    sched_dt = datetime.fromisoformat(sched.replace("Z", "+00:00"))
                    local_dt = sched_dt.astimezone(datetime.now().astimezone().tzinfo)
                    sched_display = local_dt.strftime("%Y-%m-%d %H:%M")
                except (ValueError, AttributeError):
                    sched_display = sched
                table.add_row(
                    str(i),
                    entry.get("chat_title", "")[:20],
                    entry.get("content", "")[:50],
                    sched_display,
                )
            console.print("[bold cyan]Scheduled Messages[/bold cyan]")
            console.print(table)


@click.command(name="schedule-cancel")
@click.argument("index", type=int)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def schedule_cancel(index: int, yes: bool):
    """Cancel a scheduled message by its list number."""
    from ..scheduler import cancel_scheduled, load_scheduled

    entries = load_scheduled()
    pending = [e for e in entries if e.get("status") == "pending"]

    if index < 1 or index > len(pending):
        raise ResourceNotFoundError(f"Invalid index #{index}. Run 'teams schedule-list' to see entries.")

    entry = pending[index - 1]
    if emit_dry_run(
        "cancel scheduled message",
        {"index": index, "chat": entry.get("chat_title", ""), "message": entry.get("content", "")},
    ):
        return

    if not should_skip_confirmation(yes):
        console.print(f"  [bold]Chat:[/bold] {entry.get('chat_title', '')}")
        console.print(f"  [bold]Message:[/bold] {entry.get('content', '')[:100]}")
        console.print(f"  [bold]Scheduled:[/bold] {entry.get('send_at', '')}")
        require_confirmation(
            f"Cancel scheduled message #{index}?",
            "cancel a scheduled message",
            local_force=yes,
        )

    full_entries = load_scheduled()
    for i, e in enumerate(full_entries):
        if (e.get("created_at") == entry.get("created_at")
                and e.get("conv_id") == entry.get("conv_id")
                and e.get("status") == "pending"):
            cancel_scheduled(i + 1)
            break

    print_success(f"Scheduled message #{index} cancelled")


@click.command(name="schedule-run")
@_handle_api_error
def schedule_run():
    """Send all pending scheduled messages that are due."""
    from ..scheduler import get_pending, load_scheduled, mark_sent

    pending = get_pending()
    if not pending:
        print_success("No messages due to send.")
        return

    if emit_dry_run(
        "run scheduled messages",
        {
            "count": len(pending),
            "chats": [entry.get("chat_title", "") for entry in pending],
        },
    ):
        return

    client = _get_client()
    entries = load_scheduled()

    sent_count = 0
    failures: list[Exception] = []
    for entry in pending:
        try:
            conv_id = entry["conv_id"]
            content = entry["content"]
            client.send_message(conv_id, content)

            for i, e in enumerate(entries):
                if e.get("created_at") == entry.get("created_at") and e.get("status") == "pending":
                    mark_sent(i)
                    break

            sent_count += 1
            print_success(f"Sent: {entry.get('chat_title', '')} - {content[:50]}")
        except Exception as e:
            print_error(f"Failed to send to {entry.get('chat_title', '')}: {e}")
            failures.append(e)

    print_success(f"\n{sent_count}/{len(pending)} messages sent")
    if failures:
        raise failures[0]
