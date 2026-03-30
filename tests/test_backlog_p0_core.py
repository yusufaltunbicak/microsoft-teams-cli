from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
import pytest

import teams_cli.commands._common as commands_common
from teams_cli.client import TeamsClient, TokenExpiredError
from teams_cli.models import (
    Attachment,
    Chat,
    User,
    _detect_chat_type,
    _extract_inline_images,
    _parse_dt,
    _parse_unread_count,
    _strip_html,
)


def test_request_with_retry_uses_exponential_backoff_without_retry_after(
    teams_client: TeamsClient,
    mocker,
):
    request = httpx.Request("GET", "https://example.com")
    responses = [
        httpx.Response(429, request=request),
        httpx.Response(200, json={"ok": True}, request=request),
    ]

    mocker.patch.object(teams_client._session.client, "request", side_effect=responses)
    sleep = mocker.patch("teams_cli.client.time.sleep")

    payload = teams_client._request_with_retry(
        "GET",
        "https://example.com",
        headers={"Authorization": "Bearer token"},
    )

    assert payload == {"ok": True}
    sleep.assert_called_once_with(2)


def test_request_with_retry_does_not_retry_non_rate_limit_errors(
    teams_client: TeamsClient,
    mocker,
):
    request = httpx.Request("GET", "https://example.com")
    request_mock = mocker.patch.object(
        teams_client._session.client,
        "request",
        return_value=httpx.Response(401, request=request),
    )

    with pytest.raises(TokenExpiredError):
        teams_client._request_with_retry(
            "GET",
            "https://example.com",
            headers={"Authorization": "Bearer token"},
        )

    assert request_mock.call_count == 1


def test_handle_response_covers_created_and_invalid_json(teams_client: TeamsClient):
    created = teams_client._handle_response(
        httpx.Response(
            201,
            request=httpx.Request("POST", "https://example.com"),
            json={"id": "msg-1"},
        )
    )
    created_without_json = teams_client._handle_response(
        httpx.Response(
            201,
            request=httpx.Request("POST", "https://example.com"),
            content=b"created-but-not-json",
        )
    )

    assert created == {"id": "msg-1"}
    assert created_without_json == {"status": "created"}

    with pytest.raises(json.JSONDecodeError):
        teams_client._handle_response(
            httpx.Response(
                200,
                request=httpx.Request("GET", "https://example.com"),
                content=b"not-json",
            )
        )


def test_ic3_get_builds_browser_headers_and_read_request(teams_client: TeamsClient, mocker):
    jitter = mocker.patch.object(teams_client._session, "jitter")
    headers = mocker.patch.object(
        teams_client._session,
        "browser_headers",
        return_value={"Authorization": "Bearer ic3"},
    )
    request = mocker.patch.object(teams_client, "_request_with_retry", return_value={"ok": True})

    payload = teams_client._ic3_get("/users/ME", params={"top": 1})

    assert payload == {"ok": True}
    jitter.assert_called_once_with(is_write=False)
    headers.assert_called_once_with(teams_client._ic3)
    request.assert_called_once_with(
        "GET",
        f"{teams_client._chatsvc}/users/ME",
        {"Authorization": "Bearer ic3"},
        params={"top": 1},
    )


def test_graph_post_beta_uses_graph_token_and_beta_base(teams_client: TeamsClient, mocker):
    jitter = mocker.patch.object(teams_client._session, "jitter")
    headers = mocker.patch.object(
        teams_client._session,
        "browser_headers",
        return_value={"Authorization": "Bearer graph"},
    )
    request = mocker.patch.object(teams_client, "_request_with_retry", return_value={"ok": True})

    payload = teams_client._graph_post("/chats", json_data={"chatType": "oneOnOne"}, beta=True)

    assert payload == {"ok": True}
    jitter.assert_called_once_with(is_write=True)
    headers.assert_called_once_with(teams_client._graph)
    request.assert_called_once_with(
        "POST",
        "https://graph.microsoft.com/beta/chats",
        {"Authorization": "Bearer graph"},
        json_data={"chatType": "oneOnOne"},
    )


def test_ups_put_uses_presence_token_and_write_jitter(teams_client: TeamsClient, mocker):
    jitter = mocker.patch.object(teams_client._session, "jitter")
    headers = mocker.patch.object(
        teams_client._session,
        "browser_headers",
        return_value={"Authorization": "Bearer presence"},
    )
    request = mocker.patch.object(teams_client, "_request_with_retry", return_value={"ok": True})

    payload = teams_client._ups_put("/me/forceavailability/", {"availability": "Busy"})

    assert payload == {"ok": True}
    jitter.assert_called_once_with(is_write=True)
    headers.assert_called_once_with(teams_client._presence_token)
    request.assert_called_once_with(
        "PUT",
        f"{teams_client._ups}/me/forceavailability/",
        {"Authorization": "Bearer presence"},
        json_data={"availability": "Busy"},
    )


