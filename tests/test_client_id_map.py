from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

import pytest

from teams_cli.client import TeamsClient


def _client(fake_tokens: dict[str, str]) -> TeamsClient:
    client = TeamsClient(fake_tokens)
    client._session.jitter = lambda is_write=False: None
    return client


def test_assign_chat_nums_rebuilds_display_order(teams_client, make_chat):
    teams_client._id_map["chats"] = {"1": "old-chat", "55": "chat-b"}
    chats = [
        make_chat("chat-a", "Alpha", datetime(2026, 3, 11, 10, 0, tzinfo=timezone.utc)),
        make_chat("chat-b", "Beta", datetime(2026, 3, 11, 9, 0, tzinfo=timezone.utc)),
    ]

    teams_client._assign_chat_nums(chats)

    assert [chat.display_num for chat in chats] == [1, 2]
    assert teams_client._id_map["chats"] == {"1": "chat-a", "2": "chat-b"}


def test_assign_message_nums_merges_stale_clients_without_overwriting(fake_tokens, make_message):
    client_one = _client(fake_tokens)
    client_two = _client(fake_tokens)

    first = make_message("m1", "conv-1")
    second = make_message("m2", "conv-2")

    client_one._assign_message_nums([first])
    client_two._assign_message_nums([second])

    assert first.display_num == 1
    assert second.display_num == 2

    data = client_two._load_id_map()
    assert data["messages"]["1"]["msg"] == "m1"
    assert data["messages"]["2"]["msg"] == "m2"


def test_assign_message_nums_preserves_chat_map_from_other_process(fake_tokens, make_chat, make_message):
    chat_writer = _client(fake_tokens)
    message_writer = _client(fake_tokens)

    chat = make_chat("conv-chat", "Alpha")
    chat_writer._assign_chat_nums([chat])
    message_writer._assign_message_nums([make_message("m1", "conv-chat")])

    data = message_writer._load_id_map()
    assert data["chats"] == {"1": "conv-chat"}
    assert data["messages"]["1"]["conv"] == "conv-chat"


def test_assign_message_nums_handles_concurrent_writers(fake_tokens, make_message):
    client_one = _client(fake_tokens)
    client_two = _client(fake_tokens)
    first = make_message("m1", "conv-1")
    second = make_message("m2", "conv-2")
    barrier = Barrier(2)

    def assign(client: TeamsClient, message):
        barrier.wait()
        client._assign_message_nums([message])
        return message.display_num

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(assign, client_one, first)
        second_future = executor.submit(assign, client_two, second)
        first_num = first_future.result()
        second_num = second_future.result()

    data = client_one._load_id_map()
    stored = {value["msg"] for value in data["messages"].values()}
    assert stored == {"m1", "m2"}
    assert {first_num, second_num} == {1, 2}


def test_refresh_id_map_when_resolving_chat_from_disk(fake_tokens, make_chat):
    writer = _client(fake_tokens)
    reader = _client(fake_tokens)
    chat = make_chat("conv-chat", "Alpha")

    writer._assign_chat_nums([chat])
    reader._id_map["chats"] = {}

    assert reader._resolve_chat_id("1") == "conv-chat"


def test_resolve_chat_id_accepts_direct_conversation_id(teams_client):
    conv_id = "19:user-1_user-2@unq.gbl.spaces"

    assert teams_client._resolve_chat_id(conv_id) == conv_id


def test_resolve_chat_id_accepts_special_notes_conversation(teams_client):
    assert teams_client._resolve_chat_id("48:notes") == "48:notes"


def test_assign_message_nums_evicts_old_entries(monkeypatch: pytest.MonkeyPatch, fake_tokens, make_message):
    monkeypatch.setattr(TeamsClient, "MAX_ID_MAP_SIZE", 2)
    client = _client(fake_tokens)

    client._assign_message_nums(
        [
            make_message("m1", "conv-1"),
            make_message("m2", "conv-2"),
            make_message("m3", "conv-3"),
        ]
    )

    data = client._load_id_map()
    assert set(data["messages"]) == {"2", "3"}
    assert data["messages"]["2"]["msg"] == "m2"
    assert data["messages"]["3"]["msg"] == "m3"
