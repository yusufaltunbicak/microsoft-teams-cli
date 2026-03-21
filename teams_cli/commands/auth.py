"""Auth commands: login, whoami."""

from __future__ import annotations

import click

from ..auth import login as do_login, login_with_token, verify_tokens
from ..formatter import console, print_error, print_success
from ..serialization import to_json
from ._common import _get_client, _handle_api_error, should_json


def register(cli: click.Group) -> None:
    cli.add_command(login)
    cli.add_command(whoami)


@click.command()
@click.option("--force", is_flag=True, help="Force re-login, ignore saved session")
@click.option("--debug", is_flag=True, help="Show debug info about token extraction")
@click.option("--with-token", is_flag=True, help="Read token from stdin instead of browser")
@click.option("--region", default=None, help="Region (emea/amer/apac) for --with-token mode")
def login(force: bool, debug: bool, with_token: bool, region: str | None):
    """Authenticate via browser and cache tokens.

    \b
    With --with-token, reads token(s) from stdin (useful for CI/CD):
      echo $TOKEN | teams login --with-token
      echo $TOKEN | teams login --with-token --region amer
      cat tokens.json | teams login --with-token
    """
    import os
    import sys

    try:
        if with_token:
            raw_input = sys.stdin.read().strip()
            if not raw_input:
                print_error("No token provided via stdin.")
                sys.exit(1)
            effective_region = region or os.environ.get("TEAMS_REGION", "emea")
            tokens = login_with_token(raw_input, region=effective_region)
        else:
            if region:
                print_error("--region is only used with --with-token.")
                sys.exit(1)
            tokens = do_login(force=force, debug=debug)

        if verify_tokens(tokens):
            print_success("Logged in successfully. Tokens cached.")
            console.print(f"  [dim]Region: {tokens.get('region', 'N/A')}[/dim]")
            console.print(f"  [dim]User ID: {tokens.get('user_id', 'N/A')[:12]}...[/dim]")
            if with_token:
                optional = ["graph", "presence", "csa", "substrate"]
                present = sum(1 for k in optional if tokens.get(k))
                console.print(f"  [dim]Tokens: ic3 + {present}/{len(optional)} optional[/dim]")
        else:
            print_error("Login completed but token verification failed.")
    except (RuntimeError, ValueError) as e:
        print_error(str(e))
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
