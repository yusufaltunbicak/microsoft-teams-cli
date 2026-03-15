"""Teams & channel commands: teams, channels."""

from __future__ import annotations

import click

from ..formatter import print_channels, print_success, print_teams
from ..serialization import to_json
from ._common import _get_client, _handle_api_error, should_json


def register(cli: click.Group) -> None:
    cli.add_command(teams_list)
    cli.add_command(channels)


@click.command(name="teams")
@click.option("--offset", default=0, type=int, help="Skip first N items")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@_handle_api_error
def teams_list(offset: int, as_json: bool):
    """List joined teams."""
    client = _get_client()
    team_list = client.get_joined_teams()
    if offset:
        team_list = team_list[offset:]

    if should_json(as_json):
        click.echo(to_json(team_list))
    else:
        if not team_list:
            print_success("No teams found.")
        else:
            print_teams(team_list)


@click.command()
@click.argument("team_num")
@click.option("--offset", default=0, type=int, help="Skip first N items")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@_handle_api_error
def channels(team_num: str, offset: int, as_json: bool):
    """List channels in a team."""
    client = _get_client()
    channel_list = client.get_channels(team_num)
    if offset:
        channel_list = channel_list[offset:]

    if should_json(as_json):
        click.echo(to_json(channel_list))
    else:
        if not channel_list:
            print_success(f"No channels in team #{team_num}.")
        else:
            print_channels(channel_list)
