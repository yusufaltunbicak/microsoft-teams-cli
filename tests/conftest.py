from __future__ import annotations

import base64
import io
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.console import Console

import teams_cli.serialization as serialization_mod
import teams_cli.auth as auth
import teams_cli.cli as cli
import teams_cli.client as client_mod
import teams_cli.commands._common as commands_common
import teams_cli.commands.attachments as cmd_attachments
import teams_cli.commands.auth as cmd_auth
import teams_cli.commands.chat as cmd_chat
import teams_cli.commands.presence as cmd_presence
import teams_cli.commands.schedule as cmd_schedule
import teams_cli.commands.send as cmd_send
import teams_cli.commands.group_chat as cmd_group_chat
import teams_cli.commands.mark_read as cmd_mark_read
import teams_cli.commands.reactions as cmd_reactions
import teams_cli.constants as constants
import teams_cli.formatter as formatter
import teams_cli.scheduler as scheduler
from teams_cli.client import TeamsClient
from teams_cli.config import DEFAULTS
from teams_cli.models import Attachment, Chat, Message, Reaction, User


def _encode_jwt(payload: dict) -> str:
    header = {"alg": "none", "typ": "JWT"}

    def _part(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{_part(header)}.{_part(payload)}.sig"


@pytest.fixture
def token_factory():
    def _factory(**claims: object) -> str:
        payload = {
            "oid": "user-123",
            "name": "Test User",
            "tid": "tenant-123",
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=2)).timestamp()),
        }
        payload.update(claims)
        return _encode_jwt(payload)

    return _factory


