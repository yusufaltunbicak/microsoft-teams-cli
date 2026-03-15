"""Auth commands: login, whoami."""

from __future__ import annotations

import click

from ..auth import login as do_login, verify_tokens
from ..formatter import console, print_error, print_success
from ..serialization import to_json
from ._common import _get_client, _handle_api_error, should_json


def register(cli: click.Group) -> None:
    cli.add_command(login)
    cli.add_command(whoami)


@click.command()
@click.option("--force", is_flag=True, help="Force re-login, ignore saved session")
@click.option("--debug", is_flag=True, help="Show debug info about token extraction")
def login(force: bool, debug: bool):
    """Authenticate via browser and cache tokens."""
    try:
        tokens = do_login(force=force, debug=debug)
        if verify_tokens(tokens):
            print_success("Logged in successfully. Tokens cached.")
            console.print(f"  [dim]Region: {tokens.get('region', 'N/A')}[/dim]")
            console.print(f"  [dim]User ID: {tokens.get('user_id', 'N/A')[:12]}...[/dim]")
        else:
            print_error("Login completed but token verification failed.")
    except RuntimeError as e:
        print_error(str(e))
        import sys
        sys.exit(1)


@click.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@_handle_api_error
def whoami(as_json: bool):
    """Show current user info."""
    from ..formatter import print_whoami

    client = _get_client()
    data = client.get_me()
    data["region"] = client._region
    data["user_id"] = client._user_id
    if should_json(as_json):
        click.echo(to_json(data))
    else:
        print_whoami(data)