def test_send_message_to_user_routes_self_messages_to_notes(teams_client: TeamsClient):
    captured: list[tuple[str, str, bool]] = []

    def fake_send_message(conv_id: str, content: str, html: bool = True) -> dict:
        captured.append((conv_id, content, html))
        return {"ok": True}

    teams_client.send_message = fake_send_message  # type: ignore[method-assign]

    teams_client.send_message_to_user("user-123", "hello", html=False)

    assert captured == [("48:notes", "hello", False)]


def test_send_message_to_user_creates_or_finds_one_on_one(teams_client: TeamsClient):
    captured: list[tuple[str, str, bool]] = []

    teams_client._get_or_create_1on1 = lambda user_mri: "19:one-on-one@unq.gbl.spaces"  # type: ignore[method-assign]

    def fake_send_message(conv_id: str, content: str, html: bool = True) -> dict:
        captured.append((conv_id, content, html))
        return {"ok": True}

    teams_client.send_message = fake_send_message  # type: ignore[method-assign]

    teams_client.send_message_to_user("other-user", "hello")

    assert captured == [("19:one-on-one@unq.gbl.spaces", "hello", True)]


def test_create_group_chat_extracts_location_and_sets_topic(teams_client: TeamsClient, mocker):
    browser_headers = mocker.patch.object(
        teams_client._session,
        "browser_headers",
        return_value={"Authorization": "Bearer ic3"},
    )
    jitter = mocker.patch.object(teams_client._session, "jitter")
    request = httpx.Request("POST", f"{teams_client._chatsvc}/threads")
    response = httpx.Response(
        201,
        request=request,
        headers={"location": f"{teams_client._chatsvc}/threads/19:group@thread.v2"},
    )
    raw_request = mocker.patch.object(teams_client._session.client, "request", return_value=response)
    set_topic = mocker.patch.object(teams_client, "_ic3_put", return_value={})

    created = teams_client.create_group_chat(["other-user"], topic="Release Room")

    assert created == {"id": "19:group@thread.v2", "status": "created"}
    browser_headers.assert_called_once_with(teams_client._ic3)
    jitter.assert_called_once_with(is_write=True)
    assert raw_request.call_args.kwargs["json"] == {
        "members": [
            {"id": "8:orgid:user-123", "role": "Admin"},
            {"id": "8:orgid:other-user", "role": "Admin"},
        ],
        "properties": {"threadType": "chat"},
    }
    set_topic.assert_called_once_with(
        f"/threads/{quote('19:group@thread.v2', safe='')}/properties?name=topic",
        json_data={"topic": "Release Room"},
    )


def test_create_group_chat_ignores_optional_topic_failures(teams_client: TeamsClient, mocker):
    request = httpx.Request("POST", f"{teams_client._chatsvc}/threads")
    response = httpx.Response(
        201,
        request=request,
        headers={"location": f"{teams_client._chatsvc}/threads/19:group@thread.v2"},
    )
    mocker.patch.object(teams_client._session, "browser_headers", return_value={})
    mocker.patch.object(teams_client._session, "jitter")
    mocker.patch.object(teams_client._session.client, "request", return_value=response)
    mocker.patch.object(teams_client, "_ic3_put", side_effect=RuntimeError("topic failed"))

    created = teams_client.create_group_chat(["other-user"], topic="Release Room")

    assert created == {"id": "19:group@thread.v2", "status": "created"}


def test_create_group_chat_raises_on_forbidden(teams_client: TeamsClient, mocker):
    request = httpx.Request("POST", f"{teams_client._chatsvc}/threads")
    response = httpx.Response(403, request=request)
    mocker.patch.object(teams_client._session, "browser_headers", return_value={})
    mocker.patch.object(teams_client._session, "jitter")
    mocker.patch.object(teams_client._session.client, "request", return_value=response)

    with pytest.raises(httpx.HTTPStatusError):
        teams_client.create_group_chat(["other-user"])


