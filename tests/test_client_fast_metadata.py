from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
import pytest

from teams_cli.client import TeamsClient
from teams_cli.exceptions import ResourceNotFoundError
from teams_cli.metadata_cache import MetadataCache


def test_metadata_private_scoped_expiring_and_excludes_messages(tmp_path, monkeypatch):
    cache = MetadataCache(tmp_path, "tenant:user")
    cache.put_many("users", {"alice": {"display_name": "Alice", "message": "private message"}}, ttl=10)
    cache.put_many("chats", {"room": {"title": "Room", "preview": "private message"}}, ttl=10)
    assert os.stat(cache.path).st_mode & 0o777 == 0o600
    assert os.stat(cache.path.with_suffix(".json.lock")).st_mode & 0o777 == 0o600
    assert "private message" not in cache.path.read_text()
    assert MetadataCache(tmp_path, "tenant:user").get("users", "alice") == {"display_name": "Alice"}
    assert MetadataCache(tmp_path, "other:user").get("users", "alice") is None
    monkeypatch.setattr("teams_cli.metadata_cache.time.time", lambda: 10**12)
    assert cache.get("users", "alice") is None


def test_metadata_disabled_corrupt_and_concurrent_writers(tmp_path):
    path = tmp_path / "metadata.json"
    path.write_text("bad json")
    cache = MetadataCache(tmp_path, "tenant:user")
    assert cache.get("users", "alice") is None
    disabled = MetadataCache(tmp_path, "tenant:disabled", enabled=False)
    disabled.put_many("users", {"alice": {"display_name": "Alice"}})
    assert path.read_text() == "bad json"
    writers = [MetadataCache(tmp_path, "tenant:user") for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda pair: pair[1].put_many("users", {str(pair[0]): {"display_name": str(pair[0])}}), enumerate(writers)))
    assert len(MetadataCache(tmp_path, "tenant:user").items("users")) == 4
    assert json.loads(path.read_text())["version"] == 1


def test_user_names_batched_once_and_reused_across_clients(teams_client, fake_tokens, monkeypatch):
    batches = []
    def batch(paths):
        batches.append(paths)
        return {uid: {"displayName": f"Name {uid}"} for uid in paths}
    monkeypatch.setattr(teams_client, "_graph_batch_get", batch)
    monkeypatch.setattr(teams_client, "_graph_get", lambda path, params=None: {"displayName": "Name u40"})
    users = [f"u{i}" for i in range(41)]
    assert len(teams_client._resolve_user_names(users + users)) == 41
    assert [len(paths) for paths in batches] == [20, 20, 1]
    another = TeamsClient(fake_tokens)
    monkeypatch.setattr(another, "_graph_get", lambda *a, **k: pytest.fail("cached names should not need Graph"))
    monkeypatch.setattr(another, "_graph_batch_get", lambda *a, **k: pytest.fail("cached names should not need Graph"))
    assert another._resolve_user_names(users)["u0"] == "Name u0"


def test_graph_batch_is_get_only_and_retries_only_throttled_items(teams_client, monkeypatch):
    calls = []
    sleeps = []
    def request(method, url, headers, json_data=None, **kwargs):
        calls.append(json_data["requests"])
        assert method == "POST" and url.endswith("/$batch") and kwargs["safe_read"]
        assert all(row["method"] == "GET" for row in json_data["requests"])
        if len(calls) == 1:
            return {"responses": [
                {"id": "a", "status": 200, "body": {"displayName": "A"}},
                {"id": "b", "status": 429, "headers": {"Retry-After": "2"}},
                {"id": "c", "status": 403, "body": {"error": "denied"}},
            ]}
        return {"responses": [{"id": "b", "status": 200, "body": {"displayName": "B"}}]}
    monkeypatch.setattr(teams_client, "_request_with_retry", request)
    monkeypatch.setattr("teams_cli.client.time.sleep", sleeps.append)
    assert teams_client._graph_batch_get({"a": "/users/a", "b": "/users/b", "c": "/users/c"}) == {"a": {"displayName": "A"}, "b": {"displayName": "B"}}
    assert [row["id"] for row in calls[1]] == ["b"]
    assert sleeps == [2]
    with pytest.raises(ValueError):
        teams_client._graph_batch_get({"a": "https://other.example/users/a"})
    with pytest.raises(ValueError):
        teams_client._graph_batch_get({str(i): "/users/a" for i in range(21)})