@pytest.fixture
def fake_tokens(token_factory) -> dict[str, str]:
    ic3 = token_factory(oid="user-123", name="Test User")
    return {
        "ic3": ic3,
        "graph": token_factory(oid="user-123", name="Test User"),
        "presence": token_factory(oid="user-123", name="Test User"),
        "csa": token_factory(oid="user-123", name="Test User"),
        "substrate": token_factory(oid="user-123", name="Test User"),
        "region": "emea",
        "user_id": "user-123",
    }


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    if request.node.get_closest_marker("live"):
        cli._client_cache.clear()
        yield {
            "cache_dir": constants.CACHE_DIR,
            "config_dir": constants.CONFIG_DIR,
            "tokens_file": constants.TOKENS_FILE,
            "browser_state_file": constants.BROWSER_STATE_FILE,
            "id_map_file": constants.ID_MAP_FILE,
            "scheduled_file": constants.SCHEDULED_FILE,
            "user_profile_file": constants.USER_PROFILE_FILE,
            "config_file": constants.CONFIG_FILE,
        }
        cli._client_cache.clear()
        return

    cache_dir = tmp_path / "cache"
    config_dir = tmp_path / "config"
    cache_dir.mkdir()
    config_dir.mkdir()

    tokens_file = cache_dir / "tokens.json"
    browser_state_file = cache_dir / "browser-state.json"
    id_map_file = cache_dir / "id_map.json"
    scheduled_file = cache_dir / "scheduled.json"
    user_profile_file = cache_dir / "user_profile.json"
    config_file = config_dir / "config.yaml"

    monkeypatch.setattr(constants, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(constants, "TOKENS_FILE", tokens_file)
    monkeypatch.setattr(constants, "BROWSER_STATE_FILE", browser_state_file)
    monkeypatch.setattr(constants, "ID_MAP_FILE", id_map_file)
    monkeypatch.setattr(constants, "SCHEDULED_FILE", scheduled_file)
    monkeypatch.setattr(constants, "USER_PROFILE_FILE", user_profile_file)
    monkeypatch.setattr(constants, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(constants, "CONFIG_FILE", config_file)

    monkeypatch.setattr(auth, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(auth, "TOKENS_FILE", tokens_file)
    monkeypatch.setattr(auth, "BROWSER_STATE_FILE", browser_state_file)
    monkeypatch.setattr(auth, "USER_PROFILE_FILE", user_profile_file)

    monkeypatch.setattr(client_mod, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(client_mod, "ID_MAP_FILE", id_map_file)

    monkeypatch.setattr(scheduler, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(scheduler, "SCHEDULED_FILE", scheduled_file)

    # In test environment, stdout is not a TTY; disable auto-JSON by default
    monkeypatch.setattr(serialization_mod, "is_piped", lambda: False)
    monkeypatch.setattr(commands_common, "is_piped", lambda: False)

    monkeypatch.setattr(commands_common, "cfg", deepcopy(DEFAULTS))
    monkeypatch.setattr(cli, "cfg", deepcopy(DEFAULTS))
    commands_common._client_cache.clear()
    cli._client_cache.clear()

    yield {
        "cache_dir": cache_dir,
        "config_dir": config_dir,
        "tokens_file": tokens_file,
        "browser_state_file": browser_state_file,
        "id_map_file": id_map_file,
        "scheduled_file": scheduled_file,
        "user_profile_file": user_profile_file,
        "config_file": config_file,
    }

    commands_common._client_cache.clear()
    cli._client_cache.clear()


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def console_capture(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    stream = io.StringIO()
    test_console = Console(
        file=stream,
        width=120,
        force_terminal=False,
        color_system=None,
    )
    monkeypatch.setattr(formatter, "console", test_console)
    monkeypatch.setattr(commands_common, "console", test_console)
    for mod in (cmd_auth, cmd_chat, cmd_send, cmd_presence, cmd_schedule, cmd_attachments, cmd_group_chat, cmd_mark_read, cmd_reactions):
        if hasattr(mod, "console"):
            monkeypatch.setattr(mod, "console", test_console)
    monkeypatch.setattr(cli, "console", test_console)
    return stream


@pytest.fixture
def teams_client(fake_tokens: dict[str, str]) -> TeamsClient:
    client = TeamsClient(fake_tokens)
    client._display_name = "Test User"
    client._session.jitter = lambda is_write=False: None
    return client


@pytest.fixture
def make_chat():
    def _factory(
        chat_id: str = "19:chat-a@thread.v2",
        title: str = "Alpha",
        when: datetime | None = None,
        unread_count: int = 0,
        members: list[str] | None = None,
        sender: str = "Sender",
        preview: str = "preview",
        chat_type: str = "group",
    ) -> Chat:
        return Chat(
            id=chat_id,
            topic=title,
            chat_type=chat_type,
            last_message_preview=preview,
            last_message_time=when or datetime(2026, 3, 11, 10, 0, tzinfo=timezone.utc),
            last_message_sender=sender,
            members=members or [],
            unread_count=unread_count,
        )

    return _factory


@pytest.fixture
def make_reaction():
    def _factory(emoji: str = "like", user: str = "Alice", user_id: str = "user-a") -> Reaction:
        return Reaction(emoji=emoji, user=user, user_id=user_id)

    return _factory


@pytest.fixture
def make_attachment():
    def _factory(
        name: str = "file.txt",
        content_type: str = "text/plain",
        content_url: str = "https://files.example/file.txt",
        size: int = 12,
        is_inline: bool = False,
        attachment_id: str = "att-1",
    ) -> Attachment:
        return Attachment(
            id=attachment_id,
            name=name,
            content_type=content_type,
            content_url=content_url,
            size=size,
            is_inline=is_inline,
        )

    return _factory


@pytest.fixture
def make_message(make_attachment, make_reaction):
    def _factory(
        msg_id: str = "m1",
        conv_id: str = "19:chat-a@thread.v2",
        sender: str = "Sender",
        sender_id: str = "sender-123",
        content: str = "<p>content</p>",
        text_content: str | None = None,
        timestamp: datetime | None = None,
        message_type: str = "RichText/Html",
        is_from_me: bool = False,
        reactions: list[Reaction] | None = None,
        attachments: list[Attachment] | None = None,
    ) -> Message:
        return Message(
            id=msg_id,
            conversation_id=conv_id,
            sender=sender,
            sender_id=sender_id,
            content=content,
            message_type=message_type,
            timestamp=timestamp or datetime(2026, 3, 11, 10, 0, tzinfo=timezone.utc),
            is_from_me=is_from_me,
            text_content=text_content or "content",
            reactions=reactions or [],
            attachments=attachments or [],
        )

    return _factory


@pytest.fixture
def make_user():
    def _factory(
        user_id: str = "user-a",
        display_name: str = "Alice",
        email: str = "alice@example.com",
        user_type: str = "Member",
    ) -> User:
        return User(id=user_id, display_name=display_name, email=email, user_type=user_type)

    return _factory