def test_forward_message_escapes_sender_body_and_comment(teams_client: TeamsClient, make_message):
    original = make_message(
        sender='Alice <Admin>',
        text_content="Use <script>alert(1)</script>",
    )
    captured: list[tuple[str, str, bool]] = []

    teams_client.get_message_detail = lambda msg_num: original  # type: ignore[method-assign]

    def fake_send_message_to_chat(chat_num: str, content: str, html: bool = True) -> dict:
        captured.append((chat_num, content, html))
        return {"ok": True}

    teams_client.send_message_to_chat = fake_send_message_to_chat  # type: ignore[method-assign]

    teams_client.forward_message("7", "9", comment='Review "today" & confirm')

    assert captured == [
        (
            "9",
            (
                "<blockquote><b>Forwarded from Alice &lt;Admin&gt;</b><br/>"
                "Use &lt;script&gt;alert(1)&lt;/script&gt;</blockquote>"
                "<p>Review &quot;today&quot; &amp; confirm</p>"
            ),
            True,
        )
    ]


def test_forward_message_uses_placeholder_when_original_text_missing(
    teams_client: TeamsClient,
    make_message,
):
    original = make_message(text_content="   ")
    captured: list[str] = []

    teams_client.get_message_detail = lambda msg_num: original  # type: ignore[method-assign]
    teams_client.send_message_to_chat = lambda chat_num, content, html=True: captured.append(content) or {"ok": True}  # type: ignore[method-assign]

    teams_client.forward_message("1", "2")

    assert "[message]" in captured[0]


def test_mark_message_unread_uses_bookmark_payload_and_quoted_path(
    teams_client: TeamsClient,
    mocker,
):
    captured: dict[str, object] = {}
    teams_client._resolve_message_id = lambda msg_num: {"conv": "19:chat@thread.v2", "msg": "msg-9"}  # type: ignore[method-assign]

    def fake_put(path: str, json_data: dict | None = None) -> dict:
        captured["path"] = path
        captured["json"] = json_data or {}
        return {"ok": True}

    teams_client._ic3_put = fake_put  # type: ignore[method-assign]
    mocker.patch("teams_cli.client.time.time", return_value=1234.5)

    payload = teams_client.mark_message_unread("9")

    assert payload == {"ok": True}
    assert captured["path"] == (
        f"/users/ME/conversations/{quote('19:chat@thread.v2', safe='')}/"
        "properties?name=consumptionHorizonBookmark"
    )
    assert captured["json"] == {
        "consumptionHorizonBookmark": "1234500;1234500;msg-9"
    }


def test_user_parsers_and_string_representation():
    from_api = User.from_api(
        {
            "mri": "8:orgid:user-a",
            "displayName": "Alice",
            "mail": "alice@example.com",
            "userType": "guest",
        }
    )
    from_profile = User.from_profile(
        {
            "mri": "8:orgid:user-b",
            "displayName": "Bob",
            "userPrincipalName": "bob@example.com",
            "type": "member",
        }
    )

    assert from_api.id == "user-a"
    assert str(from_api) == "Alice <alice@example.com>"
    assert from_profile == User(
        id="user-b",
        display_name="Bob",
        email="bob@example.com",
        user_type="member",
    )


def test_chat_parsers_cover_members_unread_and_display_title():
    chat = Chat.from_api(
        {
            "id": "19:group@thread.v2",
            "threadProperties": {
                "members": '[{"friendlyName":"Alice"},{"id":"user-b"},"Carol"]',
                "consumptionhorizon": {"unreadCount": "4"},
            },
            "lastMessage": {
                "content": "<p>Ship it</p>",
                "imdisplayname": "Alice",
                "composetime": "2026-03-11T10:30:00Z",
            },
        }
    )
    graph_chat = Chat.from_graph(
        {
            "id": "19:user-1_user-2@unq.gbl.spaces",
            "chatType": "oneOnOne",
            "lastMessagePreview": {
                "body": {"content": "<p>hello</p>"},
                "createdDateTime": "2026-03-11T10:00:00Z",
                "from": {"user": {"displayName": "Alice"}},
            },
        }
    )

    assert chat.members == ["Alice", "user-b", "Carol"]
    assert chat.unread_count == 4
    assert chat.last_message_preview == "Ship it"
    assert chat.display_title == "Alice, user-b, Carol"
    assert graph_chat.display_title == "1:1 Chat"
    assert graph_chat.last_message_sender == "Alice"


def test_message_from_api_parses_reactions_files_and_inline_images():
    message = Attachment.from_api(
        {"itemid": "doc-1", "fileName": "report.pdf", "fileType": "pdf", "fileInfo": {"fileUrl": "https://files.example/report.pdf"}}
    )

    assert message == Attachment(
        id="doc-1",
        name="report.pdf",
        content_type="pdf",
        content_url="https://files.example/report.pdf",
        size=0,
        is_inline=False,
    )

    parsed = TeamsClient.REACTION_EMOJIS["heart"]
    assert parsed == "❤️"


