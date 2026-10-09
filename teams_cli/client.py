from __future__ import annotations

import base64
import json
import os
import random
import secrets
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import escape
from pathlib import Path
from typing import Iterator

import httpx

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

from .anti_detection import BrowserSession
from .constants import (
    CACHE_DIR,
    CHATSVC_BASE,
    GRAPH_BASE,
    ID_MAP_FILE,
    MT_BASE,
    SUBSTRATE_SEARCH_BASE,
    UPS_BASE,
    USER_AGENT,
)
from .exceptions import (
    ApiError,
    AuthRequiredError,
    RateLimitError,
    ResourceNotFoundError,
    RetryableError,
    TokenExpiredError,
)
from .models import Attachment, Chat, Message, User
from .metadata_cache import MetadataCache


class TeamsClient:
    """HTTP client for Teams Chat Service, Middle Tier, and Graph APIs."""

    MAX_ID_MAP_SIZE = 500
    MAX_RETRIES_429 = 3
    MAX_RETRIES_5XX = 1
    CIRCUIT_BREAKER_THRESHOLD = 5
    CIRCUIT_BREAKER_RESET_SECONDS = 30

    def __init__(self, tokens: dict[str, str]):
        self._tokens = tokens
        self._ic3 = tokens.get("ic3", "")
        self._graph = tokens.get("graph", "")
        self._presence_token = tokens.get("presence", "")

        self._substrate = tokens.get("substrate", "")
        self._region = tokens.get("region", "emea")
        self._user_id = tokens.get("user_id", "")
        self._user_mri = f"8:orgid:{self._user_id}"
        self._display_name = ""

        # Resolve base URLs
        self._chatsvc = CHATSVC_BASE.format(region=self._region)
        self._mt = MT_BASE.format(region=self._region)
        self._ups = UPS_BASE.format(region=self._region)

        # Anti-detection session
        from .config import load_config
        _cfg = load_config()
        self._session = BrowserSession(
            timeout=_cfg.get("timeout", 30),
            read_jitter_base=_cfg.get("jitter", {}).get("read_base", 0.3),
            write_jitter_base=_cfg.get("jitter", {}).get("write_base", 2.0),
        )
        self._circuit_breaker = _CircuitBreaker(
            threshold=self.CIRCUIT_BREAKER_THRESHOLD,
            reset_seconds=self.CIRCUIT_BREAKER_RESET_SECONDS,
        )

        # Cache for user display name lookups (user_id -> display_name)
        self._user_name_cache: dict[str, str] = {}
        self._sender_email_cache: dict[str, str] = {}
        self._metadata = MetadataCache(
            CACHE_DIR,
            f"{self._get_tenant_id()}:{self._user_id}:{self._region}",
            enabled=_cfg.get("cache", {}).get("metadata", True),
        )
        # A single command (notably summary) may ask for the same list twice.
        # Coalesce those reads without retaining conversation previews on disk.
        self._conversations_lock = threading.RLock()
        self._conversations_snapshot: list[dict] = []
        self._conversations_snapshot_at = 0.0
        self._conversations_snapshot_size = 0

        # ID mapping: two-level (chats and messages)
        self._id_map: dict = self._load_id_map()
        if "chats" not in self._id_map:
            self._id_map["chats"] = {}
        if "messages" not in self._id_map:
            self._id_map["messages"] = {}
        self._next_chat_num = max(
            (int(k) for k in self._id_map["chats"] if k.isdigit()), default=0
        ) + 1
        self._next_msg_num = max(
            (int(k) for k in self._id_map["messages"] if k.isdigit()), default=0
        ) + 1

    def _get_tenant_id(self) -> str:
        """Extract tenant ID from substrate or IC3 JWT token."""
        for token in (self._substrate, self._ic3):
            if not token:
                continue
            try:
                payload = token.split(".")[1]
                payload += "=" * (4 - len(payload) % 4)
                data = json.loads(base64.b64decode(payload))
                tid = data.get("tid", "")
                if tid:
                    return tid
            except (ValueError, KeyError, IndexError):
                continue
        return ""

    # ------------------------------------------------------------------
    # User info
    # ------------------------------------------------------------------

    def get_me(self) -> dict:
        """Get current user info."""
        from .auth import _decode_display_name
        name = _decode_display_name(self._ic3)
        if name:
            self._display_name = name

        # Try Graph /me for clean user info
        try:
            resp = self._graph_get("/me")
            result = {
                "displayName": resp.get("displayName", name),
                "mail": resp.get("mail", resp.get("userPrincipalName", "")),
                "jobTitle": resp.get("jobTitle", ""),
                "officeLocation": resp.get("officeLocation", ""),
            }
        except (httpx.HTTPStatusError, TokenExpiredError, RateLimitError):
            result = {"displayName": name}

        result["user_id"] = self._user_id
        result["region"] = self._region
        return result

    # ------------------------------------------------------------------
    # Chats
    # ------------------------------------------------------------------

    def get_chats(self, top: int = 25, unread_only: bool = False, skip: int = 0,
                  refresh: bool = False, assign_nums: bool = True) -> list[Chat]:
        """Get recent conversations from IC3 Chat Service."""
        fetch_size = max((top + skip) * 2, 200)
        conversations = self._get_conversations(fetch_size, refresh=refresh)

        chats = []
        for conv in conversations:
            conv_id = conv.get("id", "")
            # Skip special conversations
            if conv_id.startswith("48:") or conv_id.startswith("28:"):
                continue
            chat = Chat.from_api(conv, my_user_id=self._user_id)
            if unread_only and chat.unread_count == 0:
                continue
            chats.append(chat)

        # Sort by last message time
        chats.sort(key=lambda c: c.last_message_time, reverse=True)
        if skip:
            chats = chats[skip:]
        chats = chats[:top]

        # Resolve display names for 1:1 chats that have no members
        self._resolve_1on1_chat_names(chats)
        self._cache_chat_metadata(chats)

        if assign_nums:
            self._assign_chat_nums(chats)
        return chats

    def _get_conversations(self, fetch_size: int = 200, refresh: bool = False) -> list[dict]:
        with self._conversations_lock:
            if (not refresh and self._conversations_snapshot_size >= fetch_size
                    and time.monotonic() - self._conversations_snapshot_at < 5):
                return self._conversations_snapshot
            params = {
                "view": "msnp24Equivalent",
                "pageSize": fetch_size,
                "startTime": "0",
                "targetType": "Passport|Skype|Lync|Thread|NotificationStream|cnsContact",
            }
            resp = self._ic3_get("/users/ME/conversations", params=params)
            conversations = resp.get("conversations", [])
            names: dict[str, dict] = {}
            chats: list[Chat] = []
            for conv in conversations:
                chat = Chat.from_api(conv, my_user_id=self._user_id)
                chats.append(chat)
                # The web list already includes the last sender's verified name.
                last = conv.get("lastMessage", {})
                sender_id = last.get("from", "").removeprefix("8:orgid:")
                name = last.get("imdisplayname", "")
                if sender_id and name:
                    names[sender_id] = {"display_name": name}
                    self._user_name_cache[sender_id] = name
            self._metadata.put_many("users", names)
            self._cache_chat_metadata(chats)
            self._conversations_snapshot = conversations
            self._conversations_snapshot_size = fetch_size
            self._conversations_snapshot_at = time.monotonic()
            return conversations

    def _cache_chat_metadata(self, chats: list[Chat]) -> None:
        self._metadata.put_many("chats", {
            chat.id: {
                "title": chat.display_title,
                "topic": chat.topic,
                "members": chat.members,
                "chat_type": chat.chat_type,
            }
            for chat in chats if chat.id and (chat.topic or chat.members)
        })

    def _resolve_1on1_chat_names(self, chats: list[Chat]) -> None:
        """Resolve display names for 1:1 chats that show as '1:1 Chat'.

        For 1:1 chats, the conv_id has format 19:{userId1}_{userId2}@unq.gbl.spaces.
        We extract the other user's ID and look up their display name via Graph.
        """
        pending: dict[str, str] = {}
        for chat in chats:
            # Skip if chat already has members or a topic
            if chat.topic or chat.members:
                continue
            # Only resolve 1:1 chats (identified by @unq.gbl.spaces in conv_id)
            if "@unq.gbl.spaces" not in chat.id:
                continue
            try:
                # Extract user IDs: 19:{id1}_{id2}@unq.gbl.spaces
                id_part = chat.id.split("19:")[-1].split("@")[0]
                user_ids = id_part.split("_")
                # Find the OTHER user's ID (not ours)
                other_ids = [uid for uid in user_ids if uid != self._user_id]
                if not other_ids:
                    continue
                other_id = other_ids[0]
                pending[chat.id] = other_id
            except (ValueError, KeyError):
                # If anything fails, leave it as "1:1 Chat"
                continue
        names = self._resolve_user_names(list(pending.values()))
        for chat in chats:
            other_id = pending.get(chat.id, "")
            name = names.get(other_id, "")
            if name and name != other_id:
                chat.members.append(name)

    def get_chat_messages(
        self,
        chat_num: str,
        top: int = 25,
        after: str | None = None,
        before: str | None = None,
        skip: int = 0,
    ) -> list[Message]:
        """Get messages from a specific chat."""
        conv_id = self._resolve_chat_id(chat_num)
        fetch_size = max((top + skip) * 4, 100) if (after or before) else (top + skip)
        params = {
            "view": "msnp24Equivalent|supportsMessageProperties",
            "pageSize": fetch_size,
        }
        resp = self._ic3_get(
            f"/users/ME/conversations/{conv_id}/messages",
            params=params,
        )
        raw_messages = resp.get("messages", [])

        messages = []
        for m in raw_messages:
            msg_type = m.get("messagetype", "")
            # Only include real messages
            if msg_type in ("Text", "RichText/Html", "RichText"):
                msg = Message.from_api(m, my_user_id=self._user_id)
                messages.append(msg)

        # Messages come newest-first from API, reverse for display
        messages.reverse()

        if after:
            after_dt = self._parse_date_filter(after)
            messages = [m for m in messages if m.timestamp >= after_dt]
        if before:
            before_dt = self._parse_date_filter(before)
            messages = [m for m in messages if m.timestamp <= before_dt]

        messages = messages[-(top + skip):]
        if skip:
            messages = messages[skip:]

        self._assign_message_nums(messages)
        return messages

    def get_message_detail(self, msg_num: str) -> Message:
        """Get a single message by its display number."""
        msg_info = self._resolve_message_id(msg_num)
        conv_id = msg_info["conv"]
        msg_id = msg_info["msg"]

        from urllib.parse import quote
        raw = self._ic3_get(
            f"/users/ME/conversations/{quote(conv_id, safe='')}/messages/{quote(str(msg_id), safe='')}",
            params={"view": "msnp24Equivalent|supportsMessageProperties"},
        )
        m_id = raw.get("id", raw.get("sequenceId", raw.get("version", "")))
        if str(m_id) == str(msg_id):
            raw.setdefault("conversationid", conv_id)
            msg = Message.from_api(raw, my_user_id=self._user_id)
            msg.display_num = int(msg_num.removeprefix("#"))
            return msg

        raise ResourceNotFoundError(f"Message #{msg_num} not found. Try re-reading the chat.")

    def get_message_context(self, msg_num: str, before: int = 2, after: int = 2) -> list[Message]:
        """Fetch a bounded window around an old message with one anchored read."""
        if not (0 <= before <= 50 and 0 <= after <= 50):
            raise ValueError("Message context limits must be between 0 and 50")
        info = self._resolve_message_id(msg_num)
        from urllib.parse import quote
        direction = "BIDIRECTIONAL" if before and after else "BACKWARD" if before else "FORWARD"
        size = (2 * max(before, after) + 3) if before and after else max(before, after) + 2
        response = self._ic3_get(
            f"/users/ME/conversations/{quote(info['conv'], safe='')}/messages/epochTimeStamp/{quote(str(info['msg']), safe='')}",
            params={"pageSize": size, "direction": direction, "includeMetadata": "true", "locationType": "Primary"},
        )
        messages = []
        for raw in response.get("messages", []):
            if raw.get("messagetype") in ("Text", "RichText/Html", "RichText"):
                raw.setdefault("conversationid", info["conv"])
                messages.append(Message.from_api(raw, my_user_id=self._user_id))
        if not any(str(m.id) == str(info["msg"]) for m in messages):
            messages.append(self.get_message_detail(msg_num))
        messages.sort(key=lambda m: (m.timestamp, str(m.id)))
        index = next(i for i, m in enumerate(messages) if str(m.id) == str(info["msg"]))
        messages = messages[max(0, index - before):index + after + 1]
        self._assign_message_nums(messages)
        self._resolve_chat_titles(messages)
        return messages

    # ------------------------------------------------------------------
    # Send messages
    # ------------------------------------------------------------------

    def send_message(self, conv_id: str, content: str, html: bool = True) -> dict:
        """Send a message to a conversation."""
        if not self._display_name:
            from .auth import _decode_display_name
            self._display_name = _decode_display_name(self._ic3) or "User"

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        client_msg_id = self._make_client_message_id()

        if html and not content.startswith("<"):
            paragraphs = [f"<p>{p}</p>" for p in content.split("\n") if p.strip()]
            content = "".join(paragraphs) or "<p></p>"

        payload = {
            "id": "-1",
            "type": "Message",
            "conversationid": conv_id,
            "from": self._user_mri,
            "composetime": now,
            "originalarrivaltime": now,
            "content": content,
            "messagetype": "RichText/Html" if html else "Text",
            "contenttype": "Text",
            "imdisplayname": self._display_name,
            "clientmessageid": client_msg_id,
            "properties": {
                "formatVariant": "TEAMS",
                "cards": "[]",
                "links": "[]",
                "mentions": "[]",
                "files": "[]",
            },
            "crossPostChannels": [],
        }

        return self._ic3_post(
            f"/users/ME/conversations/{conv_id}/messages",
            json_data=payload,
            is_write=True,
        )

    def send_message_to_chat(self, chat_num: str, content: str, html: bool = True) -> dict:
        """Send a message to a chat by display number."""
        conv_id = self._resolve_chat_id(chat_num)
        return self.send_message(conv_id, content, html=html)

    def reply_to_message(self, msg_num: str, content: str, html: bool = True) -> dict:
        """Reply to a message by display number."""
        original = self.get_message_detail(msg_num)
        reply_html = self._build_reply_html(original, content, html=html)
        return self.send_message(original.conversation_id, reply_html, html=True)

    def edit_message(self, msg_num: str, new_text: str) -> dict:
        """Edit a message by display number."""
        msg_info = self._resolve_message_id(msg_num)
        conv_id = msg_info["conv"]
        msg_id = msg_info["msg"]

        content = f"<p>{new_text}</p>"
        payload = {
            "content": content,
            "messagetype": "RichText/Html",
        }
        return self._ic3_put(
            f"/users/ME/conversations/{conv_id}/messages/{msg_id}",
            json_data=payload,
        )

    def delete_message(self, msg_num: str) -> dict:
        """Delete a message by display number."""
        msg_info = self._resolve_message_id(msg_num)
        conv_id = msg_info["conv"]
        msg_id = msg_info["msg"]

        payload = {
            "deletetime": int(time.time() * 1000),
        }
        return self._ic3_delete(
            f"/users/ME/conversations/{conv_id}/messages/{msg_id}",
            json_data=payload,
        )

    def send_message_to_user(self, user_id: str, content: str, html: bool = True) -> dict:
        """Send a message to a user by creating/finding 1:1 conversation."""
        # Self-message: use the 48:notes thread (Teams self-chat)
        if user_id == self._user_id:
            return self.send_message("48:notes", content, html=html)

        user_mri = f"8:orgid:{user_id}" if not user_id.startswith("8:") else user_id

        # Create 1:1 conversation via MT API
        conv_id = self._get_or_create_1on1(user_mri)
        return self.send_message(conv_id, content, html=html)

    def create_group_chat(self, user_ids: list[str], topic: str = "") -> dict:
        """Create a group chat via IC3 /threads API (same as Teams web client).

        Args:
            user_ids: List of user IDs to add as members.
            topic: Optional topic/title for the group chat.

        Returns:
            Dict with thread info. The conversation ID can be extracted from
            the response or from the Location header.
        """
        members = [{"id": self._user_mri, "role": "Admin"}]
        for uid in user_ids:
            mri = f"8:orgid:{uid}" if not uid.startswith("8:") else uid
            members.append({"id": mri, "role": "Admin"})

        payload: dict = {
            "members": members,
            "properties": {"threadType": "chat"},
        }

        raw_resp = self._ic3_post_raw("/threads", json_data=payload, is_write=True)
        if raw_resp.status_code >= 400:
            self._handle_response(raw_resp)

        # Extract thread ID from Location header
        location = raw_resp.headers.get("location", "")
        conv_id = ""
        if "/threads/" in location:
            conv_id = location.split("/threads/")[-1]
        resp = {"id": conv_id, "status": "created"}
        if topic and conv_id:
            try:
                from urllib.parse import quote
                self._ic3_put(
                    f"/threads/{quote(conv_id, safe='')}/properties?name=topic",
                    json_data={"topic": topic},
                )
            except Exception:
                pass  # Topic is optional, don't fail on it

        return resp

    def forward_message(self, msg_num: str, chat_num: str, comment: str = "") -> dict:
        """Forward a message to another chat.

        Args:
            msg_num: Display number of the message to forward.
            chat_num: Display number of the target chat.
            comment: Optional comment to add below the forwarded content.
        """
        original = self.get_message_detail(msg_num)
        sender = escape(original.sender or "Unknown")
        original_text = (original.text_content or "").strip() or "[message]"
        original_html = escape(original_text)

        forwarded = (
            f'<blockquote><b>Forwarded from {sender}</b><br/>{original_html}</blockquote>'
        )
        if comment:
            forwarded += f"<p>{escape(comment)}</p>"

        return self.send_message_to_chat(chat_num, forwarded, html=True)

    @staticmethod
    def _reply_sender_mri(sender_id: str) -> str:
        if not sender_id:
            return ""
        if ":" in sender_id:
            return sender_id
        return f"8:orgid:{sender_id}"

    @staticmethod
    def _reply_preview_text(message: Message) -> str:
        preview = (message.text_content or "").strip()
        if preview:
            return preview
        if message.attachments:
            return message.attachments[0].name
        return "[message]"

    def _build_reply_html(self, message: Message, content: str, html: bool = True) -> str:
        if html and not content.startswith("<"):
            paragraphs = [f"<p>{p}</p>" for p in content.split("\n") if p.strip()]
            content = "".join(paragraphs) or "<p></p>"

        msg_id = escape(str(message.id))
        sender_mri = escape(self._reply_sender_mri(message.sender_id))
        sender_name = escape(message.sender or "Unknown")
        preview = escape(self._reply_preview_text(message))

        return (
            f'<blockquote itemscope="" itemtype="http://schema.skype.com/Reply" itemid="{msg_id}">\r\n'
            f'<strong itemprop="mri" itemid="{sender_mri}">{sender_name}</strong>'
            f'<span itemprop="time" itemid="{msg_id}"></span>\r\n'
            f'<p itemprop="preview">{preview}</p>\r\n'
            f"</blockquote>\r\n"
            f"{content}"
        )

    def _get_or_create_1on1(self, user_mri: str) -> str:
        """Find or create a 1:1 conversation with a user."""
        # Extract user ID from MRI format
        user_id = user_mri.split("8:orgid:")[-1] if "8:orgid:" in user_mri else user_mri

        # Step 1: Search existing chats for an existing 1:1 conversation
        existing = self._find_existing_1on1(user_id)
        if existing:
            return existing

        # Step 2: Try Graph API to create chat
        if self._graph:
            try:
                payload = {
                    "chatType": "oneOnOne",
                    "members": [
                        {
                            "@odata.type": "#microsoft.graph.aadUserConversationMember",
                            "roles": ["owner"],
                            "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{self._user_id}')",
                        },
                        {
                            "@odata.type": "#microsoft.graph.aadUserConversationMember",
                            "roles": ["owner"],
                            "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{user_id}')",
                        },
                    ],
                }
                resp = self._graph_post("/chats", json_data=payload)
                chat_id = resp.get("id", "")
                if chat_id:
                    return chat_id
            except (httpx.HTTPStatusError, TokenExpiredError, RateLimitError, KeyError):
                pass

        # Step 3: Fallback to IC3 threads API
        payload = {
            "members": [
                {"id": self._user_mri, "role": "Admin"},
                {"id": user_mri, "role": "Admin"},
            ],
            "properties": {
                "threadType": "chat",
                "chatFileBehavior": "chat",
                "fixedRoster": "true",
                "uniquerosterthread": "true",
            },
        }
        resp = self._ic3_post("/threads", json_data=payload, is_write=True)

        # Response should have the conversation ID
        if isinstance(resp, dict):
            return resp.get("id", resp.get("conversationId", ""))

        # Sometimes the response is just the conv ID string
        if isinstance(resp, str):
            return resp

        raise ValueError("Could not create 1:1 conversation")

    def _find_existing_1on1(self, target_user_id: str) -> str | None:
        """Search existing chats for a 1:1 conversation with the given user."""
        # Check cached ID map for 1:1 conversations containing the user ID
        for _key, conv_id in self._id_map.get("chats", {}).items():
            if isinstance(conv_id, str) and "@unq.gbl.spaces" in conv_id:
                if self._conv_id_matches_user(conv_id, target_user_id):
                    return conv_id

        # Fetch recent chats and look for 1:1 with user
        try:
            params = {
                "view": "msnp24Equivalent",
                "pageSize": 100,
                "startTime": "0",
                "targetType": "Passport|Skype|Lync|Thread|NotificationStream|cnsContact",
            }
            resp = self._ic3_get("/users/ME/conversations", params=params)
            for conv in resp.get("conversations", []):
                conv_id = conv.get("id", "")
                if "@unq.gbl.spaces" in conv_id:
                    if self._conv_id_matches_user(conv_id, target_user_id):
                        return conv_id
        except (httpx.HTTPStatusError, TokenExpiredError, RateLimitError, KeyError):
            pass

        return None

    def _conv_id_matches_user(self, conv_id: str, target_user_id: str) -> bool:
        """Check if a 1:1 conv_id is a chat WITH the target user.

        Conv ID format: 19:{id1}_{id2}@unq.gbl.spaces
        Our own ID is in EVERY 1:1 chat, so we must check that the
        OTHER party is the target — not just that target appears anywhere.
        """
        id_part = conv_id.split("19:")[-1].split("@")[0]
        parts = id_part.split("_")
        if len(parts) != 2:
            return False

        if target_user_id == self._user_id:
            # Self-chat: both parts must be our own ID
            return parts[0] == self._user_id and parts[1] == self._user_id

        # Normal 1:1: the OTHER party (not us) must be the target
        other = parts[0] if parts[1] == self._user_id else parts[1]
        return other == target_user_id

    # ------------------------------------------------------------------
    # User search
    # ------------------------------------------------------------------

    def search_users(self, query: str, top: int = 10) -> list[User]:
        """Search for users via Graph API.

        If query looks like an email, uses /users?$filter instead of /me/people
        since People API doesn't support email addresses in $search.
        """
        if "@" in query:
            # Email query — use /users endpoint with filter
            try:
                resp = self._graph_get("/users", params={
                    "$filter": f"mail eq '{query}' or userPrincipalName eq '{query}'",
                    "$top": top,
                })
                users = []
                for item in resp.get("value", []):
                    user = User(
                        id=item.get("id", ""),
                        display_name=item.get("displayName", ""),
                        email=item.get("mail", item.get("userPrincipalName", "")),
                        user_type=item.get("userType", ""),
                    )
                    users.append(user)
                if users:
                    return users
            except (httpx.HTTPStatusError, TokenExpiredError, RateLimitError, KeyError):
                pass

        # Name query — use /me/people
        # People API doesn't accept email addresses; extract name part
        people_query = query.split("@")[0].replace(".", " ") if "@" in query else query
        resp = self._graph_get("/me/people", params={
            "$search": people_query,
            "$top": top,
        })

        users = []
        for item in resp.get("value", []):
            # Graph people API format
            email = item.get("userPrincipalName", "")
            if not email:
                # Try scoredEmailAddresses
                scored = item.get("scoredEmailAddresses", [])
                if scored:
                    email = scored[0].get("address", "")
            user = User(
                id=item.get("id", ""),
                display_name=item.get("displayName", ""),
                email=email,
                user_type=item.get("personType", {}).get("subclass", ""),
            )
            users.append(user)

        # Re-rank: /me/people returns by relevance (contact frequency),
        # not by name match. Sort so exact/partial name matches come first.
        if users:
            users = self._rank_users_by_query(users, query)

        return users

    @staticmethod
    def _rank_users_by_query(users: list[User], query: str) -> list[User]:
        """Re-rank user results so name matches appear before relevance-only results."""
        q = query.lower()

        def score(u: User) -> tuple[int, str]:
            name = u.display_name.lower()
            email = u.email.lower()
            if name == q:
                return (0, name)
            if email.split("@")[0] == q:
                return (1, name)
            if name.startswith(q):
                return (2, name)
            words = q.split()
            if all(w in name for w in words):
                return (3, name)
            if any(w in name for w in words):
                return (4, name)
            if q in email:
                return (5, name)
            return (6, name)

        return sorted(users, key=score)

    def get_presence(self) -> dict:
        """Get current presence status, with UPS fallback for tenants that block Graph."""
        capability = self._metadata.get("capabilities", "graph_presence") or {}
        if not (self._presence_token and capability.get("denied") is True):
            try:
                return self._graph_get("/me/presence")
            except (httpx.HTTPStatusError, TokenExpiredError) as exc:
                # Cache only a confirmed permission denial. Expiry and transient
                # errors must not suppress Graph for the next invocation.
                if not self._presence_token:
                    raise
                if isinstance(exc, httpx.HTTPStatusError):
                    if exc.response is None or exc.response.status_code not in (401, 403):
                        raise
                    if exc.response.status_code == 403:
                        self._metadata.put_many("capabilities", {"graph_presence": {"denied": True}})

        resp = self._ups_post(
            "/presence/getpresence/",
            [{"mri": self._user_mri}],
        )
        if not isinstance(resp, list) or not resp:
            return {}

        presence = resp[0].get("presence", {})
        forced = presence.get("forcedAvailability", {})
        return {
            "availability": presence.get("availability") or forced.get("availability", "Unknown"),
            "activity": presence.get("activity") or forced.get("activity", ""),
        }

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_messages(
        self,
        query: str,
        top: int = 25,
        chat_num: str | None = None,
        from_filter: str | None = None,
        after: str | None = None,
        before: str | None = None,
        offset: int = 0,
    ) -> list[Message]:
        """Search messages across chats or within a specific chat."""
        fetch_size = top + offset
        conv_id = self._resolve_chat_id(chat_num) if chat_num else None
        if self._substrate:
            server_query = self._search_query(query, conv_id, from_filter, after, before)
            messages = self._substrate_search(server_query, fetch_size)
        else:
            messages = self._mt_search_fallback(query, fetch_size, chat_num)

        # Client-side filtering
        if from_filter:
            from_lower = from_filter.removeprefix("8:orgid:").casefold()
            messages = [m for m in messages if from_lower in m.sender.casefold()
                        or from_lower == m.sender_id.casefold()
                        or from_lower == self._sender_email_cache.get(m.sender_id, "").casefold()]
        if after:
            after_dt = self._parse_date_filter(after)
            messages = [m for m in messages if m.timestamp >= after_dt]
        if before:
            before_dt = self._parse_date_filter(before)
            if len(before) == 10:
                messages = [m for m in messages if m.timestamp < before_dt + timedelta(days=1)]
            else:
                messages = [m for m in messages if m.timestamp <= before_dt]
        if conv_id:
            messages = [m for m in messages if m.conversation_id == conv_id]

        if offset:
            messages = messages[offset:]
        messages = messages[:top]
        self._assign_message_nums(messages)
        self._resolve_chat_titles(messages)
        return messages

    @staticmethod
    def _search_query(query: str, conv_id: str | None = None, from_filter: str | None = None,
                      after: str | None = None, before: str | None = None) -> str:
        """Push supported filters into Teams' index before selecting the top hits."""
        def quoted(value: str) -> str:
            return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
        terms = []
        if conv_id:
            terms.append(f"ClientThreadId:{quoted(conv_id)}")
        if from_filter:
            uid = from_filter.removeprefix("8:orgid:")
            try:
                uuid.UUID(uid)
            except ValueError:
                terms.append(f"from:{quoted(from_filter)}")
            else:
                terms.append("Extension_SkypeSpaces_ConversationPost_Extension_FromSkypeInternalId_String:"
                             + quoted(f"8:orgid:{uid}"))
        if after:
            date = TeamsClient._parse_date_filter(after).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            terms.append(f"sent>={quoted(date)}")
        if before:
            date = TeamsClient._parse_date_filter(before)
            operator = "<="
            if len(before) == 10:
                date += timedelta(days=1)
                operator = "<"
            value = date.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            terms.append(f"sent{operator}{quoted(value)}")
        return f"({query}) AND " + " AND ".join(terms) if terms else query

    def _substrate_search(self, query: str, top: int) -> list[Message]:
        """Search via Substrate Search API (same API Teams web uses)."""
        payload = {
            "EntityRequests": [
                {
                    "entityType": "Message",
                    "contentSources": ["Teams"],
                    "fields": [
                        "Extension_SkypeSpaces_ConversationPost_Extension_FromSkypeInternalId_String",
                        "Extension_SkypeSpaces_ConversationPost_Extension_FileData_String",
                        "Extension_SkypeSpaces_ConversationPost_Extension_ThreadType_String",
                        "Extension_SkypeSpaces_ConversationPost_Extension_SenderTenantId_String",
                        "Extension_SkypeSpaces_ConversationPost_Extension_ParentMessageId_String",
                    ],
                    "propertySet": "Optimized",
                    "query": {
                        "queryString": f"NOT (isClientSoftDeleted:TRUE) AND {query}",
                        "displayQueryString": query,
                    },
                    "size": top,
                    "topResultsCount": min(top, 9),
                }
            ],
            "QueryAlterationOptions": {"EnableAlteration": True},
            "cvid": str(uuid.uuid4()),
            "logicalId": str(uuid.uuid4()),
            "scenario": {
                "Dimensions": [
                    {"DimensionName": "QueryType", "DimensionValue": "All"},
                    {"DimensionName": "FormFactor", "DimensionValue": "general.web.reactSearch"},
                ],
                "Name": "powerbar",
            },
        }

        self._session.jitter(is_write=False)
        tenant_id = self._get_tenant_id()
        headers = {
            "Authorization": f"Bearer {self._substrate}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "x-anchormailbox": f"Oid:{self._user_id}@{tenant_id}",
            "client-request-id": str(uuid.uuid4()),
        }
        data = self._request_with_retry(
            "POST", SUBSTRATE_SEARCH_BASE, headers=headers, json_data=payload,
            safe_read=True,
        )

        messages = []
        seen: set[tuple[str, str]] = set()
        for entity_set in data.get("EntitySets", []):
            for result_set in entity_set.get("ResultSets", []):
                if result_set.get("ContentSources") != ["Teams"]:
                    continue
                for result in result_set.get("Results", []):
                    msg = self._parse_substrate_result(result)
                    if msg and (msg.conversation_id, str(msg.id)) not in seen:
                        seen.add((msg.conversation_id, str(msg.id)))
                        messages.append(msg)
        names = self._resolve_user_names([m.sender_id for m in messages if m.sender == m.sender_id])
        for msg in messages:
            if msg.sender == msg.sender_id:
                msg.sender = names.get(msg.sender_id, msg.sender_id)
        observed_names = {
            msg.sender_id: {"display_name": msg.sender}
            for msg in messages if msg.sender_id and msg.sender and msg.sender != msg.sender_id
        }
        self._metadata.put_many("users", observed_names)
        self._user_name_cache.update({uid: value["display_name"] for uid, value in observed_names.items()})
        self._metadata.put_many("chats", {
            msg.conversation_id: {"title": msg.chat_title, "topic": msg.chat_title}
            for msg in messages if msg.conversation_id and msg.chat_title
        })
        return messages

    def _parse_substrate_result(self, result: dict) -> Message | None:
        """Convert a Substrate search result into a Message."""
        source = result.get("Source", {})
        exts = source.get("Extensions", {})

        sender_mri = exts.get(
            "SkypeSpaces_ConversationPost_Extension_FromSkypeInternalId", ""
        )
        sender_id = sender_mri.split("8:orgid:")[-1] if "8:orgid:" in sender_mri else sender_mri
        sender_address = source.get("Sender", {}).get("EmailAddress", {}).get("Address", "")
        if sender_id and sender_address:
            self._sender_email_cache[sender_id] = sender_address

        thread_id = source.get("ClientThreadId", "")
        message_id = source.get("InternetMessageId", "")
        sent = source.get("DateTimeSent", source.get("DateTimeCreated", ""))

        # Build text from HitHighlightedSummary or Preview
        summary = result.get("HitHighlightedSummary", "")
        preview = source.get("Preview", summary)

        # Parse file attachments from extension data
        attachments = []
        file_data_str = exts.get(
            "SkypeSpaces_ConversationPost_Extension_FileData", ""
        )
        if file_data_str:
            try:
                file_list = json.loads(file_data_str)
                for f in file_list:
                    attachments.append(Attachment(
                        id=f.get("id", f.get("itemid", "")),
                        name=f.get("fileName", f.get("title", "")),
                        content_type=f.get("fileType", ""),
                        content_url=f.get("objectUrl", ""),
                        size=0,
                    ))
            except (json.JSONDecodeError, TypeError):
                pass

        is_from_me = sender_id == self._user_id

        # Resolve sender display name from cached user lookups
        sender_name = source.get("Sender", {}).get("EmailAddress", {}).get("Name", "")
        if not sender_name and is_from_me:
            sender_name = self._display_name
        if not sender_name:
            cached = self._metadata.get("users", sender_id) or {}
            sender_name = self._user_name_cache.get(sender_id) or cached.get("display_name") or sender_id

        from .models import _parse_dt, _strip_html

        msg = Message(
            id=message_id,
            conversation_id=thread_id,
            sender=sender_name,
            sender_id=sender_id,
            content=preview,
            message_type="RichText/Html",
            timestamp=_parse_dt(sent),
            is_from_me=is_from_me,
            text_content=_strip_html(preview),
            attachments=attachments,
            chat_title=source.get("ConversationTopic", "") or "",
        )
        return msg

    def _resolve_user_name(self, user_id: str) -> str:
        """Try to resolve a user ID to a display name via Graph (cached)."""
        return self._resolve_user_names([user_id]).get(user_id, user_id)

    def _resolve_user_names(self, user_ids: list[str]) -> dict[str, str]:
        """Resolve only missing names, in Graph batches of at most 20 reads."""
        names: dict[str, str] = {}
        missing: list[str] = []
        for uid in dict.fromkeys(uid for uid in user_ids if uid):
            entry = self._metadata.get("users", uid) or {}
            name = self._user_name_cache.get(uid) or entry.get("display_name")
            if name:
                names[uid] = name
            else:
                missing.append(uid)
        resolved: dict[str, dict] = {}
        not_found: dict[str, dict] = {}
        from urllib.parse import quote
        for start in range(0, len(missing), 20):
            chunk = missing[start:start + 20]
            try:
                responses = self._graph_batch_get({
                    uid: f"/users/{quote(uid, safe='')}?$select=id,displayName"
                    for uid in chunk
                })
                for uid, body in responses.items():
                    name = body.get("displayName", "")
                    if name:
                        names[uid] = name
                        resolved[uid] = {"display_name": name}
                    elif body.get("_not_found"):
                        # Directory-deleted/guest IDs can remain in old chats.
                        # A short 404 cache avoids a repeated failed lookup on
                        # every invocation; observed web sender names replace it.
                        not_found[uid] = {"display_name": uid, "not_found": True}
            except (httpx.HTTPStatusError, AuthRequiredError, TokenExpiredError, RateLimitError, RetryableError, KeyError):
                # Name enrichment must not discard otherwise valid messages.
                pass
        self._metadata.put_many("users", resolved)
        self._metadata.put_many("users", not_found, ttl=MetadataCache.MISSING_USER_TTL)
        for uid in missing:
            names.setdefault(uid, uid)
        self._user_name_cache.update(names)
        return names

    def _mt_search_fallback(
        self, query: str, top: int, chat_num: str | None,
    ) -> list[Message]:
        """Fallback search via in-chat message scanning."""
        if chat_num:
            all_msgs = self.get_chat_messages(chat_num, top=200)
            query_lower = query.lower()
            return [m for m in all_msgs if query_lower in m.text_content.lower()][:top]

        # No substrate token and no specific chat — scan recent chats
        chats = self.get_chats(top=10)
        self._cache_chat_metadata(chats)
        results: list[Message] = []
        query_lower = query.lower()
        for chat in chats:
            try:
                msgs = self.get_chat_messages(str(chat.display_num), top=50)
                for m in msgs:
                    if query_lower in m.text_content.lower():
                        results.append(m)
            except (httpx.HTTPStatusError, TokenExpiredError, RateLimitError, ValueError):
                continue
            if len(results) >= top:
                break
        return results[:top]

    # ------------------------------------------------------------------
    # File sending
    # ------------------------------------------------------------------

    def send_file(self, conv_id: str, file_path: str, message: str = "") -> dict:
        """Upload a file to OneDrive and send it in a chat message.

        Flow: Upload to OneDrive "Microsoft Teams Chat Files" → send message with file reference.
        """
        import os
        from pathlib import Path

        path = Path(file_path)
        if not path.exists():
            raise ResourceNotFoundError(f"File not found: {file_path}")

        file_name = path.name
        file_size = path.stat().st_size
        content_bytes = path.read_bytes()

        # Step 1: Upload to OneDrive via Graph API
        token = self._graph or self._ic3
        self._session.jitter(is_write=True)
        upload_url = f"{GRAPH_BASE}/me/drive/root:/Microsoft Teams Chat Files/{file_name}:/content"
        resp = self._session.client.put(
            upload_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/octet-stream",
            },
            content=content_bytes,
        )
        if resp.status_code == 401:
            raise TokenExpiredError("Token expired. Run: teams login")
        resp.raise_for_status()
        item = resp.json()
        file_url = item.get("webUrl", "")
        file_id = item.get("id", "")

        # Step 2: Grant access to chat members via invite + org-wide link
        self._session.jitter(is_write=True)
        # Create organization-wide sharing link
        self._session.client.post(
            f"{GRAPH_BASE}/me/drive/items/{file_id}/createLink",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"type": "edit", "scope": "organization"},
        )
        # Also grant direct read access to chat participants
        try:
            members = self._get_chat_members(conv_id)
            if members:
                self._session.client.post(
                    f"{GRAPH_BASE}/me/drive/items/{file_id}/invite",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={
                        "requireSignIn": True,
                        "sendInvitation": False,
                        "roles": ["read"],
                        "recipients": [{"email": m} for m in members if m],
                    },
                )
        except (httpx.HTTPStatusError, TokenExpiredError, RateLimitError, KeyError):
            pass  # Best effort — org link should suffice

        # Step 3: Get sharing URL for the file
        share_url = ""
        try:
            perms_resp = self._session.client.get(
                f"{GRAPH_BASE}/me/drive/items/{file_id}/permissions",
                headers={"Authorization": f"Bearer {token}"},
            )
            if perms_resp.status_code == 200:
                for p in perms_resp.json().get("value", []):
                    link = p.get("link", {})
                    if link.get("scope") == "organization":
                        share_url = link.get("webUrl", "")
                        break
        except (httpx.HTTPStatusError, TokenExpiredError, RateLimitError, KeyError):
            pass

        # Extract siteUrl from webUrl
        # e.g. https://acerpro-my.sharepoint.com/personal/user/Documents/... → https://acerpro-my.sharepoint.com/personal/user/
        site_url = ""
        if "/Documents/" in file_url:
            site_url = file_url.split("/Documents/")[0] + "/"

        # Step 4: Send message with file attachment (matching Teams web format)
        if not self._display_name:
            from .auth import _decode_display_name
            self._display_name = _decode_display_name(self._ic3) or "User"

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        client_msg_id = self._make_client_message_id()

        text_part = f"<p>{message}</p>" if message else ""
        file_ext = path.suffix.lstrip(".")
        content_html = (
            f'{text_part}'
            f'<p><span itemscope="" itemtype="http://schema.skype.com/File">'
            f'<a href="{file_url}">{file_name}</a>'
            f'</span></p>'
        )

        payload = {
            "id": "-1",
            "type": "Message",
            "conversationid": conv_id,
            "from": self._user_mri,
            "composetime": now,
            "originalarrivaltime": now,
            "content": content_html,
            "messagetype": "RichText/Html",
            "contenttype": "Text",
            "imdisplayname": self._display_name,
            "clientmessageid": client_msg_id,
            "properties": {
                "importance": "",
                "subject": "",
                "formatVariant": "TEAMS",
                "cards": "[]",
                "links": "[]",
                "mentions": "[]",
                "files": json.dumps([{
                    "fileName": file_name,
                    "fileType": file_ext,
                    "itemid": file_id,
                    "fileInfo": {
                        "itemId": file_id,
                        "fileUrl": file_url,
                        "siteUrl": site_url,
                        "serverRelativeUrl": "",
                        "shareUrl": share_url,
                    },
                    "filePreview": {
                        "previewUrl": share_url or file_url,
                    },
                    "state": "active",
                }]),
            },
            "crossPostChannels": [],
        }

        return self._ic3_post(
            f"/users/ME/conversations/{conv_id}/messages",
            json_data=payload,
            is_write=True,
        )

    @staticmethod
    def _make_client_message_id() -> str:
        """Generate a browser-like positive numeric client message ID."""
        return str(secrets.randbelow(9 * 10**18) + 10**18)

    def send_file_to_chat(self, chat_num: str, file_path: str, message: str = "") -> dict:
        """Send a file to a chat by display number."""
        conv_id = self._resolve_chat_id(chat_num)
        return self.send_file(conv_id, file_path, message=message)

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    def get_attachments(self, msg_num: str) -> list:
        """Get attachments for a message."""
        msg = self.get_message_detail(msg_num)
        return msg.attachments

    def download_attachment(self, attachment) -> bytes:
        """Download attachment content.

        - ASM URLs (inline images): IC3 token direct download
        - SharePoint URLs (file attachments): Graph shares API with base64 encoding
        """
        url = attachment.content_url
        if not url:
            raise ResourceNotFoundError(f"No download URL for attachment '{attachment.name}'")

        self._session.jitter(is_write=False)

        if "sharepoint.com" in url:
            return self._download_sharepoint(url)

        # ASM / direct URLs — use IC3 token
        headers = {"Authorization": f"Bearer {self._ic3}"}
        resp = self._session.client.get(url, headers=headers, follow_redirects=True)
        if resp.status_code == 401:
            raise TokenExpiredError("Token expired. Run: teams login")
        resp.raise_for_status()
        return resp.content

    def _download_sharepoint(self, url: str) -> bytes:
        """Download from SharePoint via Graph shares API."""
        import base64
        token = self._graph or self._ic3
        encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        share_id = f"u!{encoded}"
        resp = self._session.client.get(
            f"{GRAPH_BASE}/shares/{share_id}/driveItem/content",
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=True,
        )
        if resp.status_code == 401:
            raise TokenExpiredError("Token expired. Run: teams login")
        resp.raise_for_status()
        return resp.content

    # ------------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------------

    # Emoji name → Unicode mapping for Graph API reactions
    REACTION_EMOJIS = {
        "like": "\U0001f44d",
        "heart": "\u2764\ufe0f",
        "laugh": "\U0001f602",
        "surprised": "\U0001f62e",
        "sad": "\U0001f622",
        "angry": "\U0001f621",
    }

    def add_reaction(self, msg_num: str, emoji: str) -> dict:
        """Add a reaction to a message via Graph beta API."""
        msg_info = self._resolve_message_id(msg_num)
        conv_id = msg_info["conv"]
        msg_id = msg_info["msg"]

        unicode_emoji = self.REACTION_EMOJIS.get(emoji, emoji)
        return self._graph_post(
            f"/chats/{conv_id}/messages/{msg_id}/setReaction",
            json_data={"reactionType": unicode_emoji},
            beta=True,
        )

    def remove_reaction(self, msg_num: str, emoji: str) -> dict:
        """Remove a reaction from a message via Graph beta API."""
        msg_info = self._resolve_message_id(msg_num)
        conv_id = msg_info["conv"]
        msg_id = msg_info["msg"]

        unicode_emoji = self.REACTION_EMOJIS.get(emoji, emoji)
        return self._graph_post(
            f"/chats/{conv_id}/messages/{msg_id}/unsetReaction",
            json_data={"reactionType": unicode_emoji},
            beta=True,
        )

    def mark_chat_read(self, chat_num: str) -> dict:
        """Mark an entire chat as read by setting consumptionhorizon to now."""
        conv_id = self._resolve_chat_id(chat_num)
        from urllib.parse import quote
        now_ms = str(int(time.time() * 1000))
        horizon_value = f"{now_ms};{now_ms};{now_ms}"
        return self._ic3_put(
            f"/users/ME/conversations/{quote(conv_id, safe='')}/properties?name=consumptionhorizon",
            json_data={"consumptionhorizon": horizon_value},
        )

    def mark_message_read(self, msg_num: str) -> dict:
        """Mark a message (and its conversation up to that point) as read.

        Uses the IC3 consumption horizon API to set the read position
        in the conversation to the given message.
        """
        msg_info = self._resolve_message_id(msg_num)
        conv_id = msg_info["conv"]
        msg_id = msg_info["msg"]
        # consumptionhorizon format: "<timestamp>;<originalarrivaltime>;<clientmessageid>"
        # Using the msg_id as both the arrival time marker and client id.
        horizon_value = f"{int(time.time() * 1000)};{msg_id};{msg_id}"
        return self._ic3_put(
            f"/users/ME/conversations/{conv_id}/properties?name=consumptionhorizon",
            json_data={"consumptionhorizon": horizon_value},
        )

    def mark_message_unread(self, msg_num: str) -> dict:
        """Mark a message as unread using consumptionHorizonBookmark.

        Sets the bookmark to the message's timestamp so that the conversation
        appears unread from that message onward (same as Teams web "Mark as unread").
        """
        msg_info = self._resolve_message_id(msg_num)
        conv_id = msg_info["conv"]
        msg_id = msg_info["msg"]
        # Format: "{timestamp};{bookmark_timestamp};{msg_id}"
        # Setting bookmark_timestamp to current time signals "unread from msg_id"
        now_ms = str(int(time.time() * 1000))
        bookmark = f"{now_ms};{now_ms};{msg_id}"
        from urllib.parse import quote
        return self._ic3_put(
            f"/users/ME/conversations/{quote(conv_id, safe='')}/properties?name=consumptionHorizonBookmark",
            json_data={"consumptionHorizonBookmark": bookmark},
        )

    # ------------------------------------------------------------------
    # ID mapping
    # ------------------------------------------------------------------

    def _get_chat_members(self, conv_id: str) -> list[str]:
        """Get email addresses of chat members via Graph /me/people or conversation."""
        emails = []
        try:
            # For 1:1 chats, extract user ID from conv_id and look up
            if "@unq.gbl.spaces" in conv_id:
                # 19:{userId1}_{userId2}@unq.gbl.spaces
                parts = conv_id.split("19:")[-1].split("@")[0].split("_")
                for uid in parts:
                    if uid != self._user_id:
                        resp = self._graph_get(f"/users/{uid}")
                        email = resp.get("mail", resp.get("userPrincipalName", ""))
                        if email:
                            emails.append(email)
            else:
                # Group chat — fetch conversation members
                resp = self._ic3_get(f"/users/ME/conversations/{conv_id}")
                for m in resp.get("members", []):
                    mri = m.get("id", "")
                    if "8:orgid:" in mri:
                        uid = mri.split("8:orgid:")[-1]
                        if uid != self._user_id:
                            try:
                                user_resp = self._graph_get(f"/users/{uid}")
                                email = user_resp.get("mail", user_resp.get("userPrincipalName", ""))
                                if email:
                                    emails.append(email)
                            except (httpx.HTTPStatusError, TokenExpiredError, RateLimitError, KeyError):
                                pass
        except (httpx.HTTPStatusError, TokenExpiredError, RateLimitError, KeyError, json.JSONDecodeError):
            pass
        return emails

    def _resolve_chat_id(self, display_id: str) -> str:
        display_id = display_id.removeprefix("#")
        self._refresh_id_map_entry("chats", display_id)
        if display_id in self._id_map["chats"]:
            return self._id_map["chats"][display_id]
        # Maybe it's already a conversation ID
        if ":" in display_id and (
            "@" in display_id
            or display_id.startswith(("48:", "28:"))
            or len(display_id) > 50
        ):
            return display_id
        if not display_id.isdigit():
            conv_id = self.find_chat(display_id)
            if conv_id:
                return conv_id
        raise ResourceNotFoundError(
            f"Unknown chat #{display_id}. Run 'teams chats' first to populate the ID map."
        )

    def find_chat(self, name: str, refresh: bool = False) -> str | None:
        """Resolve an exact or unambiguous partial title without creating a chat."""
        def matches() -> tuple[list[str], list[str]]:
            q = name.casefold().strip()
            entries = self._metadata.items("chats")
            exact = [cid for cid, value in entries.items() if value.get("title", "").casefold() == q]
            partial = [cid for cid, value in entries.items() if q in value.get("title", "").casefold()]
            return exact, partial

        if not refresh:
            exact, partial = matches()
            if len(exact) == 1:
                return exact[0]
        # Freshen the bounded recent list before accepting partial matches.
        chats = self.get_chats(top=200, refresh=refresh)
        exact, partial = matches()
        candidates = exact or partial
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            titles = self._metadata.items("chats")
            numbered = {chat.id: chat.display_num for chat in chats}
            options = ", ".join(
                f"#{numbered[cid]} {titles[cid]['title']}" if cid in numbered else titles[cid]["title"]
                for cid in candidates[:5]
            )
            raise ResourceNotFoundError(f"Chat name is ambiguous: {options}. Use a chat number or full ID.")
        return None

    def _resolve_message_id(self, display_id: str) -> dict:
        display_id = display_id.removeprefix("#")
        self._refresh_id_map_entry("messages", display_id)
        if display_id in self._id_map["messages"]:
            return self._id_map["messages"][display_id]
        raise ResourceNotFoundError(
            f"Unknown message #{display_id}. Read a chat first to populate the ID map."
        )

    def _assign_chat_nums(self, chats: list[Chat]) -> None:
        def update(id_map: dict) -> None:
            id_map["chats"] = {}
            for index, chat in enumerate(chats, 1):
                chat.display_num = index
                id_map["chats"][str(index)] = chat.id

        self._update_id_map(update)
        self._next_chat_num = len(chats) + 1

    def _assign_message_nums(self, messages: list[Message]) -> None:
        def update(id_map: dict) -> None:
            msg_map = id_map.setdefault("messages", {})
            next_msg_num = max(
                (int(k) for k in msg_map if k.isdigit()),
                default=0,
            ) + 1
            existing_by_message = {
                (value.get("conv", ""), str(value.get("msg"))): int(key)
                for key, value in msg_map.items()
                if key.isdigit() and isinstance(value, dict) and value.get("msg")
            }

            for msg in messages:
                identity = (msg.conversation_id, str(msg.id))
                existing_num = existing_by_message.get(identity)
                if existing_num is not None:
                    msg.display_num = existing_num
                    continue

                msg.display_num = next_msg_num
                msg_map[str(next_msg_num)] = {
                    "conv": msg.conversation_id,
                    "msg": msg.id,
                }
                existing_by_message[identity] = next_msg_num
                next_msg_num += 1

            self._evict_old_entries_from_map(id_map, "messages")

        self._update_id_map(update)

    def _resolve_chat_titles(self, messages: list[Message]) -> None:
        """Resolve conversation IDs to chat titles for search results."""
        wanted = {m.conversation_id for m in messages if m.conversation_id}
        if not wanted:
            return
        conv_to_title = {
            cid: value["title"] for cid in wanted
            if (value := self._metadata.get("chats", cid)) and value.get("title")
        }
        conv_to_title.update({m.conversation_id: m.chat_title for m in messages if m.chat_title})
        missing = wanted - set(conv_to_title)
        if missing:
            try:
                # Resolve names for result conversations only, rather than every
                # chat in the account. Reuse an earlier list response when possible.
                conversations = self._get_conversations()
                chats = [Chat.from_api(conv, my_user_id=self._user_id)
                         for conv in conversations if conv.get("id") in missing]
                self._resolve_1on1_chat_names(chats)
                self._cache_chat_metadata(chats)
                conv_to_title.update({chat.id: chat.display_title for chat in chats})
            except (httpx.HTTPStatusError, AuthRequiredError, TokenExpiredError, RateLimitError, RetryableError, ValueError):
                pass

        # Enrichment must not renumber a previously displayed chat list.
        def update(id_map: dict) -> None:
            chat_map = id_map.setdefault("chats", {})
            existing = set(chat_map.values())
            next_num = max((int(k) for k in chat_map if k.isdigit()), default=0) + 1
            for cid in sorted(wanted):
                if cid not in existing:
                    chat_map[str(next_num)] = cid
                    existing.add(cid)
                    next_num += 1
            self._evict_old_entries_from_map(id_map, "chats")
        self._update_id_map(update)
        conv_to_num = {cid: num for num, cid in self._id_map["chats"].items()}
        for msg in messages:
            if not msg.conversation_id:
                continue
            num = conv_to_num.get(msg.conversation_id)
            if num:
                title = conv_to_title.get(msg.conversation_id, "")
                if title:
                    msg.chat_title = f"#{num} {title}"
                else:
                    msg.chat_title = f"Chat #{num}"

    def _evict_old_entries(self, section: str) -> None:
        self._evict_old_entries_from_map(self._id_map, section)

    @classmethod
    def _evict_old_entries_from_map(cls, id_map: dict, section: str) -> None:
        entries = id_map.get(section, {})
        numeric = sorted(
            ((int(k), k) for k in entries if k.isdigit()),
            key=lambda x: x[0],
        )
        if len(numeric) <= cls.MAX_ID_MAP_SIZE:
            return
        to_remove = numeric[: len(numeric) - cls.MAX_ID_MAP_SIZE]
        for _, k in to_remove:
            del entries[k]

    def _load_id_map(self) -> dict:
        return self._normalize_id_map(self._read_id_map_from_disk())

    @staticmethod
    def _parse_date_filter(value: str) -> datetime:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    @staticmethod
    def _normalize_id_map(id_map: dict | None) -> dict:
        if not isinstance(id_map, dict):
            id_map = {}
        id_map.setdefault("chats", {})
        id_map.setdefault("messages", {})
        return id_map

    def _read_id_map_from_disk(self) -> dict:
        if ID_MAP_FILE.exists():
            try:
                return json.loads(ID_MAP_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {"chats": {}, "messages": {}}

    def _refresh_id_map(self) -> None:
        self._id_map = self._load_id_map()
        self._next_chat_num = max(
            (int(k) for k in self._id_map["chats"] if k.isdigit()), default=0
        ) + 1
        self._next_msg_num = max(
            (int(k) for k in self._id_map["messages"] if k.isdigit()), default=0
        ) + 1

    def _refresh_id_map_entry(self, section: str, key: str) -> None:
        if key not in self._id_map.get(section, {}):
            self._refresh_id_map()

    def _refresh_misc_id_map_entry(self, key: str) -> None:
        if key not in self._id_map:
            self._refresh_id_map()

    @contextmanager
    def _id_map_lock(self) -> Iterator[None]:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        lock_path = Path(f"{ID_MAP_FILE}.lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a+") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _write_id_map_to_disk(self, id_map: dict) -> None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            dir=str(CACHE_DIR),
            prefix="id_map.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            json.dump(id_map, temp_file)
            temp_path = Path(temp_file.name)
        os.replace(temp_path, ID_MAP_FILE)

    def _update_id_map(self, updater) -> None:
        with self._id_map_lock():
            id_map = self._normalize_id_map(self._read_id_map_from_disk())
            updater(id_map)
            self._write_id_map_to_disk(id_map)
        self._id_map = id_map
        self._next_chat_num = max(
            (int(k) for k in self._id_map["chats"] if k.isdigit()), default=0
        ) + 1
        self._next_msg_num = max(
            (int(k) for k in self._id_map["messages"] if k.isdigit()), default=0
        ) + 1

    # ------------------------------------------------------------------
    # HTTP helpers — retry wrapper
    # ------------------------------------------------------------------

    def _request_with_retry(
        self,
        method: str,
        url: str,
        headers: dict,
        params: dict | None = None,
        json_data: dict | None = None,
        max_retries: int = 3,
        safe_read: bool = False,
    ) -> dict:
        """Execute HTTP request with retry hardening for 429/5xx/network failures."""
        resp = self._request_raw_with_retry(
            method,
            url,
            headers=headers,
            params=params,
            json_data=json_data,
            max_retries=max_retries,
            safe_read=safe_read,
        )
        return self._handle_response(resp)

    def _request_raw_with_retry(
        self,
        method: str,
        url: str,
        headers: dict,
        params: dict | None = None,
        json_data: dict | list | None = None,
        max_retries: int = 3,
        safe_read: bool = False,
    ) -> httpx.Response:
        self._circuit_breaker.raise_if_open()
        retries_429 = 0
        retries_5xx = 0
        retries_network = 0
        refreshed_auth = False
        can_replay = method.upper() == "GET" or safe_read
        headers = dict(headers)
        resource = self._auth_resource(url)
        if resource and self._tokens.get(resource):
            from .auth import token_is_fresh
            if not token_is_fresh(self._tokens, resource):
                self._refresh_auth_headers(headers, resource, force=False)

        while True:
            try:
                resp = self._session.client.request(
                    method, url, headers=headers, params=params, json=json_data,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                self._circuit_breaker.record_failure()
                if not can_replay or retries_network >= self.MAX_RETRIES_5XX:
                    raise RetryableError(str(exc)) from exc
                retries_network += 1
                time.sleep(1)
                continue

            if resp.status_code < 400:
                self._circuit_breaker.record_success()
                return resp

            if resp.status_code == 401 and can_replay and resource and not refreshed_auth:
                self._refresh_auth_headers(headers, resource, force=True)
                refreshed_auth = True
                continue

            if resp.status_code == 429:
                if retries_429 >= min(max_retries, self.MAX_RETRIES_429):
                    self._circuit_breaker.record_failure()
                    return resp
                retries_429 += 1
                time.sleep(self._retry_after_delay(resp, retries_429))
                continue

            if resp.status_code >= 500:
                self._circuit_breaker.record_failure()
                if not can_replay or retries_5xx >= self.MAX_RETRIES_5XX:
                    return resp
                retries_5xx += 1
                time.sleep(1)
                continue

            return resp

    def _auth_resource(self, url: str) -> str | None:
        """Route only known authenticated hosts; never refresh arbitrary URLs."""
        if url.startswith(SUBSTRATE_SEARCH_BASE):
            return "substrate"
        if url.startswith(GRAPH_BASE.rsplit("/", 1)[0] + "/"):
            return "graph" if self._graph else "ic3"
        if url.startswith(self._ups + "/"):
            return "presence"
        if url.startswith(self._chatsvc + "/") or url.startswith(self._mt + "/"):
            return "ic3"
        return None

    def _refresh_auth_headers(self, headers: dict, resource: str, *, force: bool) -> None:
        from .auth import refresh_tokens
        refreshed = refresh_tokens(self._tokens, required=(resource,), force=force)
        self._tokens = refreshed
        self._ic3 = refreshed.get("ic3", "")
        self._graph = refreshed.get("graph", "")
        self._presence_token = refreshed.get("presence", "")
        self._substrate = refreshed.get("substrate", "")
        for key in list(headers):
            if key.lower() == "authorization":
                headers[key] = "Bearer " + refreshed.get(resource, "")
                break

    # ------------------------------------------------------------------
    # HTTP helpers — IC3 Chat Service
    # ------------------------------------------------------------------

    def _ic3_get(self, path: str, params: dict | None = None) -> dict:
        self._session.jitter(is_write=False)
        headers = self._session.browser_headers(self._ic3)
        url = f"{self._chatsvc}{path}"
        return self._request_with_retry("GET", url, headers, params=params)

    def _ic3_post(
        self,
        path: str,
        json_data: dict | None = None,
        is_write: bool = False,
    ) -> dict:
        self._session.jitter(is_write=is_write)
        headers = self._session.browser_headers(self._ic3)
        url = f"{self._chatsvc}{path}"
        return self._request_with_retry("POST", url, headers, json_data=json_data)

    def _ic3_post_raw(
        self,
        path: str,
        json_data: dict | None = None,
        is_write: bool = False,
    ) -> httpx.Response:
        self._session.jitter(is_write=is_write)
        headers = self._session.browser_headers(self._ic3)
        url = f"{self._chatsvc}{path}"
        return self._request_raw_with_retry("POST", url, headers, json_data=json_data)

    def _ic3_put(
        self,
        path: str,
        json_data: dict | None = None,
    ) -> dict:
        self._session.jitter(is_write=True)
        headers = self._session.browser_headers(self._ic3)
        url = f"{self._chatsvc}{path}"
        return self._request_with_retry("PUT", url, headers, json_data=json_data)

    def _ic3_delete(
        self,
        path: str,
        json_data: dict | None = None,
    ) -> dict:
        self._session.jitter(is_write=True)
        headers = self._session.browser_headers(self._ic3)
        url = f"{self._chatsvc}{path}"
        return self._request_with_retry("DELETE", url, headers, json_data=json_data)

    # ------------------------------------------------------------------
    # HTTP helpers — Middle Tier
    # ------------------------------------------------------------------

    def _mt_post(self, path: str, json_data: dict | None = None) -> dict:
        self._session.jitter(is_write=False)
        headers = self._session.browser_headers(self._ic3)
        url = f"{self._mt}{path}"
        return self._request_with_retry("POST", url, headers, json_data=json_data)

    def _ups_post(self, path: str, json_data: dict | list | None = None):
        self._session.jitter(is_write=False)
        headers = self._session.browser_headers(self._presence_token)
        url = f"{self._ups}{path}"
        return self._request_with_retry("POST", url, headers, json_data=json_data, safe_read=True)

    def _ups_put(self, path: str, json_data: dict | None = None):
        self._session.jitter(is_write=True)
        headers = self._session.browser_headers(self._presence_token)
        url = f"{self._ups}{path}"
        return self._request_with_retry("PUT", url, headers, json_data=json_data)

    # ------------------------------------------------------------------
    # HTTP helpers — Graph
    # ------------------------------------------------------------------

    def _graph_batch_get(self, paths: dict[str, str]) -> dict[str, dict]:
        """GET-only batch transport; a POST envelope does not mutate Graph."""
        if len(paths) > 20:
            raise ValueError("Graph batches support at most 20 requests")
        if not paths:
            return {}
        if any(not p.startswith("/") or p.startswith("//") or "://" in p for p in paths.values()):
            raise ValueError("Graph batch paths must be relative")
        pending = dict(paths)
        results: dict[str, dict] = {}
        for attempt in range(self.MAX_RETRIES_429 + 1):
            self._session.jitter(is_write=False)
            headers = self._session.browser_headers(self._graph or self._ic3)
            data = self._request_with_retry("POST", f"{GRAPH_BASE}/$batch", headers, json_data={
                "requests": [{"id": uid, "method": "GET", "url": path} for uid, path in pending.items()],
            }, safe_read=True)
            retry: dict[str, str] = {}
            delay = 0.0
            for response in data.get("responses", []):
                uid = response.get("id", "")
                if uid not in pending:
                    continue
                status = response.get("status", 0)
                body = response.get("body", {})
                if status == 200 and isinstance(body, dict):
                    results[uid] = body
                elif status == 404:
                    results[uid] = {"_not_found": True}
                elif status == 429:
                    retry[uid] = pending[uid]
                    retry_headers = {k.lower(): str(v) for k, v in response.get("headers", {}).items()}
                    delay = max(delay, self._parse_retry_after(retry_headers.get("retry-after", "")) or 2 ** attempt)
            if not retry:
                break
            if attempt == self.MAX_RETRIES_429:
                # Retain successful names, but do not persist failure placeholders.
                break
            time.sleep(delay)
            pending = retry
        return results

    def _graph_get(self, path: str, params: dict | None = None) -> dict:
        token = self._graph or self._ic3
        self._session.jitter(is_write=False)
        headers = self._session.browser_headers(token)
        url = f"{GRAPH_BASE}{path}"
        return self._request_with_retry("GET", url, headers, params=params)

    def _graph_post(
        self,
        path: str,
        json_data: dict | None = None,
        beta: bool = False,
    ) -> dict:
        token = self._graph or self._ic3
        self._session.jitter(is_write=True)
        headers = self._session.browser_headers(token)
        base = "https://graph.microsoft.com/beta" if beta else GRAPH_BASE
        url = f"{base}{path}"
        return self._request_with_retry("POST", url, headers, json_data=json_data)

    def _graph_put(self, path: str, json_data: dict | None = None) -> dict:
        token = self._graph or self._ic3
        self._session.jitter(is_write=True)
        headers = self._session.browser_headers(token)
        url = f"{GRAPH_BASE}{path}"
        return self._request_with_retry("PUT", url, headers, json_data=json_data)

    # ------------------------------------------------------------------
    # Response handling
    # ------------------------------------------------------------------

    def _handle_response(self, resp: httpx.Response) -> dict:
        if resp.status_code == 401:
            raise TokenExpiredError("Token expired. Run: teams login")

        if resp.status_code == 429:
            raise RateLimitError("Rate limited")

        if resp.status_code == 404:
            return {}

        if resp.status_code == 201:
            # Created — common for send operations
            if not resp.content:
                return {"status": "created"}
            try:
                return resp.json()
            except (json.JSONDecodeError, ValueError):
                return {"status": "created"}

        if resp.status_code == 204:
            return {}

        resp.raise_for_status()

        if not resp.content:
            return {}
        return resp.json()

    def _retry_after_delay(self, resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After", "")
        parsed = self._parse_retry_after(retry_after)
        if parsed is not None:
            return parsed

        base_delay = float(2 ** max(attempt - 1, 0))
        return base_delay + random.uniform(0.0, 0.25)

    @staticmethod
    def _parse_retry_after(value: str) -> float | None:
        value = (value or "").strip()
        if not value:
            return None

        if value.isdigit():
            return float(value)

        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None

        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


class _CircuitBreaker:
    def __init__(self, threshold: int = 5, reset_seconds: int = 30):
        self.threshold = threshold
        self.reset_seconds = reset_seconds
        self.failures = 0
        self.opened_at = 0.0

    @property
    def is_open(self) -> bool:
        if not self.opened_at:
            return False
        if time.time() - self.opened_at >= self.reset_seconds:
            self.failures = 0
            self.opened_at = 0.0
            return False
        return True

    def raise_if_open(self) -> None:
        if self.is_open:
            raise RetryableError("Circuit breaker is open. Try again later.")

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = 0.0

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.time()


# Backward-compat re-exports (exceptions now live in exceptions.py)
__all__ = ["TeamsClient", "TokenExpiredError", "RateLimitError", "_CircuitBreaker"]
