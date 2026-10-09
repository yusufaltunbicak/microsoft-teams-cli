"""Private, account-scoped name/title cache. Message bodies never enter this file."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None


class MetadataCache:
    USER_TTL = 7 * 24 * 60 * 60
    CHAT_TTL = 24 * 60 * 60
    MAX_USERS = 5000
    MAX_CHATS = 2000

    def __init__(self, directory: Path, account: str, enabled: bool = True):
        self.path = directory / "metadata.json"
        self.scope = hashlib.sha256(account.encode()).hexdigest()[:32]
        self.enabled = enabled
        self._mutex = threading.RLock()
        self._data = self._read()

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text())
            if value.get("version") == 1 and isinstance(value.get("accounts"), dict):
                return value
        except (OSError, ValueError, AttributeError):
            pass
        return {"version": 1, "accounts": {}}

    def get(self, section: str, key: str) -> dict | None:
        if not self.enabled:
            return None
        with self._mutex:
            entry = self._data.get("accounts", {}).get(self.scope, {}).get(section, {}).get(key)
            if not isinstance(entry, dict) or entry.get("expires_at", 0) <= time.time():
                return None
            return {k: v for k, v in entry.items() if k != "expires_at"}

    def items(self, section: str) -> dict[str, dict]:
        with self._mutex:
            entries = self._data.get("accounts", {}).get(self.scope, {}).get(section, {})
            return {key: value for key in entries if (value := self.get(section, key)) is not None}

    def put_many(self, section: str, entries: dict[str, dict], ttl: float | None = None) -> None:
        if not self.enabled or not entries:
            return
        if section not in ("users", "chats"):
            raise ValueError("Metadata cache accepts only users and chats")
        # Whitelist metadata fields rather than trusting callers to exclude message text.
        allowed = {"display_name"} if section == "users" else {"title", "topic", "members", "chat_type"}
        expires = time.time() + (ttl if ttl is not None else (self.USER_TTL if section == "users" else self.CHAT_TTL))
        with self._mutex, self._file_lock():
            data = self._read()
            account = data["accounts"].setdefault(self.scope, {})
            target = account.setdefault(section, {})
            for key, value in entries.items():
                target[key] = {k: v for k, v in value.items() if k in allowed}
                target[key]["expires_at"] = expires
            limit = self.MAX_USERS if section == "users" else self.MAX_CHATS
            now = time.time()
            target = {k: v for k, v in target.items() if isinstance(v, dict) and v.get("expires_at", 0) > now}
            account[section] = dict(sorted(target.items(), key=lambda pair: pair[1]["expires_at"], reverse=True)[:limit])
            self._write(data)
            self._data = data

    @contextmanager
    def _file_lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.path.with_suffix(".json.lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a+") as lock:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _write(self, data: dict) -> None:
        path = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=self.path.parent, prefix="metadata.", suffix=".tmp", delete=False) as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(data, handle, ensure_ascii=False)
                path = Path(handle.name)
            os.replace(path, self.path)
        finally:
            if path and path.exists():
                path.unlink()