def test_message_model_parses_system_messages_reactions_and_attachments():
    from teams_cli.models import Message

    data = {
        "id": "msg-1",
        "conversationid": "conv-1",
        "imdisplayname": "Alice",
        "from": "8:orgid:user-123",
        "content": (
            '<p>Hello</p><img src="https://img.asm.skype.com/v1/objects/object-1/views/imgo" '
            'itemscope="gif">'
        ),
        "messagetype": "RichText/Html",
        "composetime": "2026-03-11T10:00:00Z",
        "properties": {
            "emotions": json.dumps(
                [{"key": "heart", "users": [{"value": "Alice", "mri": "8:orgid:user-123"}]}]
            ),
            "files": json.dumps(
                [{"itemid": "file-1", "fileName": "notes.txt", "fileType": "text/plain"}]
            ),
        },
    }

    message = Message.from_api(data, my_user_id="user-123")

    assert message.content.startswith("<p>Hello</p>")
    assert message.is_from_me is True
    assert message.reactions[0].emoji == "heart"
    assert message.attachments[0].name == "notes.txt"
    assert message.attachments[1].is_inline is True
    assert message.attachments[1].name == "image_1.png"

    system_message = Message.from_api(
        {
            **data,
            "messagetype": "ThreadActivity/TopicUpdate",
            "properties": {},
        },
        my_user_id="user-123",
    )

    assert system_message.content == "[ThreadActivity/TopicUpdate]"


def test_model_helpers_cover_edge_cases():
    unread_from_dict = _parse_unread_count({"consumptionhorizon": {"unreadCount": "3"}})
    unread_from_flat = _parse_unread_count({"consumptionhorizon": {"unreadCount": "x"}, "unreadMessageCount": "2"})
    images = _extract_inline_images(
        '<p>hello</p><img src="https://img.asm.skype.com/v1/objects/object-9/views/imgo" itemscope="png">'
    )

    assert unread_from_dict == 3
    assert unread_from_flat == 2
    assert images[0].id == "object-9"
    assert _detect_chat_type("19:user-a_user-b@unq.gbl.spaces", {}) == "oneOnOne"
    assert _detect_chat_type("meeting_abc", {}) == "meeting"
    assert _detect_chat_type("19:group@thread.v2", {}) == "group"
    assert _parse_dt("bad-date") == datetime.min.replace(tzinfo=timezone.utc)
    assert _strip_html("<p>Hello <b>world</b></p>") == "Helloworld"


def test_get_client_caches_constructed_client(mocker):
    commands_common._client_cache.clear()
    tokens = {"ic3": "token", "region": "emea", "user_id": "user-1"}
    get_tokens = mocker.patch.object(commands_common, "get_tokens", return_value=tokens)
    constructor = mocker.patch.object(commands_common, "TeamsClient", side_effect=lambda value: {"tokens": value})

    first = commands_common._get_client()
    second = commands_common._get_client()

    assert first is second
    assert first == {"tokens": tokens}
    get_tokens.assert_called_once_with()
    constructor.assert_called_once_with(tokens)


def test_get_client_exits_cleanly_when_token_lookup_fails(mocker):
    commands_common._client_cache.clear()
    mocker.patch.object(commands_common, "get_tokens", side_effect=RuntimeError("login first"))

    with pytest.raises(commands_common.AuthRequiredError, match="login first"):
        commands_common._get_client()


def test_handle_api_error_relogs_and_retries_successfully(mocker):
    calls = {"count": 0}
    commands_common._client_cache["c"] = object()
    do_login = mocker.patch.object(commands_common, "do_login", return_value={})

    @commands_common._handle_api_error
    def flaky():
        calls["count"] += 1
        if calls["count"] == 1:
            raise TokenExpiredError()
        return "ok"

    assert flaky() == "ok"
    assert calls["count"] == 2
    do_login.assert_called_once_with()
    assert commands_common._client_cache == {}


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ValueError("bad input"), "bad input"),
        (RuntimeError("boom"), "Error: boom"),
    ],
)
def test_handle_api_error_exits_on_non_retryable_errors(mocker, exc: Exception, expected: str):
    @commands_common._handle_api_error
    def broken():
        raise exc

    with pytest.raises(type(exc), match=str(exc)):
        broken()


def test_handle_api_error_exits_when_relogin_fails(mocker):
    mocker.patch.object(commands_common, "do_login", side_effect=RuntimeError("still expired"))

    @commands_common._handle_api_error
    def broken():
        raise TokenExpiredError()

    with pytest.raises(commands_common.AuthRequiredError, match="Auto re-login failed. Run: teams login --force"):
        broken()