def test_chats_use_known_last_sender_and_summary_coalesces_reads(teams_client, monkeypatch):
    reads = []
    uid = "other-user"
    cid = f"19:{teams_client._user_id}_{uid}@unq.gbl.spaces"
    def read(path, params=None):
        reads.append(path)
        return {"conversations": [{"id": cid, "lastMessage": {
            "from": f"8:orgid:{uid}", "imdisplayname": "Alice", "content": "never persist",
            "composetime": "2026-10-09T10:00:00Z",
        }}]}
    monkeypatch.setattr(teams_client, "_ic3_get", read)
    monkeypatch.setattr(teams_client, "_graph_get", lambda *a, **k: pytest.fail("sender name was supplied"))
    assert teams_client.get_chats()[0].display_title == "Alice"
    teams_client.get_chats(unread_only=True)
    assert len(reads) == 1
    teams_client.get_chats(refresh=True)
    assert len(reads) == 2
    assert "never persist" not in teams_client._metadata.path.read_text()


def test_search_title_enrichment_preserves_prior_chat_numbers(teams_client, make_message, monkeypatch):
    teams_client._update_id_map(lambda value: value["chats"].update({"1": "old-chat"}))
    teams_client._metadata.put_many("chats", {"new-chat": {"title": "New title"}})
    message = make_message(conv_id="new-chat")
    monkeypatch.setattr(teams_client, "_ic3_get", lambda *a, **k: pytest.fail("cached title should not fetch all chats"))
    teams_client._resolve_chat_titles([message])
    assert teams_client._id_map["chats"]["1"] == "old-chat"
    assert message.chat_title == "#2 New title"


def test_chat_names_exact_cached_and_ambiguous_rejected(teams_client, monkeypatch):
    teams_client._metadata.put_many("chats", {"a": {"title": "Project Alpha"}, "b": {"title": "Project Beta"}})
    monkeypatch.setattr(teams_client, "get_chats", lambda **kwargs: [])
    assert teams_client._resolve_chat_id("project alpha") == "a"
    with pytest.raises(ResourceNotFoundError, match="ambiguous"):
        teams_client._resolve_chat_id("Project")


def test_message_detail_targets_old_message_and_escapes_ids(teams_client, monkeypatch):
    teams_client._update_id_map(lambda value: value["messages"].update({"1": {"conv": "19:a@thread.v2", "msg": "old/id"}}))
    def read(path, params=None):
        assert path == "/users/ME/conversations/19%3Aa%40thread.v2/messages/old%2Fid"
        return {"id": "old/id", "content": "anchor", "messagetype": "Text"}
    monkeypatch.setattr(teams_client, "_ic3_get", read)
    message = teams_client.get_message_detail("#1")
    assert message.id == "old/id" and message.display_num == 1
    assert message.conversation_id == "19:a@thread.v2"


def test_search_parses_native_sender_and_title_without_graph(teams_client, monkeypatch):
    monkeypatch.setattr(teams_client, "_graph_get", lambda *a, **k: pytest.fail("native sender should avoid Graph"))
    monkeypatch.setattr(teams_client, "_graph_batch_get", lambda *a, **k: pytest.fail("native sender should avoid Graph"))
    monkeypatch.setattr(teams_client, "_ic3_get", lambda *a, **k: pytest.fail("native title should avoid chat list"))
    result = {"Source": {
        "ClientThreadId": "group", "InternetMessageId": "42", "Preview": "matching text",
        "ConversationTopic": "Team project", "Sender": {"EmailAddress": {"Name": "Alice"}},
        "Extensions": {"SkypeSpaces_ConversationPost_Extension_FromSkypeInternalId": "8:orgid:alice"},
    }}
    monkeypatch.setattr(teams_client, "_request_with_retry", lambda *a, **k: {"EntitySets": [{"ResultSets": [{"ContentSources": ["Teams"], "Results": [result]}]}]})
    messages = teams_client.search_messages("matching")
    assert messages[0].sender == "Alice"
    assert messages[0].chat_title == "#1 Team project"
    assert teams_client._metadata.get("users", "alice") == {"display_name": "Alice"}


