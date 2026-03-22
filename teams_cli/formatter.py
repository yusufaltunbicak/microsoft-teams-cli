from __future__ import annotations

import warnings
from datetime import datetime, timezone

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

try:
    from bs4 import MarkupResemblesLocatorWarning
    warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)
except ImportError:
    pass

from .models import Chat, Meeting, Message, Recording, Transcript, User

console = Console(stderr=True)

# Chat type icons
_CHAT_ICONS = {
    "oneOnOne": "\u2502",   # │ subtle vertical bar for 1:1
    "group": "\u25cb",      # ○ circle for group
    "chat": "\u25cb",       # ○ same for generic "chat" type
    "meeting": "\u25a1",    # □ square for meeting
}

# Status color mapping
STATUS_COLORS = {
    "Available": "green",
    "Busy": "red",
    "DoNotDisturb": "red",
    "BeRightBack": "yellow",
    "Away": "yellow",
    "Offline": "dim",
    "Unknown": "dim",
}

STATUS_DOTS = {
    "Available": "[green]\u25cf[/green]",       # ●
    "Busy": "[red]\u25cf[/red]",
    "DoNotDisturb": "[red]\u2b24[/red]",         # ⬤
    "BeRightBack": "[yellow]\u25cf[/yellow]",
    "Away": "[yellow]\u25cb[/yellow]",           # ○
    "Offline": "[dim]\u25cb[/dim]",
    "Unknown": "[dim]?[/dim]",
}


