"""Local scheduled message tracking for Teams CLI.

Teams doesn't have a native scheduled send API, so we track
pending messages locally and send them via `teams schedule-run`.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from .constants import CACHE_DIR, SCHEDULED_FILE


def load_scheduled() -> list[dict]:
    if SCHEDULED_FILE.exists():
        try:
            SCHEDULED_FILE.chmod(0o600)
            return json.loads(SCHEDULED_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return []


def save_scheduled(entries: list[dict]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=CACHE_DIR, delete=False) as handle:
            os.fchmod(handle.fileno(), 0o600)
            temporary = Path(handle.name)
            json.dump(entries, handle, indent=2)
        os.replace(temporary, SCHEDULED_FILE)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def add_scheduled(
    conv_id: str,
    content: str,
    send_at: str,
    chat_title: str = "",
) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {
        "conv_id": conv_id,
        "content": content,
        "send_at": send_at,
        "chat_title": chat_title,
        "created_at": now,
        "status": "pending",
    }
    entries = load_scheduled()
    entries.append(entry)
    save_scheduled(entries)
    return entry


def cancel_scheduled(index: int) -> dict | None:
    entries = load_scheduled()
    if index < 1 or index > len(entries):
        return None
    entries[index - 1]["status"] = "cancelled"
    entries[index - 1]["cancelled_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    save_scheduled(entries)
    return entries[index - 1]


def get_pending() -> list[dict]:
    """Get entries that are due to be sent."""
    now = datetime.now(timezone.utc)
    entries = load_scheduled()
    pending = []
    for entry in entries:
        if entry.get("status") != "pending":
            continue
        try:
            send_at = datetime.fromisoformat(entry["send_at"].replace("Z", "+00:00"))
            if send_at <= now:
                pending.append(entry)
        except (ValueError, KeyError):
            pass
    return pending


def mark_sent(index: int) -> None:
    """Mark a scheduled entry as sent (0-based index)."""
    entries = load_scheduled()
    if 0 <= index < len(entries):
        entries[index]["status"] = "sent"
        entries[index]["sent_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        save_scheduled(entries)
