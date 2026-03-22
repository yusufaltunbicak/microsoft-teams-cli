"""Meeting commands: meetings, recordings, transcripts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import click

from ..formatter import (
    console,
    print_error,
    print_meeting_detail,
    print_meetings,
    print_recordings,
    print_success,
    print_transcripts,
)
from ..serialization import to_json
from ._common import _get_client, _handle_api_error, should_json


def register(cli: click.Group) -> None:
    cli.add_command(meetings)
    cli.add_command(recordings)
    cli.add_command(transcripts)


@click.command()
@click.option("--past", is_flag=True, help="Show past meetings instead of upcoming")
@click.option("--today", is_flag=True, help="Show only today's meetings")
@click.option("--days", default=7, type=int, help="Number of days to look ahead/back (default: 7)")
@click.option("--limit", default=20, type=int, help="Max meetings to show (default: 20)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@_handle_api_error
def meetings(past: bool, today: bool, days: int, limit: int, as_json: bool):
    """List upcoming or past Teams meetings from your calendar."""
    client = _get_client()
    now = datetime.now(timezone.utc)

    if today:
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timedelta(days=1)
        start_dt = start_of_day.isoformat()
        end_dt = end_of_day.isoformat()
    elif past:
        start_dt = (now - timedelta(days=days)).isoformat()
        end_dt = now.isoformat()
    else:
        start_dt = now.isoformat()
        end_dt = (now + timedelta(days=days)).isoformat()

    meeting_list = client.get_meetings(start_dt, end_dt, limit=limit)

    if should_json(as_json):
        click.echo(to_json(meeting_list))
    elif not meeting_list:
        label = "past" if past else "today's" if today else "upcoming"
        print_error(f"No {label} meetings found.")
    else:
        print_meetings(meeting_list)


@click.command()
@click.argument("meeting", type=str)
@click.option("--download", is_flag=True, help="Download recording to current directory")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@_handle_api_error
def recordings(meeting: str, download: bool, as_json: bool):
    """List or download recordings for a meeting.

    MEETING is the meeting number from 'teams meetings' output.
    """
    client = _get_client()
    recording_list = client.get_recordings(meeting)

    if should_json(as_json) and not download:
        click.echo(to_json(recording_list))
    elif not recording_list:
        print_error("No recordings found for this meeting.")
    elif download:
        for i, rec in enumerate(recording_list, 1):
            filename = rec.name if rec.name else f"recording_{meeting}_{i}.mp4"
            console.print(f"  Downloading recording {i}/{len(recording_list)}...")
            size = client.download_recording(meeting, rec.id, filename)
            print_success(f"Saved {filename} ({_format_size(size)})")
    else:
        print_recordings(recording_list)


@click.command()
@click.argument("meeting", type=str)
@click.option("--view", is_flag=True, help="Display transcript content in terminal")
@click.option("--download", "download_path", default=None, help="Save transcript to FILE")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@_handle_api_error
def transcripts(meeting: str, view: bool, download_path: str | None, as_json: bool):
    """List, view, or download transcripts for a meeting.

    MEETING is the meeting number from 'teams meetings' output.
    """
    client = _get_client()
    transcript_list = client.get_transcripts(meeting)

    if not transcript_list:
        if should_json(as_json):
            click.echo(to_json([]))
        else:
            print_error("No transcripts found for this meeting.")
        return

    if view or download_path:
        tr = transcript_list[0]
        content = client.get_transcript_content(meeting, tr.id)
        if download_path:
            with open(download_path, "w", encoding="utf-8") as f:
                f.write(content)
            print_success(f"Transcript saved to {download_path} ({_format_size(len(content.encode('utf-8')))})")
        else:
            click.echo(content)
    elif should_json(as_json):
        click.echo(to_json(transcript_list))
    else:
        print_transcripts(transcript_list)


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"
