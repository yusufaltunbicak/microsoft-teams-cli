"""Private, account-scoped local message index. No network access on reads."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, unquote

from . import constants
from .exceptions import AuthRequiredError, ResourceNotFoundError
from .models import Message


def normalize(value: str) -> str:
    value = value.casefold().replace("ı", "i")
    return "".join(c for c in unicodedata.normalize("NFD", value) if not unicodedata.combining(c))


def account_key(tokens: dict) -> str:
    from .msal_cache import claims
    data = claims(tokens.get("ic3", "")) if isinstance(tokens, dict) else {}
    uid = data.get("oid")
    tenant = data.get("tid")
    if not uid or not tenant:
        raise AuthRequiredError("No saved account. Run: teams login")
    return hashlib.sha256(f"{tenant}:{uid}".encode()).hexdigest()


def saved_account() -> str:
    """Identify a saved account even offline or after access-token expiry."""
    return account_key(saved_tokens())


def saved_tokens() -> dict:
    """Read one account snapshot; this never renews credentials."""
    env = os.environ.get("TEAMS_IC3_TOKEN")
    if env:
        return {"ic3": env}
    try:
        tokens = json.loads(constants.TOKENS_FILE.read_text())
        account_key(tokens)
        return tokens
    except (OSError, ValueError, TypeError) as exc:
        raise AuthRequiredError("No saved account for local search. Run: teams login") from exc


def number_messages(messages: list[Message], tokens: dict | None = None) -> None:
    """Reuse the existing locked ID-map writer without login or network calls."""
    from .client import TeamsClient
    tokens = tokens if tokens is not None else saved_tokens()
    client = TeamsClient(tokens)
    try:
        client._assign_message_nums(messages)
    finally:
        client._session.close()


class HistoryIndex:
    def __init__(self, account: str, path: Path | None = None):
        self.account = account
        self.path = path or constants.CACHE_DIR / "history.sqlite3"
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        # Create securely before sqlite opens it; rollback journal inherits 0600.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                account TEXT NOT NULL, conv TEXT NOT NULL, msg TEXT NOT NULL,
                sender TEXT NOT NULL, sender_id TEXT NOT NULL, title TEXT NOT NULL,
                timestamp TEXT NOT NULL, text TEXT NOT NULL, search_text TEXT NOT NULL,
                is_from_me INTEGER NOT NULL, PRIMARY KEY(account,conv,msg));
            CREATE INDEX IF NOT EXISTS chronology ON messages(account,conv,timestamp,msg);
            CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(search_text,
                content='messages', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2');
            CREATE TRIGGER IF NOT EXISTS message_insert AFTER INSERT ON messages BEGIN
                INSERT INTO message_fts(rowid,search_text) VALUES(new.rowid,new.search_text); END;
            CREATE TRIGGER IF NOT EXISTS message_delete AFTER DELETE ON messages BEGIN
                INSERT INTO message_fts(message_fts,rowid,search_text)
                VALUES('delete',old.rowid,old.search_text); END;
            CREATE TRIGGER IF NOT EXISTS message_update AFTER UPDATE ON messages BEGIN
                INSERT INTO message_fts(message_fts,rowid,search_text)
                VALUES('delete',old.rowid,old.search_text);
                INSERT INTO message_fts(rowid,search_text) VALUES(new.rowid,new.search_text); END;
            CREATE TABLE IF NOT EXISTS coverage (
                account TEXT NOT NULL, conv TEXT NOT NULL, title TEXT NOT NULL,
                synced_at TEXT NOT NULL, requested_after TEXT NOT NULL,
                complete INTEGER NOT NULL, oldest TEXT, newest TEXT,
                PRIMARY KEY(account,conv));
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def upsert(self, messages: list[Message], title: str = "") -> int:
        rows = [(self.account, m.conversation_id, m.id, m.sender, m.sender_id,
                 title or m.chat_title, m.timestamp.astimezone(timezone.utc).isoformat(),
                 m.text_content, normalize(m.text_content), int(m.is_from_me)) for m in messages]
        self.db.executemany("""INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account,conv,msg) DO UPDATE SET sender=excluded.sender,
            sender_id=excluded.sender_id,title=excluded.title,timestamp=excluded.timestamp,
            text=excluded.text,search_text=excluded.search_text,is_from_me=excluded.is_from_me""", rows)
        self.db.commit()
        return len(rows)

    def record_coverage(self, conv: str, title: str, cutoff: datetime, complete: bool):
        bounds = self.db.execute("SELECT min(timestamp),max(timestamp) FROM messages WHERE account=? AND conv=?",
                                 (self.account, conv)).fetchone()
        self.db.execute("INSERT OR REPLACE INTO coverage VALUES(?,?,?,?,?,?,?,?)",
                        (self.account, conv, title, datetime.now(timezone.utc).isoformat(),
                         cutoff.isoformat(), int(complete), bounds[0], bounds[1]))
        self.db.commit()

    @staticmethod
    def _message(row) -> Message:
        return Message(id=row["msg"], conversation_id=row["conv"], sender=row["sender"],
                       sender_id=row["sender_id"], content=row["text"], text_content=row["text"],
                       message_type="Text", timestamp=datetime.fromisoformat(row["timestamp"]),
                       is_from_me=bool(row["is_from_me"]), chat_title=row["title"])

    def resolve_chat(self, value: str) -> str:
        if value.startswith(("19:", "48:", "28:")):
            return value
        try:
            mapping = json.loads(constants.ID_MAP_FILE.read_text()).get("chats", {})
            if value.lstrip("#") in mapping:
                return mapping[value.lstrip("#")]
        except (OSError, ValueError):
            pass
        rows = self.db.execute("SELECT conv,title FROM coverage WHERE account=?", (self.account,)).fetchall()
        q = normalize(value)
        exact = [r for r in rows if normalize(r["title"]) == q]
        matches = exact or [r for r in rows if q in normalize(r["title"])]
        if len(matches) == 1:
            return matches[0]["conv"]
        if matches:
            raise ValueError("Ambiguous local chat name; use the full title or conversation ID.")
        raise ResourceNotFoundError("Chat is not in the local index. Run: teams sync")

    def search(self, query: str, top: int = 25, offset: int = 0, chat: str | None = None,
               sender: str | None = None, after: str | None = None, before: str | None = None) -> list[Message]:
        terms = re.findall(r"\w+", normalize(query), flags=re.UNICODE)
        if not terms:
            raise ValueError("Local search needs at least one word.")
        clause = ' AND '.join('"' + term + '"*' for term in terms)
        where = ["message_fts MATCH ?", "m.account=?"]
        args: list = [clause, self.account]
        if chat:
            where.append("m.conv=?")
            args.append(self.resolve_chat(chat))
        if sender:
            # Python normalization registered as SQLite function keeps Turkish matching consistent.
            self.db.create_function("normalize_text", 1, normalize)
            where.append("(instr(normalize_text(m.sender),?)>0 OR m.sender_id=?)")
            args.extend([normalize(sender), sender])
        if after:
            where.append("m.timestamp>=?")
            args.append(date_bound(after).isoformat())
        if before:
            where.append("m.timestamp<?" if len(before) == 10 else "m.timestamp<=?")
            args.append(date_bound(before, end=True).isoformat())
        rows = self.db.execute("SELECT m.* FROM message_fts JOIN messages m ON m.rowid=message_fts.rowid WHERE "
                               + " AND ".join(where) + " ORDER BY m.timestamp DESC,m.msg DESC LIMIT ? OFFSET ?",
                               [*args, top, offset]).fetchall()
        return [self._message(r) for r in rows]

    def context(self, message: Message, count: int) -> list[Message]:
        params = (self.account, message.conversation_id, message.timestamp.isoformat(),
                  message.timestamp.isoformat(), message.id, count)
        before = self.db.execute("""SELECT * FROM messages WHERE account=? AND conv=?
            AND (timestamp<? OR (timestamp=? AND msg<?)) ORDER BY timestamp DESC,msg DESC LIMIT ?""", params).fetchall()
        after = self.db.execute("""SELECT * FROM messages WHERE account=? AND conv=?
            AND (timestamp>? OR (timestamp=? AND msg>?)) ORDER BY timestamp,msg LIMIT ?""", params).fetchall()
        return [self._message(r) for r in reversed(before)] + [message] + [self._message(r) for r in after]

    def status(self) -> dict:
        row = self.db.execute("SELECT count(*),min(timestamp),max(timestamp) FROM messages WHERE account=?",
                              (self.account,)).fetchone()
        coverage = [dict(r) for r in self.db.execute("SELECT title,synced_at,requested_after,complete,oldest,newest FROM coverage WHERE account=?",
                                                   (self.account,)).fetchall()]
        return {"path": str(self.path), "messages": row[0], "oldest": row[1], "newest": row[2],
                "chats": len(coverage), "coverage": coverage, "scope": "indexed chats only",
                "live": False, "complete": bool(coverage) and all(r["complete"] for r in coverage)}


def date_bound(value: str, end: bool = False) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if end and len(value) == 10:
        dt += timedelta(days=1)
    return dt.astimezone(timezone.utc)


def sync_history(client, index: HistoryIndex, chats: int = 50, days: int = 60, max_pages: int = 5) -> dict:
    """Bounded IC3 history walk; only follows same-origin server pagination links."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    chat_list = client.get_chats(top=chats)
    fetched = pages = 0
    for chat in chat_list:
        path = f"/users/ME/conversations/{chat.id}/messages"
        params = {"view": "msnp24Equivalent|supportsMessageProperties", "pageSize": 100}
        seen: set[str] = set()
        complete = False
        for _ in range(max_pages):
            response = client._ic3_get(path, params=params)
            pages += 1
            if not isinstance(response, dict) or not isinstance(response.get("messages"), list):
                # A missing/vanished conversation must never look fully indexed.
                break
            raw = response.get("messages", [])
            # Include tombstones so a repeated sync can remove locally cached deletions.
            with index.db:
                for item in raw:
                    if item.get("properties", {}).get("deletetime") or item.get("messagetype") == "Control/MessageDelete":
                        index.db.execute("DELETE FROM messages WHERE account=? AND conv=? AND msg=?",
                                         (index.account, chat.id, str(item.get("id", ""))))
            messages = [Message.from_api(m, my_user_id=client._user_id) for m in raw
                        if m.get("messagetype") in ("Text", "RichText/Html", "RichText")
                        and not m.get("properties", {}).get("deletetime")]
            for m in messages:
                m.conversation_id = chat.id
            fetched += index.upsert([m for m in messages if m.timestamp >= cutoff], chat.display_title)
            link = response.get("_metadata", {}).get("backwardLink")
            times = [date_bound(m.get("composetime", m.get("originalarrivaltime", "")))
                     for m in raw if m.get("composetime") or m.get("originalarrivaltime")]
            if not link or (times and min(times) <= cutoff):
                complete = True
                break
            if link in seen:
                break
            seen.add(link)
            url = urlsplit(link)
            base = urlsplit(client._chatsvc)
            expected = base.path + f"/users/ME/conversations/{chat.id}/messages"
            if url.scheme != base.scheme or url.netloc != base.netloc or unquote(url.path) != expected:
                raise ValueError("Refusing an unexpected history pagination URL.")
            path = url.path[len(base.path):]
            params = {k: v[0] for k, v in parse_qs(url.query).items()}
        index.record_coverage(chat.id, chat.display_title, cutoff, complete)
    return {"indexed": fetched, "pages": pages, **index.status()}
