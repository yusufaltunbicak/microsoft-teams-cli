from __future__ import annotations

import json

import httpx
import pytest

import teams_cli.cli as cli_mod
import teams_cli.commands.search as cmd_search
import teams_cli.commands.send as cmd_send
import teams_cli.exceptions as exc_mod
import teams_cli.serialization as serialization_mod
from teams_cli.client import TeamsClient, TokenExpiredError
from teams_cli.models import Attachment


def test_get_tenant_id_falls_back_to_ic3_and_ignores_invalid_substrate(token_factory):
    tokens = {
        "ic3": token_factory(tid="tenant-abc"),
        "graph": "",
        "presence": "",
        "csa": "",
        "substrate": "broken-token",
        "region": "emea",
        "user_id": "user-123",
    }

    client = TeamsClient(tokens)

    assert client._get_tenant_id() == "tenant-abc"


def test_get_me_falls_back_to_local_token_name(mocker, teams_client: TeamsClient):
    mocker.patch("teams_cli.auth._decode_display_name", return_value="Local User")
    teams_client._graph_get = lambda path: (_ for _ in ()).throw(TokenExpiredError())  # type: ignore[method-assign]

    me = teams_client.get_me()

    assert me == {
        "displayName": "Local User",
        "user_id": "user-123",
        "region": "emea",
    }


def test_get_presence_reraises_forbidden_without_presence_token(teams_client: TeamsClient):
    def fake_graph_get(path: str, params: dict | None = None):
        request = httpx.Request("GET", "https://graph.microsoft.com/v1.0/me/presence")
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    teams_client._presence_token = ""
    teams_client._graph_get = fake_graph_get  # type: ignore[method-assign]

    with pytest.raises(httpx.HTTPStatusError):
        teams_client.get_presence()


def test_reply_helpers_cover_sender_preview_and_html(teams_client: TeamsClient, make_message, make_attachment):
    message = make_message(
        sender="Alice",
        sender_id="user-999",
        text_content="   ",
        attachments=[make_attachment(name="diagram.png")],
    )

    assert teams_client._reply_sender_mri("user-999") == "8:orgid:user-999"
    assert teams_client._reply_sender_mri("8:orgid:user-999") == "8:orgid:user-999"
    assert teams_client._reply_preview_text(message) == "diagram.png"
    assert "diagram.png" in teams_client._build_reply_html(message, "Looks good")


def test_search_users_email_path_hits_users_endpoint(teams_client: TeamsClient):
    captured: dict[str, object] = {}

    def fake_graph_get(path: str, params: dict | None = None) -> dict:
        captured["path"] = path
        captured["params"] = params
        return {
            "value": [
                {
                    "id": "user-1",
                    "displayName": "Alice",
                    "mail": "alice@example.com",
                    "userType": "Member",
                }
            ]
        }

    teams_client._graph_get = fake_graph_get  # type: ignore[method-assign]

    users = teams_client.search_users("alice@example.com", top=3)

    assert captured["path"] == "/users"
    assert captured["params"] == {
        "$filter": "mail eq 'alice@example.com' or userPrincipalName eq 'alice@example.com'",
        "$top": 3,
    }
    assert users[0].email == "alice@example.com"


def test_rank_users_prioritizes_exact_name_prefix_and_email(make_user):
    users = [
        make_user(user_id="3", display_name="Bob Stone", email="bob@example.com"),
        make_user(user_id="2", display_name="Alice Brown", email="alice@example.com"),
        make_user(user_id="1", display_name="Alice", email="a@example.com"),
    ]

    ranked = TeamsClient._rank_users_by_query(users, "alice")

    assert [user.id for user in ranked] == ["1", "2", "3"]


def test_conv_id_matching_uses_other_party_not_any_occurrence(teams_client: TeamsClient):
    assert teams_client._conv_id_matches_user("19:user-123_user-456@unq.gbl.spaces", "user-456") is True
    assert teams_client._conv_id_matches_user("19:user-123_user-456@unq.gbl.spaces", "user-123") is False
    assert teams_client._conv_id_matches_user("19:user-123_user-123@unq.gbl.spaces", "user-123") is True


