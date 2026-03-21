from __future__ import annotations

import warnings
from datetime import datetime, timezone

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

try:
    from bs4 import MarkupResemblesLocatorWarning
    warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)
except ImportError:
    pass

from .models import Chat, Message, User

console = Console(stderr=True)


def print_chats(chats: list[Chat]) -> None:
    table = Table(show_header=True, header_style="bold cyan", box=None, pad_edge=False)
    table.add_column("Chat", width=35, no_wrap=True, overflow="ellipsis")
    table.add_column("Last Message", ratio=1, no_wrap=True, overflow="ellipsis")
    table.add_column("From", width=15, no_wrap=True)
    table.add_column("Time", width=9, no_wrap=True, justify="right")
    table.add_column("", width=2)  # flags

    for chat in chats:
        flags = ""
        if chat.unread_count > 0:
            flags = f"*{chat.unread_count}" if chat.unread_count > 1 else "*"

        style = "bold" if chat.unread_count > 0 else ""
        title = _format_chat_title(chat)
        preview = _truncate(chat.last_message_preview, 50)
        sender = _truncate(chat.last_message_sender, 13)

        table.add_row(
            title,
            preview,
            sender,
            _format_date(chat.last_message_time),
            flags,
            style=style,
        )

    console.print(table)


def print_messages(messages: list[Message], chat_title: str = "") -> None:
    if chat_title:
        console.print(f"[bold cyan]{chat_title}[/bold cyan]")
        console.print()

    for msg in messages:
        _print_message_line(msg)


def _print_message_line(msg: Message) -> None:
    if msg.timestamp != datetime.min.replace(tzinfo=timezone.utc):
        local_ts = msg.timestamp.astimezone(datetime.now().astimezone().tzinfo)
        time_str = local_ts.strftime("%H:%M")
    else:
        time_str = ""
    num_str = f"[dim]#{msg.display_num}[/dim]" if msg.display_num else ""

    # Chat title for search results
    chat_str = f" [dim]\\[{msg.chat_title}][/dim]" if msg.chat_title else ""

    sender_style = "bold blue" if msg.is_from_me else "bold"
    sender = f"[{sender_style}]{msg.sender}[/{sender_style}]"

    content = _strip_html_for_display(msg.content)
    content = _truncate(content, 200)

    # Reactions summary
    reaction_str = ""
    if msg.reactions:
        from collections import Counter
        counts = Counter(r.emoji for r in msg.reactions)
        reaction_str = " " + " ".join(f"[dim]{e}({c})[/dim]" for e, c in counts.items())

    console.print(f"  {num_str} [dim]{time_str}[/dim]{chat_str} {sender}: {content}{reaction_str}")


def print_message_detail(msg: Message) -> None:
    header = f"[bold]From:[/bold] {msg.sender}"
    if msg.subject:
        header += f"\n[bold]Subject:[/bold] {msg.subject}"
    local_ts = msg.timestamp.astimezone(datetime.now().astimezone().tzinfo)
    header += f"\n[bold]Time:[/bold] {local_ts.strftime('%Y-%m-%d %H:%M:%S')}"
    if msg.importance:
        header += f"\n[bold]Importance:[/bold] {msg.importance}"

    body = _strip_html_for_display(msg.content)

    console.print(Panel(header, title=f"Message #{msg.display_num}", border_style="cyan"))
    console.print()
    console.print(body)

    if msg.reactions:
        console.print()
        console.print("[bold]Reactions:[/bold]")
        from collections import Counter
        counts = Counter(r.emoji for r in msg.reactions)
        for emoji, count in counts.items():
            users = [r.user for r in msg.reactions if r.emoji == emoji]
            console.print(f"  {emoji} ({count}): {', '.join(users)}")

    if msg.attachments:
        console.print()
        console.print("[bold]Attachments:[/bold]")
        for att in msg.attachments:
            console.print(f"  {att.name} ({att.content_type})")


def print_users(users: list[User]) -> None:
    table = Table(show_header=True, header_style="bold cyan", box=None, pad_edge=False)
    table.add_column("#", style="dim", width=5, justify="right")
    table.add_column("Name", min_width=25)
    table.add_column("Email", min_width=30)
    table.add_column("Type", width=10)

    for i, user in enumerate(users, 1):
        table.add_row(str(i), user.display_name, user.email, user.user_type)

    console.print(table)


def print_whoami(data: dict) -> None:
    console.print(f"[bold]Name:[/bold]    {data.get('displayName', 'N/A')}")
    if data.get("mail"):
        console.print(f"[bold]Email:[/bold]   {data['mail']}")
    if data.get("jobTitle"):
        console.print(f"[bold]Title:[/bold]   {data['jobTitle']}")
    console.print(f"[bold]User ID:[/bold] {data.get('user_id', 'N/A')}")
    console.print(f"[bold]Region:[/bold]  {data.get('region', 'N/A')}")


def print_success(msg: str) -> None:
    console.print(f"[green]{msg}[/green]")


def print_error(msg: str) -> None:
    console.print(f"[red]{msg}[/red]")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _truncate(s: str, max_len: int) -> str:
    if not s:
        return ""
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "\u2026"


def _format_date(dt: datetime) -> str:
    if dt.year == 1:  # datetime.min sentinel
        return ""
    now_local = datetime.now().astimezone()
    if dt.tzinfo is None:
        from datetime import timezone as tz
        dt = dt.replace(tzinfo=tz.utc)
    dt_local = dt.astimezone(now_local.tzinfo)
    if dt_local.date() == now_local.date():
        return dt_local.strftime("%H:%M")
    if (now_local.date() - dt_local.date()).days == 1:
        return "Yday"
    diff = now_local - dt_local
    if diff.days < 7:
        return dt_local.strftime("%a")
    if dt_local.year == now_local.year:
        return dt_local.strftime("%d %b")
    return dt_local.strftime("%d %b %y")


def _format_chat_title(chat: Chat) -> str:
    prefix = f"#{chat.display_num} " if chat.display_num else ""
    return f"{prefix}{_truncate(chat.display_title, 30)}"


def _strip_html_for_display(html: str) -> str:
    """Convert HTML to readable text."""
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["style", "script"]):
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)
    except ImportError:
        import re
        return re.sub(r"<[^>]+>", "", html)
