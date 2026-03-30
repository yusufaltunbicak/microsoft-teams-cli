"""Auth commands: login, whoami."""

from __future__ import annotations

import click

from ..auth import get_auth_status, login as do_login, login_with_token, verify_tokens
from ..formatter import console, print_error, print_success
from ..serialization import to_json
from ._common import _get_client, _handle_api_error, should_json


def register(cli: click.Group) -> None:
    cli.add_command(login)
    cli.add_command(whoami)
    cli.add_command(auth_status)


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

    if with_token:
        import sys

        raw_input = sys.stdin.read().strip()
        if not raw_input:
            raise click.UsageError("No token provided via stdin.")
        effective_region = region or os.environ.get("TEAMS_REGION", "emea")
        try:
            tokens = login_with_token(raw_input, region=effective_region)
        except ValueError as exc:
            raise click.UsageError(str(exc)) from exc
    else:
        if region:
            raise click.UsageError("--region is only used with --with-token.")
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
        raise click.ClickException("Login completed but token verification failed.")


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


@click.command(name="auth-status")
@click.option("--check", is_flag=True, help="Verify the active IC3 token with a live API call")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def auth_status(check: bool, as_json: bool):
    """Inspect auth/cache state without triggering login."""
    data = get_auth_status(check=check)

    if should_json(as_json):
        click.echo(to_json(data))
        return

    cache = data["cache"]
    identity = data["identity"]
    tokens = data["tokens"]
    ic3 = data["ic3"]

    console.print(f"[bold]Auth Source:[/bold] {data['auth_source']}")
    console.print(f"[bold]Token Cache:[/bold] {'yes' if cache['token_cache_exists'] else 'no'}")
    console.print(f"[bold]Browser State:[/bold] {'yes' if cache['browser_state_exists'] else 'no'}")
    console.print(f"[bold]Region:[/bold] {identity.get('region') or 'N/A'}")
    console.print(f"[bold]User ID:[/bold] {identity.get('user_id') or 'N/A'}")
    console.print(f"[bold]Display Name:[/bold] {identity.get('display_name') or 'N/A'}")
    console.print(f"[bold]IC3:[/bold] {'present' if tokens.get('ic3') else 'missing'}")
    console.print(f"[bold]Graph:[/bold] {'present' if tokens.get('graph') else 'missing'}")
    console.print(f"[bold]Presence:[/bold] {'present' if tokens.get('presence') else 'missing'}")
    console.print(f"[bold]CSA:[/bold] {'present' if tokens.get('csa') else 'missing'}")
    console.print(f"[bold]Substrate:[/bold] {'present' if tokens.get('substrate') else 'missing'}")
    if ic3.get("expires_at"):
        console.print(f"[bold]IC3 Expires At:[/bold] {ic3['expires_at']}")
    if ic3.get("expires_in_human"):
        console.print(f"[bold]IC3 Expires In:[/bold] {ic3['expires_in_human']}")
    if check:
        valid = ic3.get("valid")
        if valid is None:
            console.print("[bold]IC3 Check:[/bold] unavailable")
        else:
            console.print(f"[bold]IC3 Check:[/bold] {'valid' if valid else 'invalid'}")
