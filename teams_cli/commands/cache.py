"""Explicit local history synchronization and cache management."""
from __future__ import annotations

import time

import click

from .. import constants
from ..formatter import console, print_success
from ..history import HistoryIndex, account_key, saved_account, sync_history
from ..serialization import to_json
from ._common import (_get_client, _handle_api_error, should_json, emit_dry_run,
                      require_confirmation, should_skip_confirmation)


def register(cli: click.Group):
    cli.add_command(sync)
    cli.add_command(cache)


@click.command()
@click.option("--chats", default=50, type=click.IntRange(1, 1000), show_default=True)
@click.option("--days", default=60, type=click.IntRange(1, 3650), show_default=True)
@click.option("--max-pages", default=5, type=click.IntRange(1, 1000), show_default=True,
              help="Maximum history pages per chat (100 messages/page).")
@click.option("--watch", default=0, type=click.IntRange(0, 86400),
              help="Repeat in the foreground every N seconds (minimum 60); Ctrl-C stops.")
@click.option("--json", "as_json", is_flag=True)
@_handle_api_error
def sync(chats: int, days: int, max_pages: int, watch: int, as_json: bool):
    """Index recent chat history locally using read-only Teams requests."""
    if watch and watch < 60:
        raise click.BadParameter("--watch must be at least 60 seconds.")
    while True:
        client = _get_client()
        with HistoryIndex(account_key(client._tokens)) as index:
            result = sync_history(client, index, chats=chats, days=days, max_pages=max_pages)
        if should_json(as_json):
            click.echo(to_json(result))
        else:
            print_success(f"Indexed {result['indexed']} messages across {result['chats']} chats.")
            console.print(f"[dim]{result['path']} — local coverage; use cache status for limits.[/dim]")
        if not watch:
            break
        time.sleep(watch)


@click.group()
def cache():
    """Inspect or remove private local caches; keeps auth and schedules."""


@cache.command(name="status")
@click.option("--json", "as_json", is_flag=True)
@_handle_api_error
def cache_status(as_json: bool):
    """Show indexed scope and freshness without contacting Teams."""
    path = constants.CACHE_DIR / "history.sqlite3"
    if path.exists():
        with HistoryIndex(saved_account()) as index:
            result = index.status()
    else:
        result = {"path": str(path), "messages": 0, "chats": 0, "coverage": [],
                  "scope": "indexed chats only", "complete": False, "live": False}
    result["metadata_path"] = str(constants.CACHE_DIR / "metadata.json")
    if should_json(as_json):
        click.echo(to_json(result))
    else:
        console.print(f"{result['messages']} messages, {result['chats']} chats — local index")
        console.print(f"{result['path']}")
        for row in result["coverage"]:
            state = "covered" if row["complete"] else (row.get("reason") or "partial")
            console.print(f"{row['title']}: {state}; last sync {row['synced_at']}", markup=False)


@cache.command(name="clear")
@click.option("--messages-only", is_flag=True, help="Remove message index; keep metadata.")
@click.option("--metadata-only", is_flag=True, help="Reset names/capabilities; keep message history.")
@click.option("--yes", "-y", is_flag=True, help="Skip local deletion confirmation.")
@click.option("--json", "as_json", is_flag=True)
@_handle_api_error
def cache_clear(messages_only: bool, metadata_only: bool, yes: bool, as_json: bool):
    """Delete local message/metadata files for all cached accounts."""
    if messages_only and metadata_only:
        raise click.UsageError("Choose only one of --messages-only and --metadata-only.")
    names = ["history.sqlite3", "history.sqlite3-journal", "history.sqlite3-wal", "history.sqlite3-shm"]
    if metadata_only:
        names = []
    if not messages_only:
        names.append("metadata.json")
    if emit_dry_run("clear local cache", {"files": names}, as_json=as_json):
        return
    if not should_skip_confirmation(yes):
        require_confirmation("Delete local caches for all accounts?", "clear local cache")
    removed = 0
    for name in names:
        path = constants.CACHE_DIR / name
        if path.exists():
            path.unlink()
            removed += 1
    if should_json(as_json):
        click.echo(to_json({"removed_files": removed}))
    else:
        print_success(f"Removed {removed} local cache files.")
