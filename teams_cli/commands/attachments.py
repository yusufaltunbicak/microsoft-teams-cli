"""Attachment commands: attachments."""

from __future__ import annotations

import click

from ..formatter import console, print_error, print_success
from ..serialization import to_json
from ._common import _format_size, _get_client, _handle_api_error, should_json


def register(cli: click.Group) -> None:
    cli.add_command(attachments)


@click.command()
@click.argument("msg_num")
@click.option("-d", "--download", is_flag=True, help="Download all attachments")
@click.option("--save-to", type=click.Path(), default=".", help="Download directory")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@_handle_api_error
def attachments(msg_num: str, download: bool, save_to: str, as_json: bool):
    """List or download attachments/images from a message."""
    from pathlib import Path

    client = _get_client()
    atts = client.get_attachments(msg_num)

    if not atts:
        print_success("No attachments.")
        return

    # JSON output (but don't skip download if requested)
    if should_json(as_json):
        click.echo(to_json(atts))
    else:
        from rich.table import Table
        table = Table(show_header=True, header_style="bold cyan", box=None)
        table.add_column("#", width=4, justify="right")
        table.add_column("Name", min_width=25)
        table.add_column("Type", width=15)
        table.add_column("Size", width=10, justify="right")
        table.add_column("", width=8)

        for i, att in enumerate(atts, 1):
            size_str = _format_size(att.size) if att.size else ""
            tag = "[dim]inline[/dim]" if att.is_inline else ""
            table.add_row(str(i), att.name, att.content_type, size_str, tag)

        console.print(table)

    if download:
        save_path = Path(save_to)
        save_path.mkdir(parents=True, exist_ok=True)
        for att in atts:
            try:
                data = client.download_attachment(att)
                file_path = save_path / att.name
                file_path.write_bytes(data)
                print_success(f"  Saved: {file_path} ({_format_size(len(data))})")
            except Exception as e:
                print_error(f"  Failed: {att.name} — {e}")