def test_old_message_context_is_one_anchored_read(teams_client, monkeypatch):
    teams_client._update_id_map(lambda value: value["messages"].update({"1": {"conv": "19:group@thread.v2", "msg": "3"}}))
    teams_client._metadata.put_many("chats", {"19:group@thread.v2": {"title": "Group"}})
    reads = []
    def read(path, params=None):
        reads.append(path)
        assert path.endswith("/messages/epochTimeStamp/3")
        assert params["direction"] == "BIDIRECTIONAL"
        return {"messages": [{"id": str(i), "messagetype": "Text", "content": str(i), "composetime": f"2026-10-09T10:00:0{i}Z"} for i in range(1, 7)]}
    monkeypatch.setattr(teams_client, "_ic3_get", read)
    result = teams_client.get_message_context("1", before=1, after=2)
    assert [m.id for m in result] == ["2", "3", "4", "5"]
    assert next(m for m in result if m.id == "3").display_num == 1
    assert all(m.conversation_id == "19:group@thread.v2" for m in result)
    assert len(reads) == 1
    with pytest.raises(ValueError):
        teams_client.get_message_context("1", before=51)


def test_search_query_filters_ids_escapes_names_and_includes_before_day():
    query = TeamsClient._search_query("meeting OR deploy", conv_id='19:room"quoted', from_filter='Alice "Ops"',
                                      after="2026-10-01T03:00:00+03:00", before="2026-10-09")
    assert query.startswith("(meeting OR deploy) AND ")
    assert 'ClientThreadId:"19:room\\"quoted"' in query
    assert 'from:"Alice \\"Ops\\""' in query
    assert 'sent>="2026-10-01T00:00:00Z"' in query
    assert 'sent<"2026-10-10T00:00:00Z"' in query
    guid = "12345678-1234-1234-1234-123456789abc"
    assert "FromSkypeInternalId_String" in TeamsClient._search_query("meeting", from_filter=guid)


def test_search_before_date_includes_entire_day_and_sender_id_filter(teams_client, make_message, monkeypatch):
    message = make_message(timestamp=datetime(2026, 10, 9, 23, 59, tzinfo=timezone.utc), sender="Alice", sender_id="alice-id")
    monkeypatch.setattr(teams_client, "_substrate_search", lambda *a: [message])
    monkeypatch.setattr(teams_client, "_resolve_chat_titles", lambda *a: None)
    result = teams_client.search_messages("meeting", from_filter="alice-id", before="2026-10-09")
    assert result == [message]


def test_message_number_identity_includes_conversation(teams_client, make_message):
    first = make_message(msg_id="same-epoch", conv_id="first")
    second = make_message(msg_id="same-epoch", conv_id="second")
    teams_client._assign_message_nums([first, second])
    assert first.display_num != second.display_num
    assert teams_client._resolve_message_id(str(first.display_num))["conv"] == "first"
    assert teams_client._resolve_message_id(str(second.display_num))["conv"] == "second"


def test_unchanged_fresh_metadata_avoids_disk_write(tmp_path, monkeypatch):
    cache = MetadataCache(tmp_path, "tenant:user")
    entry = {"alice": {"display_name": "Alice"}}
    cache.put_many("users", entry)
    monkeypatch.setattr(cache, "_write", lambda *a: pytest.fail("unchanged fresh cache must avoid I/O"))
    cache.put_many("users", entry)


def test_missing_directory_user_is_cached_briefly_and_replaced_by_observed_name(teams_client, fake_tokens, monkeypatch):
    monkeypatch.setattr(teams_client, "_graph_batch_get", lambda *a, **k: {"removed-user": {"_not_found": True}})
    assert teams_client._resolve_user_name("removed-user") == "removed-user"
    another = TeamsClient(fake_tokens)
    monkeypatch.setattr(another, "_graph_batch_get", lambda *a, **k: pytest.fail("404 should be cached across CLI processes"))
    assert another._resolve_user_name("removed-user") == "removed-user"
    assert another._metadata.get("users", "removed-user")["not_found"]
    another._metadata.put_many("users", {"removed-user": {"display_name": "Restored User"}})
    refreshed = TeamsClient(fake_tokens)
    assert refreshed._resolve_user_name("removed-user") == "Restored User"
    assert "not_found" not in refreshed._metadata.get("users", "removed-user")


def test_directory_failure_cache_distinguishes_404_from_other_statuses(teams_client, monkeypatch):
    monkeypatch.setattr(teams_client, "_request_with_retry", lambda *a, **k: {"responses": [
        {"id": "missing", "status": 404},
        {"id": "denied", "status": 403},
        {"id": "expired", "status": 401},
        {"id": "empty", "status": 200, "body": {}},
    ]})
    teams_client._resolve_user_names(["missing", "denied", "expired", "empty"])
    assert teams_client._metadata.get("users", "missing")["not_found"]
    assert all(teams_client._metadata.get("users", uid) is None for uid in ("denied", "expired", "empty"))


