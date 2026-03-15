from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response

from teams_cli.client import RateLimitError, TeamsClient, TokenExpiredError
from teams_cli.constants import GRAPH_BASE


def test_send_message_uses_browser_like_payload(teams_client):
    captured: dict[str, object] = {}

    def fake_post(_path: str, json_data: dict | None = None, is_write: bool = False) -> dict:
        captured["json"] = json_data or {}
        captured["is_write"] = is_write
        return {"ok": True}

    teams_client._ic3_post = fake_post  # type: ignore[method-assign]

    teams_client.send_message("conv-1", "hello")

    payload = captured["json"]
    assert payload["content"] == "<p>hello</p>"
    assert str(payload["clientmessageid"]).isdigit()
    assert len(str(payload["clientmessageid"])) == 19
    assert "importance" not in payload["properties"]
    assert "subject" not in payload["properties"]
    assert captured["is_write"] is True


def test_send_message_preserves_existing_html(teams_client):
    captured: dict[str, object] = {}

    def fake_post(_path: str, json_data: dict | None = None, is_write: bool = False) -> dict:
        captured["json"] = json_data or {}
        return {"ok": True}

    teams_client._ic3_post = fake_post  # type: ignore[method-assign]

    teams_client.send_message("conv-1", "<div>Hello</div>")

    assert captured["json"]["content"] == "<div>Hello</div>"


def test_reply_to_message_embeds_reply_blockquote(teams_client, make_message):
    captured: dict[str, object] = {}
    original = make_message(
        msg_id="msg-1",
        conv_id="conv-1",
        sender="Alice Johnson",
        sender_id="user-456",
        text_content="meeting moved to room 5",
    )

    def fake_post(_path: str, json_data: dict | None = None, is_write: bool = False) -> dict:
        captured["json"] = json_data or {}
        captured["is_write"] = is_write
        return {"ok": True}

    teams_client._ic3_post = fake_post  # type: ignore[method-assign]
    teams_client.get_message_detail = lambda msg_num: original  # type: ignore[method-assign]

    teams_client.reply_to_message("42", "I will join too")

    payload = captured["json"]
    assert captured["is_write"] is True
    assert payload["conversationid"] == "conv-1"
    assert 'itemtype="http://schema.skype.com/Reply"' in payload["content"]
    assert 'itemid="msg-1"' in payload["content"]
    assert "Alice Johnson" in payload["content"]
    assert "meeting moved to room 5" in payload["content"]
    assert "<p>I will join too</p>" in payload["content"]


@respx.mock
def test_send_file_uploads_and_posts_expected_payload(teams_client, tmp_path: Path, mocker):
    file_path = tmp_path / "report.txt"
    file_path.write_text("quarterly report")

    upload_url = f"{GRAPH_BASE}/me/drive/root:/Microsoft Teams Chat Files/{file_path.name}:/content"
    respx.put(upload_url).mock(
        return_value=Response(
            200,
            json={
                "webUrl": "https://tenant.sharepoint.com/personal/user/Documents/report.txt",
                "id": "file-123",
            },
        )
    )
    respx.post(f"{GRAPH_BASE}/me/drive/items/file-123/createLink").mock(
        return_value=Response(200, json={"link": {"webUrl": "https://share/link"}})
    )
    respx.post(f"{GRAPH_BASE}/me/drive/items/file-123/invite").mock(
        return_value=Response(200, json={"value": []})
    )
    respx.get(f"{GRAPH_BASE}/me/drive/items/file-123/permissions").mock(
        return_value=Response(
            200,
            json={"value": [{"link": {"scope": "organization", "webUrl": "https://share/link"}}]},
        )
    )

    mocker.patch.object(teams_client, "_get_chat_members", return_value=["member@example.com"])
    captured: dict[str, object] = {}

    def fake_post(_path: str, json_data: dict | None = None, is_write: bool = False) -> dict:
        captured["json"] = json_data or {}
        captured["is_write"] = is_write
        return {"ok": True}

    teams_client._ic3_post = fake_post  # type: ignore[method-assign]

    teams_client.send_file("conv-1", str(file_path), message="See attached")

    payload = captured["json"]
    files = json.loads(payload["properties"]["files"])
    assert captured["is_write"] is True
    assert "<p>See attached</p>" in payload["content"]
    assert file_path.name in payload["content"]
    assert files[0]["fileName"] == "report.txt"
    assert files[0]["itemid"] == "file-123"
    assert files[0]["fileInfo"]["shareUrl"] == "https://share/link"


def test_add_and_remove_reaction_use_graph_beta_unicode(teams_client):
    teams_client._id_map["messages"] = {"1": {"conv": "conv-1", "msg": "msg-1"}}
    calls: list[tuple[str, dict, bool]] = []

    def fake_graph_post(path: str, json_data: dict | None = None, beta: bool = False) -> dict:
        calls.append((path, json_data or {}, beta))
        return {}

    teams_client._graph_post = fake_graph_post  # type: ignore[method-assign]

    teams_client.add_reaction("1", "heart")
    teams_client.remove_reaction("1", "laugh")

    assert calls == [
        ("/chats/conv-1/messages/msg-1/setReaction", {"reactionType": "❤️"}, True),
        ("/chats/conv-1/messages/msg-1/unsetReaction", {"reactionType": "😂"}, True),
    ]


def test_request_with_retry_retries_after_429(teams_client, mocker):
    request = httpx.Request("GET", "https://example.com")
    responses = [
        httpx.Response(429, headers={"Retry-After": "3"}, request=request),
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
    sleep.assert_called_once_with(3)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(404, request=httpx.Request("GET", "https://example.com")), {}),
        (httpx.Response(201, request=httpx.Request("POST", "https://example.com")), {"status": "created"}),
        (httpx.Response(204, request=httpx.Request("DELETE", "https://example.com")), {}),
    ],
)
def test_handle_response_variants(teams_client, response: httpx.Response, expected: dict):
    assert teams_client._handle_response(response) == expected


def test_handle_response_raises_on_auth_errors(teams_client):
    response = httpx.Response(401, request=httpx.Request("GET", "https://example.com"))

    with pytest.raises(TokenExpiredError):
        teams_client._handle_response(response)


def test_request_with_retry_raises_after_max_retries(teams_client, mocker):
    request = httpx.Request("GET", "https://example.com")
    responses = [
        httpx.Response(429, request=request),
        httpx.Response(429, request=request),
        httpx.Response(429, request=request),
        httpx.Response(429, request=request),
    ]

    mocker.patch.object(teams_client._session.client, "request", side_effect=responses)
    mocker.patch("teams_cli.client.time.sleep")

    with pytest.raises(RateLimitError):
        teams_client._request_with_retry(
            "GET",
            "https://example.com",
            headers={"Authorization": "Bearer token"},
        )