def print_chats(chats: list[Chat]) -> None:
    table = Table(
        show_header=True,
        header_style="bold cyan",
        box=box.ROUNDED,
        border_style="dim",
        pad_edge=True,
        show_lines=False,
    )
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("", width=1)  # chat type icon
    table.add_column("Chat", width=30, no_wrap=True, overflow="ellipsis")
    table.add_column("Last Message", ratio=1, no_wrap=True, overflow="ellipsis")
    table.add_column("From", width=14, no_wrap=True)
    table.add_column("Time", width=9, no_wrap=True, justify="right")
    table.add_column("", width=3)  # unread badge

    for chat in chats:
        # Unread badge
        if chat.unread_count > 1:
            badge = f"[bold white on blue] {chat.unread_count} [/bold white on blue]"
        elif chat.unread_count == 1:
            badge = "[bold cyan]*[/bold cyan]"
        else:
            badge = ""

        row_style = "bold" if chat.unread_count > 0 else ""
        icon = _CHAT_ICONS.get(chat.chat_type, "\u2502")
        num = str(chat.display_num) if chat.display_num else ""
        title = _truncate(chat.display_title, 28)
        preview = _truncate(chat.last_message_preview, 50)
        sender = _truncate(chat.last_message_sender, 12)

        table.add_row(
            num,
            f"[dim]{icon}[/dim]",
            title,
            preview,
            sender,
            _format_date(chat.last_message_time),
            badge,
            style=row_style,
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


def print_meetings(meetings: list[Meeting]) -> None:
    table = Table(
        show_header=True,
        header_style="bold cyan",
        box=box.ROUNDED,
        border_style="dim",
        pad_edge=True,
        show_lines=False,
    )
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Subject", min_width=25, no_wrap=True, overflow="ellipsis")
    table.add_column("Organizer", width=18, no_wrap=True)
    table.add_column("Start", width=16, no_wrap=True)
    table.add_column("End", width=7, no_wrap=True)
    table.add_column("Attendees", width=5, justify="right")

    for m in meetings:
        num = str(m.display_num) if m.display_num else ""
        subject = _truncate(m.subject, 35)
        organizer = _truncate(m.organizer, 16)
        start = _format_meeting_time(m.start_time)
        end = _format_meeting_end(m.start_time, m.end_time)
        att_count = str(len(m.attendees)) if m.attendees else ""

        table.add_row(num, subject, organizer, start, end, att_count)

    console.print(table)


def print_meeting_detail(meeting: Meeting) -> None:
    header = f"[bold]Subject:[/bold] {meeting.subject}"
    header += f"\n[bold]Organizer:[/bold] {meeting.organizer}"
    if meeting.organizer_email:
        header += f" <{meeting.organizer_email}>"
    start_local = meeting.start_time.astimezone(datetime.now().astimezone().tzinfo)
    end_local = meeting.end_time.astimezone(datetime.now().astimezone().tzinfo)
    header += f"\n[bold]Start:[/bold] {start_local.strftime('%Y-%m-%d %H:%M')}"
    header += f"\n[bold]End:[/bold] {end_local.strftime('%Y-%m-%d %H:%M')}"
    if meeting.location:
        header += f"\n[bold]Location:[/bold] {meeting.location}"
    if meeting.join_url:
        header += f"\n[bold]Join URL:[/bold] {meeting.join_url}"

    console.print(Panel(header, title=f"Meeting #{meeting.display_num}", border_style="cyan"))

    if meeting.attendees:
        console.print()
        console.print(f"[bold]Attendees ({len(meeting.attendees)}):[/bold]")
        for a in meeting.attendees:
            console.print(f"  {a}")


def print_recordings(recordings: list[Recording]) -> None:
    has_names = any(r.name for r in recordings)
    table = Table(
        show_header=True,
        header_style="bold cyan",
        box=box.ROUNDED,
        border_style="dim",
        pad_edge=True,
    )
    table.add_column("#", style="dim", width=4, justify="right")
    if has_names:
        table.add_column("Name", min_width=30, no_wrap=True, overflow="ellipsis")
        table.add_column("Size", width=10, justify="right")
    table.add_column("Created", min_width=16)

    for i, rec in enumerate(recordings, 1):
        created = _format_meeting_time(rec.created_time)
        if has_names:
            table.add_row(str(i), _truncate(rec.name, 40), _format_file_size(rec.size), created)
        else:
            table.add_row(str(i), created)

    console.print(table)


def print_transcripts(transcripts: list[Transcript]) -> None:
    has_names = any(t.name for t in transcripts)
    table = Table(
        show_header=True,
        header_style="bold cyan",
        box=box.ROUNDED,
        border_style="dim",
        pad_edge=True,
    )
    table.add_column("#", style="dim", width=4, justify="right")
    if has_names:
        table.add_column("Name", min_width=30, no_wrap=True, overflow="ellipsis")
        table.add_column("Size", width=10, justify="right")
    table.add_column("Created", min_width=16)

    for i, tr in enumerate(transcripts, 1):
        created = _format_meeting_time(tr.created_time)
        if has_names:
            table.add_row(str(i), _truncate(tr.name, 40), _format_file_size(tr.size), created)
        else:
            table.add_row(str(i), created)

    console.print(table)


def print_users(users: list[User]) -> None:
    table = Table(
        show_header=True,
        header_style="bold cyan",
        box=box.ROUNDED,
        border_style="dim",
        pad_edge=True,
    )
    table.add_column("#", style="dim", width=5, justify="right")
    table.add_column("Name", min_width=25)
    table.add_column("Email", min_width=30)
    table.add_column("Type", width=10)

    for i, user in enumerate(users, 1):
        table.add_row(str(i), user.display_name, user.email, user.user_type)

    console.print(table)


def print_status(resp: dict) -> None:
    """Print presence status with colored indicator."""
    availability = resp.get("availability", "Unknown")
    activity = resp.get("activity", "")
    status_msg = resp.get("statusMessage", {}).get("message", {}).get("content", "")

    dot = STATUS_DOTS.get(availability, STATUS_DOTS["Unknown"])
    color = STATUS_COLORS.get(availability, "dim")

    console.print(f"  {dot} [bold {color}]{availability}[/bold {color}]")
    if activity and activity != availability:
        console.print(f"    [dim]Activity:[/dim] {activity}")
    if status_msg:
        console.print(f"    [dim]Message:[/dim]  {status_msg}")


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


def _format_meeting_time(dt: datetime) -> str:
    if dt.year == 1:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_local = dt.astimezone(datetime.now().astimezone().tzinfo)
    now_local = datetime.now().astimezone()
    if dt_local.date() == now_local.date():
        return f"Today {dt_local.strftime('%H:%M')}"
    if (dt_local.date() - now_local.date()).days == 1:
        return f"Tomorrow {dt_local.strftime('%H:%M')}"
    if (now_local.date() - dt_local.date()).days == 1:
        return f"Yesterday {dt_local.strftime('%H:%M')}"
    return dt_local.strftime("%d %b %H:%M")


def _format_meeting_end(start: datetime, end: datetime) -> str:
    if end.year == 1:
        return ""
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start_local = start.astimezone(datetime.now().astimezone().tzinfo)
    end_local = end.astimezone(datetime.now().astimezone().tzinfo)
    if start_local.date() == end_local.date():
        return end_local.strftime("%H:%M")
    return end_local.strftime("%d %b %H:%M")


def _format_file_size(size_bytes: int) -> str:
    if not size_bytes:
        return ""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


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