def test_summary_unread_does_not_replace_recent_chat_numbers(teams_client, monkeypatch):
    from teams_cli.commands.summary import _fetch_unread
    teams_client._update_id_map(lambda value: value["chats"].update({"1": "recent-chat"}))
    monkeypatch.setattr(teams_client, "_get_conversations", lambda *a, **k: [{
        "id": "unread-chat", "threadProperties": {"topic": "Unread", "unreadMessageCount": 1},
    }])
    _fetch_unread(teams_client)
    assert teams_client._id_map["chats"] == {"1": "recent-chat"}


def test_graph_presence_403_cached_only_with_ups_and_expires(teams_client, fake_tokens, monkeypatch):
    request = httpx.Request("GET", "https://graph.microsoft.com/v1.0/me/presence")
    def denied(*a, **k):
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError("denied", request=request, response=response)
    monkeypatch.setattr(teams_client, "_graph_get", denied)
    ups_calls = []
    def ups(*a, **k):
        ups_calls.append(1)
        return [{"presence": {"availability": "Available"}}]
    monkeypatch.setattr(teams_client, "_ups_post", ups)
    assert teams_client.get_presence()["availability"] == "Available"
    assert teams_client._metadata.get("capabilities", "graph_presence") == {"denied": True}
    another = TeamsClient(fake_tokens)
    monkeypatch.setattr(another, "_graph_get", lambda *a: pytest.fail("warm denial should avoid failed Graph request"))
    monkeypatch.setattr(another, "_ups_post", ups)
    assert another.get_presence()["availability"] == "Available"
    monkeypatch.setattr("teams_cli.metadata_cache.time.time", lambda: 10**12)
    graph_calls = []
    monkeypatch.setattr(another, "_graph_get", lambda *a: graph_calls.append(1) or {"availability": "Busy"})
    assert another.get_presence()["availability"] == "Busy"
    assert graph_calls == [1] and len(ups_calls) == 2


@pytest.mark.parametrize("status", [401, 429, 500])
def test_other_presence_errors_do_not_persist_permission_denial(teams_client, monkeypatch, status):
    request = httpx.Request("GET", "https://graph.microsoft.com/v1.0/me/presence")
    def fail(*a, **k):
        raise httpx.HTTPStatusError("error", request=request, response=httpx.Response(status, request=request))
    monkeypatch.setattr(teams_client, "_graph_get", fail)
    monkeypatch.setattr(teams_client, "_ups_post", lambda *a: [{"presence": {"availability": "Available"}}])
    if status == 401:
        assert teams_client.get_presence()["availability"] == "Available"
    else:
        with pytest.raises(httpx.HTTPStatusError):
            teams_client.get_presence()
    assert teams_client._metadata.get("capabilities", "graph_presence") is None


def test_presence_denial_without_ups_remains_graph_error(teams_client, monkeypatch):
    teams_client._presence_token = ""
    request = httpx.Request("GET", "https://graph.microsoft.com/v1.0/me/presence")
    def denied(*a, **k):
        raise httpx.HTTPStatusError("denied", request=request, response=httpx.Response(403, request=request))
    monkeypatch.setattr(teams_client, "_graph_get", denied)
    with pytest.raises(httpx.HTTPStatusError):
        teams_client.get_presence()
    assert teams_client._metadata.get("capabilities", "graph_presence") is None


def test_context_expands_past_control_events_but_stops_at_history_boundary(teams_client, monkeypatch):
    teams_client._update_id_map(lambda value: value["messages"].update({"1": {"conv": "group", "msg": "3"}}))
    teams_client._metadata.put_many("chats", {"group": {"title": "Group"}})
    sizes = []
    def read(path, params=None):
        sizes.append(params["pageSize"])
        if len(sizes) == 1:
            return {"messages": [
                {"id": str(i), "messagetype": "Text" if i == 3 else "Event/Call", "content": "event", "composetime": f"2026-10-09T10:00:0{i}Z"}
                for i in range(1, 6)
            ], "_metadata": {"backwardLink": "older", "forwardLink": "newer"}}
        return {"messages": [
            {"id": str(i), "messagetype": "Text", "content": "message", "composetime": f"2026-10-09T10:00:0{i}Z"}
            for i in range(1, 6)
        ], "_metadata": {}}
    monkeypatch.setattr(teams_client, "_ic3_get", read)
    assert [m.id for m in teams_client.get_message_context("1", before=1, after=1)] == ["2", "3", "4"]
    assert sizes == [5, 20]


