from __future__ import annotations

from datetime import datetime, timezone

import pytest
import respx
from httpx import HTTPStatusError, Request, Response

from teams_cli.client import TeamsClient
import teams_cli.exceptions as exc_mod
from teams_cli.models import Attachment


def test_get_chat_messages_applies_after_before_filters(teams_client):
    teams_client._id_map["chats"] = {"1": "conv-1"}
    captured_params: dict[str, object] = {}

    def fake_ic3_get(_path: str, params: dict | None = None) -> dict:
        captured_params.update(params or {})
        return {
            "messages": [
                {
                    "id": "m3",
                    "conversationid": "conv-1",
                    "imdisplayname": "Sender",
                    "from": "8:orgid:other",
                    "content": "<p>Newest</p>",
                    "messagetype": "RichText/Html",
                    "composetime": "2026-03-11T11:30:00Z",
                },
                {
                    "id": "m2",
                    "conversationid": "conv-1",
                    "imdisplayname": "Sender",
                    "from": "8:orgid:other",
                    "content": "<p>Inside window</p>",
                    "messagetype": "RichText/Html",
                    "composetime": "2026-03-11T10:30:00Z",
                },
                {
                    "id": "m1",
                    "conversationid": "conv-1",
                    "imdisplayname": "Sender",
                    "from": "8:orgid:other",
                    "content": "<p>Oldest</p>",
                    "messagetype": "RichText/Html",
                    "composetime": "2026-03-11T09:30:00Z",
                },
            ]
        }

    teams_client._ic3_get = fake_ic3_get  # type: ignore[method-assign]

    messages = teams_client.get_chat_messages(
        "1",
        top=2,
        after="2026-03-11T10:00:00Z",
        before="2026-03-11T11:00:00Z",
    )

    assert [message.id for message in messages] == ["m2"]
    assert captured_params["pageSize"] == 100


def test_search_messages_filters_and_resolves_chat_titles(teams_client, make_message):
    search_results = [
        make_message(
            msg_id="m1",
            conv_id="conv-1",
            sender="Alice",
            timestamp=datetime(2026, 3, 11, 10, 30, tzinfo=timezone.utc),
            text_content="deploy complete",
        ),
        make_message(
            msg_id="m2",
            conv_id="conv-2",
            sender="Bob",
            timestamp=datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc),
            text_content="deploy failed",
        ),
    ]

    def fake_substrate_search(query: str, top: int):
        assert query == "deploy"
        return search_results

    def fake_ic3_get(path: str, params: dict | None = None) -> dict:
        if path == "/users/ME/conversations":
            return {
                "conversations": [
                    {
                        "id": "conv-1",
                        "threadProperties": {"topic": "Alpha"},
                        "lastMessage": {
                            "content": "<p>latest</p>",
                            "imdisplayname": "Alice",
                            "composetime": "2026-03-11T10:30:00Z",
                        },
                    }
                ]
            }
        raise AssertionError(f"unexpected path {path}")

    teams_client._substrate_search = fake_substrate_search  # type: ignore[method-assign]
    teams_client._ic3_get = fake_ic3_get  # type: ignore[method-assign]
    teams_client._resolve_chat_id = lambda chat_num: "conv-1"  # type: ignore[method-assign]

    results = teams_client.search_messages(
        "deploy",
        top=5,
        chat_num="1",
        from_filter="alice",
        after="2026-03-11T10:00:00Z",
        before="2026-03-11T11:00:00Z",
    )

    assert [message.id for message in results] == ["m1"]
    assert results[0].chat_title == "#1 Alpha"


def test_mt_search_fallback_scans_recent_chats(teams_client, make_chat, make_message):
    teams_client._substrate = ""
    first_chat = make_chat("conv-1", "Alpha")
    first_chat.display_num = 1
    second_chat = make_chat("conv-2", "Beta")
    second_chat.display_num = 2

    def fake_get_chats(top: int = 10):
        return [first_chat, second_chat]

    def fake_get_chat_messages(chat_num: str, top: int = 50):
        if chat_num == "1":
            return [make_message("m1", "conv-1", text_content="hello deploy")]
        return [make_message("m2", "conv-2", text_content="nothing to see")]

    teams_client.get_chats = fake_get_chats  # type: ignore[method-assign]
    teams_client.get_chat_messages = fake_get_chat_messages  # type: ignore[method-assign]

    results = teams_client.search_messages("deploy", top=10)

    assert [message.id for message in results] == ["m1"]


