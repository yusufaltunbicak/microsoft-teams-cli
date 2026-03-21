"""Presence commands: status, set-status."""

from __future__ import annotations

from datetime import datetime, timezone

import click

from ..formatter import console, print_error, print_status, print_success
from ..serialization import to_json
from ._common import _get_client, _handle_api_error, _parse_schedule_time, should_json


def register(cli: click.Group) -> None:
    cli.add_command(status)
    cli.add_command(set_status)


@click.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@_handle_api_error
def status(as_json: bool):
    """Show current presence status."""
    client = _get_client()
    try:
        resp = client.get_presence()
        if should_json(as_json):
            click.echo(to_json(resp))
        else:
            print_status(resp)
    except Exception as e:
        print_error(f"Could not get presence: {e}")


# Map CLI-friendly names to UPS API values
_STATUS_MAP = {
    "available": {"availability": "Available"},
    "busy": {"availability": "Busy"},
    "donotdisturb": {"availability": "DoNotDisturb"},
    "berightback": {"availability": "BeRightBack"},
    "away": {"availability": "Away"},
    "offline": {"availability": "Offline", "activity": "OffWork"},
}


@click.command(name="set-status")
@click.argument("availability", type=click.Choice([
    "Available", "Busy", "DoNotDisturb", "BeRightBack", "Away", "Offline",
], case_sensitive=False))
@click.option("--expiry", default=None, help="Duration: +30m, +1h, +4h")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@_handle_api_error
def set_status(availability: str, expiry: str | None, yes: bool):
    """Set presence status via Teams UPS API (same as Teams web client)."""
    if not yes:
        console.print(f"  [bold]Status:[/bold] {availability}")
        if expiry:
            console.print(f"  [bold]Expiry:[/bold] {expiry}")
        click.confirm("Set this status?", abort=True)

    client = _get_client()

    # Build UPS forceavailability payload
    payload: dict = dict(_STATUS_MAP.get(availability.lower(), {"availability": availability}))

    if expiry:
        dt = _parse_schedule_time(expiry)
        payload["desiredExpirationTime"] = dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    try:
        client._ups_put("/me/forceavailability/", payload)
        print_success(f"Status set to {availability}")
    except Exception as e:
        print_error(f"Could not set status: {e}")