def test_read_id_map_and_lock_cover_corrupt_files_and_missing_fcntl(
    teams_client: TeamsClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from teams_cli.constants import ID_MAP_FILE

    ID_MAP_FILE.write_text("{broken json")

    assert teams_client._read_id_map_from_disk() == {"chats": {}, "messages": {}}
    assert teams_client._normalize_id_map({"messages": {"1": {"msg": "a"}}}) == {
        "messages": {"1": {"msg": "a"}},
        "chats": {},
        "meetings": {},
    }

    monkeypatch.setattr("teams_cli.client.fcntl", None)
    with teams_client._id_map_lock():
        pass


def test_json_envelope_snapshots_are_stable(make_user):
    payload = serialization_mod.to_json([make_user(user_id="user-1", display_name="Alice")], pretty=True)
    error = serialization_mod.to_json_error("boom")

    parsed_payload = json.loads(payload)
    parsed_error = json.loads(error)

    assert list(parsed_payload.keys()) == ["ok", "schema_version", "data"]
    assert parsed_payload["data"][0]["display_name"] == "Alice"
    assert parsed_error == {
        "ok": False,
        "schema_version": "1.0",
        "error": "boom",
    }


def test_exception_codes_remain_stable():
    assert exc_mod.AuthRequiredError().code == "auth_required"
    assert exc_mod.TokenExpiredError().code == "token_expired"
    assert exc_mod.RateLimitError().code == "rate_limited"
    assert exc_mod.ResourceNotFoundError().code == "not_found"
    api_error = exc_mod.ApiError("bad gateway", status_code=502)
    assert api_error.code == "api_error"
    assert api_error.status_code == 502


def test_send_command_refuses_uncertain_match_with_yes(runner, console_capture, mocker, make_user):
    users = [make_user(user_id="user-1", display_name="Alice Brown", email="alice@example.com")]

    class FakeClient:
        def __init__(self) -> None:
            self.sent: list[tuple[str, str, bool]] = []

        def search_users(self, query: str, top: int = 10):
            return users

        def send_message_to_user(self, user_id: str, message: str, html: bool = True):
            self.sent.append((user_id, message, html))

    fake_client = FakeClient()
    mocker.patch.object(cmd_send, "_get_client", return_value=fake_client)

    result = runner.invoke(cli_mod.cli, ["send", "carol", "Hello", "-y"])

    assert result.exit_code == 0
    assert fake_client.sent == []
    rendered = console_capture.getvalue()
    assert "No exact match for 'carol'" in rendered
    assert "Refusing to send with -y" in rendered


def test_search_command_applies_offset_after_client_results(runner, mocker, make_message):
    messages = [make_message(msg_id="m1"), make_message(msg_id="m2"), make_message(msg_id="m3")]

    class FakeClient:
        def search_messages(self, query: str, top: int = 25, **kwargs):
            assert top == 4
            return messages

    mocker.patch.object(cmd_search, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["search", "deploy", "--offset", "1", "--max", "3", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert [item["id"] for item in payload["data"]] == ["m2", "m3"]


def test_reply_preview_falls_back_to_placeholder_without_text_or_attachments(make_message):
    message = make_message(text_content="   ", attachments=[])

    assert TeamsClient._reply_preview_text(message) == "[message]"


def test_attachment_inline_image_factory_extracts_object_id():
    attachment = Attachment.from_inline_image(
        "https://img.asm.skype.com/v1/objects/object-5/views/imgo",
        img_type="jpeg",
        index=1,
    )

    assert attachment == Attachment(
        id="object-5",
        name="image_2.jpeg",
        content_type="image/jpeg",
        content_url="https://img.asm.skype.com/v1/objects/object-5/views/imgo",
        size=0,
        is_inline=True,
    )