def test_chat_and_read_stay_consistent(teams_client):
    teams_client._id_map["chats"] = {"1": "conv-1"}

    def fake_ic3_get(path: str, params: dict | None = None) -> dict:
        assert path == "/users/ME/conversations/conv-1/messages"
        return {
            "messages": [
                {
                    "id": "m2",
                    "conversationid": "conv-1",
                    "imdisplayname": "Sender",
                    "from": "8:orgid:other",
                    "content": "<p>Second</p>",
                    "messagetype": "RichText/Html",
                    "composetime": "2026-03-11T11:00:00Z",
                },
                {
                    "id": "m1",
                    "conversationid": "conv-1",
                    "imdisplayname": "Sender",
                    "from": "8:orgid:other",
                    "content": "<p>First</p>",
                    "messagetype": "RichText/Html",
                    "composetime": "2026-03-11T10:00:00Z",
                },
            ]
        }

    teams_client._ic3_get = fake_ic3_get  # type: ignore[method-assign]

    messages = teams_client.get_chat_messages("1", top=10)
    detail = teams_client.get_message_detail(str(messages[0].display_num))

    assert messages[0].id == "m1"
    assert detail.id == "m1"
    assert detail.display_num == messages[0].display_num


def test_parse_substrate_result_extracts_attachments(teams_client):
    result = {
        "HitHighlightedSummary": "<p>Quarterly report</p>",
        "Source": {
            "ClientThreadId": "conv-1",
            "InternetMessageId": "msg-1",
            "DateTimeSent": "2026-03-11T10:00:00Z",
            "DisplayTo": "",
            "Preview": "<p>Quarterly report</p>",
            "Extensions": {
                "SkypeSpaces_ConversationPost_Extension_FromSkypeInternalId": "8:orgid:user-123",
                "SkypeSpaces_ConversationPost_Extension_FileData": (
                    '[{"id":"att-1","fileName":"report.pdf","fileType":"pdf","objectUrl":"https://files.example/report.pdf"}]'
                ),
            },
        },
    }

    message = teams_client._parse_substrate_result(result)

    assert message is not None
    assert message.id == "msg-1"
    assert message.attachments[0].name == "report.pdf"
    assert message.is_from_me is True


@respx.mock
def test_download_attachment_sharepoint_uses_graph_shares_api(teams_client, make_attachment):
    attachment = make_attachment(
        name="report.pdf",
        content_type="application/pdf",
        content_url="https://tenant.sharepoint.com/sites/example/report.pdf",
    )
    route = respx.get(url__regex=r"https://graph\.microsoft\.com/v1\.0/shares/.+/driveItem/content").mock(
        return_value=Response(200, content=b"pdf-data")
    )

    content = teams_client.download_attachment(attachment)

    assert route.called
    assert content == b"pdf-data"


@respx.mock
def test_download_attachment_direct_url_uses_ic3_token(teams_client, make_attachment):
    attachment = make_attachment(
        name="inline.png",
        content_type="image/png",
        content_url="https://img.asm.skype.com/object",
    )
    route = respx.get("https://img.asm.skype.com/object").mock(
        return_value=Response(200, content=b"img-data")
    )

    content = teams_client.download_attachment(attachment)

    assert route.called
    assert content == b"img-data"


def test_download_attachment_without_url_raises(teams_client):
    attachment = Attachment(
        id="att-1",
        name="broken",
        content_type="text/plain",
        content_url="",
    )

    with pytest.raises(exc_mod.ResourceNotFoundError, match="No download URL"):
        teams_client.download_attachment(attachment)


def test_get_presence_uses_ups_when_graph_forbidden(teams_client):
    teams_client._presence_token = "presence-token"

    def fake_graph_get(path: str, params: dict | None = None):
        request = Request("GET", f"https://graph.microsoft.com/v1.0{path}")
        response = Response(403, request=request)
        raise HTTPStatusError("forbidden", request=request, response=response)

    def fake_ups_post(path: str, json_data=None):
        assert path == "/presence/getpresence/"
        assert json_data == [{"mri": "8:orgid:user-123"}]
        return [
            {
                "presence": {
                    "availability": "Available",
                    "activity": "Available",
                }
            }
        ]

    teams_client._graph_get = fake_graph_get  # type: ignore[method-assign]
    teams_client._ups_post = fake_ups_post  # type: ignore[method-assign]

    presence = teams_client.get_presence()

    assert presence == {"availability": "Available", "activity": "Available"}