def test_context_does_not_expand_when_requested_side_is_exhausted(teams_client, monkeypatch):
    teams_client._update_id_map(lambda value: value["messages"].update({"1": {"conv": "group", "msg": "3"}}))
    teams_client._metadata.put_many("chats", {"group": {"title": "Group"}})
    calls = []
    def read(path, params=None):
        calls.append(1)
        return {"messages": [
            {"id": str(i), "messagetype": "Text", "content": "message", "composetime": f"2026-10-09T10:00:0{i}Z"}
            for i in range(1, 4)
        ], "_metadata": {"backwardLink": "older"}}
    monkeypatch.setattr(teams_client, "_ic3_get", read)
    assert [m.id for m in teams_client.get_message_context("1", before=1, after=1)] == ["2", "3"]
    assert len(calls) == 1


def test_context_expansion_has_request_and_page_limit(teams_client, monkeypatch):
    teams_client._update_id_map(lambda value: value["messages"].update({"1": {"conv": "group", "msg": "3"}}))
    teams_client._metadata.put_many("chats", {"group": {"title": "Group"}})
    sizes = []
    def read(path, params=None):
        sizes.append(params["pageSize"])
        return {"messages": [{"id": "3", "messagetype": "Text", "content": "message"}],
                "_metadata": {"backwardLink": "older", "forwardLink": "newer"}}
    monkeypatch.setattr(teams_client, "_ic3_get", read)
    assert len(teams_client.get_message_context("1", before=50, after=50)) == 1
    assert sizes == [103, 200]


def test_chat_and_context_exclude_deleted_text_neighbors(teams_client, monkeypatch):
    teams_client._update_id_map(lambda value: value["messages"].update({"1": {"conv": "group", "msg": "3"}}))
    teams_client._metadata.put_many("chats", {"group": {"title": "Group"}})
    rows = [
        {"id": str(i), "messagetype": "Text", "content": "message", "composetime": f"2026-10-09T10:00:0{i}Z",
         "properties": {"deletetime": "123456789"} if i == 2 else {}}
        for i in range(1, 5)
    ]
    monkeypatch.setattr(teams_client, "_ic3_get", lambda *a, **k: {"messages": rows})
    context = teams_client.get_message_context("1", before=1, after=1)
    assert [m.id for m in context] == ["1", "3", "4"]
    chat = teams_client.get_chat_messages("19:group@thread.v2")
    assert all(m.id != "2" for m in chat)


@pytest.mark.parametrize("marker", [
    {"messagetype": "Text", "properties": {"deletetime": "123456789"}},
    {"messagetype": "RichText/Html", "properties": {"deletetime": 123456789}},
    {"messagetype": "Control/MessageDelete", "properties": {}},
])
def test_context_deleted_anchor_is_rejected_without_detail_fallback(teams_client, monkeypatch, marker):
    teams_client._update_id_map(lambda value: value["messages"].update({"1": {"conv": "group", "msg": "3"}}))
    monkeypatch.setattr(teams_client, "_ic3_get", lambda *a, **k: {"messages": [{"id": "3", "content": "old content", **marker}]})
    monkeypatch.setattr(teams_client, "get_message_detail", lambda *a: pytest.fail("deleted anchor must never be fetched back"))
    with pytest.raises(ResourceNotFoundError, match="deleted"):
        teams_client.get_message_context("1")


@pytest.mark.parametrize("marker", [
    {"messagetype": "Text", "properties": {"deletetime": "123456789"}},
    {"messagetype": "Control/MessageDelete", "properties": {}},
])
def test_detail_rejects_deleted_anchor(teams_client, monkeypatch, marker):
    teams_client._update_id_map(lambda value: value["messages"].update({"1": {"conv": "group", "msg": "3"}}))
    monkeypatch.setattr(teams_client, "_ic3_get", lambda *a, **k: {"id": "3", "content": "old content", **marker})
    with pytest.raises(ResourceNotFoundError, match="deleted"):
        teams_client.get_message_detail("1")


@pytest.mark.parametrize("value", [None, "", 0, "0", False])
def test_zero_deletion_time_does_not_hide_live_message(value):
    assert not TeamsClient._is_deleted_message({"messagetype": "Text", "properties": {"deletetime": value}})
