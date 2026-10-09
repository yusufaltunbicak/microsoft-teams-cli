from datetime import datetime, timedelta, timezone
import json
import stat
import httpx

import pytest

from teams_cli.history import HistoryIndex, account_key, normalize, saved_account, sync_history
from teams_cli import constants
from teams_cli.cli import cli
import teams_cli.commands.cache as cache_commands
import teams_cli.commands.search as search_commands


def test_index_unicode_sender_dates_and_context(tmp_path, make_message):
    now = datetime(2026, 9, 15, 18, tzinfo=timezone.utc)
    msgs = [make_message(msg_id=str(i), conv_id="19:alpha@thread.v2", sender="Şule IŞIK",
                         text_content="Ödeme toplantısı" if i == 2 else "before or after", timestamp=now + timedelta(minutes=i))
            for i in range(5)]
    with HistoryIndex("a", tmp_path / "history.sqlite3") as index:
        index.upsert(msgs, "Çalışma Grubu")
        index.record_coverage(msgs[0].conversation_id, "Çalışma Grubu", now, True)
        hits = index.search("odeme toplantisi", sender="sule isik", chat="calisma",
                            after="2026-09-15", before="2026-09-15")
        assert [m.id for m in hits] == ["2"]
        assert [m.id for m in index.context(hits[0], 1)] == ["1", "2", "3"]
        assert index.search("toplantı", before="2026-09-14") == []
        assert index.status()["complete"]
    assert stat.S_IMODE((tmp_path / "history.sqlite3").stat().st_mode) == 0o600


def test_index_isolates_accounts_updates_and_punctuation(tmp_path, make_message):
    path = tmp_path / "history.sqlite3"
    msg = make_message(msg_id="m", text_content="hello world")
    with HistoryIndex("a", path) as index:
        index.upsert([msg])
        assert len(index.search('hello OR "world"')) == 0  # terms are literal, no FTS injection
        with pytest.raises(ValueError):
            index.search("---")
        msg.text_content = "updated text"
        index.upsert([msg])
        assert index.search("hello") == []
        assert len(index.search("updated")) == 1
    with HistoryIndex("b", path) as index:
        assert index.search("updated") == []
        assert index.status()["messages"] == 0


def test_account_key_tenant_boundary_and_offline_expiry(fake_tokens, token_factory, isolated_paths):
    first = account_key(fake_tokens)
    alternate = dict(fake_tokens, ic3=token_factory(tid="different-tenant"))
    assert account_key(alternate) != first
    expired = dict(fake_tokens, ic3=token_factory(exp=1))
    isolated_paths["tokens_file"].write_text(json.dumps(expired))
    assert saved_account() == first
    # Token claims are authoritative; stale bundle metadata cannot select another account.
    assert account_key(dict(fake_tokens, user_id="someone-else")) == first


def test_ambiguous_names_are_not_guessed(tmp_path):
    now = datetime.now(timezone.utc)
    with HistoryIndex("a", tmp_path / "history.sqlite3") as index:
        index.record_coverage("a", "Finance One", now, True)
        index.record_coverage("b", "Finance Two", now, False)
        with pytest.raises(ValueError, match="Ambiguous"):
            index.resolve_chat("Finance")
        assert index.resolve_chat("Finance One") == "a"
        assert not index.status()["complete"]


def raw_message(mid, when, text_content="toplantı"):
    return {"id": mid, "conversationid": "19:c@thread.v2", "messagetype": "Text",
            "content": text_content, "composetime": when.isoformat(), "from": "8:orgid:u", "imdisplayname": "Alice"}


@pytest.mark.parametrize("encoded", [False, True])
def test_sync_paginates_and_reports_limits(tmp_path, make_chat, encoded):
    now = datetime.now(timezone.utc)
    chat = make_chat(chat_id="19:c@thread.v2", title="Finance")
    link = "https://teams.cloud.microsoft/api/chatsvc/emea/v1/users/ME/conversations/19:c@thread.v2/messages?syncState=opaque"
    if encoded:
        link = link.replace("19:c@thread.v2", "19%3Ac%40thread.v2")
    class Client:
        _user_id = "me"
        _chatsvc = "https://teams.cloud.microsoft/api/chatsvc/emea/v1"
        calls = []
        def get_chats(self, top):
            return [chat]
        def _ic3_get(self, path, params):
            self.calls.append((path, params))
            if len(self.calls) == 1:
                return {"messages": [raw_message("2", now)], "_metadata": {"backwardLink": link}}
            return {"messages": [raw_message("1", now-timedelta(days=90))], "_metadata": {}}
    client = Client()
    with HistoryIndex("a", tmp_path / "history.sqlite3") as index:
        result = sync_history(client, index, chats=1, days=60, max_pages=2)
        assert result["pages"] == 2
        assert result["messages"] == 1
        assert result["complete"]
        assert client.calls[1][1] == {"syncState": "opaque"}
    client.calls = []
    with HistoryIndex("b", tmp_path / "history.sqlite3") as index:
        result = sync_history(client, index, chats=1, days=60, max_pages=1)
        assert not result["complete"]


@pytest.mark.parametrize("url", ["https://evil.example/messages?token=oops",
                                  "https://teams.cloud.microsoft/api/chatsvc/emea/v1/threads/write",
                                  "http://teams.cloud.microsoft/api/chatsvc/emea/v1/users/ME/conversations/c/messages"])
def test_sync_refuses_untrusted_pagination(url, tmp_path, make_chat):
    class Client:
        _user_id = "me"
        _chatsvc = "https://teams.cloud.microsoft/api/chatsvc/emea/v1"
        def get_chats(self, top): return [make_chat(chat_id="c")]
        def _ic3_get(self, path, params):
            return {"messages": [raw_message("1", datetime.now(timezone.utc))],
                    "_metadata": {"backwardLink": url}}
    with HistoryIndex("a", tmp_path / "history.sqlite3") as index:
        with pytest.raises(ValueError, match="pagination"):
            sync_history(Client(), index)


def test_sync_removes_tombstones(tmp_path, make_chat, make_message):
    class Client:
        _user_id = "me"
        _chatsvc = "https://teams.cloud.microsoft/api/chatsvc/emea/v1"
        def get_chats(self, top): return [make_chat(chat_id="19:c@thread.v2")]
        def _ic3_get(self, path, params):
            return {"messages": [{"id": "1", "properties": {"deletetime": "1"}}]}
    with HistoryIndex("a", tmp_path / "history.sqlite3") as index:
        index.upsert([make_message(msg_id="1", conv_id="19:c@thread.v2", text_content="hello")])
        sync_history(Client(), index)
        assert not index.search("hello")


def test_sync_missing_messages_is_incomplete(tmp_path, make_chat):
    class Client:
        _user_id = "me"
        _chatsvc = "https://teams.cloud.microsoft/api/chatsvc/emea/v1"
        def get_chats(self, top): return [make_chat()]
        def _ic3_get(self, path, params): return {}
    with HistoryIndex("a", tmp_path / "history.sqlite3") as index:
        result = sync_history(Client(), index)
        assert not result["complete"]
        assert result["coverage"][0]["reason"] == "unavailable"


def test_sync_skips_only_inaccessible_chats(tmp_path, make_chat):
    class Client:
        _user_id = "me"
        _chatsvc = "https://teams.cloud.microsoft/api/chatsvc/emea/v1"
        calls = 0
        def get_chats(self, top): return [make_chat(chat_id="denied"), make_chat(chat_id="accessible")]
        def _ic3_get(self, path, params):
            self.calls += 1
            if self.calls == 1:
                response = httpx.Response(403, request=httpx.Request("GET", "https://example.com"))
                response.raise_for_status()
            return {"messages": []}
    with HistoryIndex("a", tmp_path / "history.sqlite3") as index:
        result = sync_history(Client(), index)
        assert result["chats"] == 2
        assert not result["complete"]
        assert {row["reason"] for row in result["coverage"]} == {"access_denied", None}
        assert sum(bool(row["complete"]) for row in result["coverage"]) == 1


def test_sync_auth_failure_is_not_hidden(tmp_path, make_chat):
    from teams_cli.exceptions import AuthRequiredError
    class Client:
        _user_id = "me"
        def get_chats(self, top): return [make_chat()]
        def _ic3_get(self, path, params): raise AuthRequiredError("Run: teams login")
    with HistoryIndex("a", tmp_path / "history.sqlite3") as index:
        with pytest.raises(AuthRequiredError):
            sync_history(Client(), index)
        assert not index.status()["chats"]


def test_local_search_no_auth_or_network(runner, mocker, isolated_paths, fake_tokens, make_message):
    isolated_paths["tokens_file"].write_text(json.dumps(fake_tokens))
    with HistoryIndex(account_key(fake_tokens)) as index:
        index.upsert([make_message(text_content="toplantı")])
    live = mocker.patch.object(search_commands, "_get_client", side_effect=AssertionError("network"))
    result = runner.invoke(cli, ["--no-input", "search", "toplantı", "--local", "--context", "1", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["meta"]["source"] == "local"
    assert payload["meta"]["coverage"]["messages"] == 1
    assert payload["data"][0]["context"][0]["display_num"]
    live.assert_not_called()


def test_cache_clear_safety_preserves_auth(runner, isolated_paths):
    for name in ("history.sqlite3", "metadata.json", "tokens.json", "scheduled.json"):
        (constants.CACHE_DIR / name).write_text("private")
    dry = runner.invoke(cli, ["--dry-run", "cache", "clear", "--json"])
    assert dry.exit_code == 0
    assert (constants.CACHE_DIR / "history.sqlite3").exists()
    refused = runner.invoke(cli, ["--no-input", "cache", "clear"])
    assert refused.exit_code == 2
    cleared = runner.invoke(cli, ["--force", "cache", "clear", "--messages-only", "--json"])
    assert cleared.exit_code == 0
    assert not (constants.CACHE_DIR / "history.sqlite3").exists()
    assert (constants.CACHE_DIR / "metadata.json").exists()
    assert (constants.CACHE_DIR / "tokens.json").exists()
    assert (constants.CACHE_DIR / "scheduled.json").exists()
    (constants.CACHE_DIR / "history.sqlite3").write_text("preserve")
    metadata = runner.invoke(cli, ["--force", "cache", "clear", "--metadata-only", "--json"])
    assert metadata.exit_code == 0
    assert not (constants.CACHE_DIR / "metadata.json").exists()
    assert (constants.CACHE_DIR / "history.sqlite3").exists()
    conflicting = runner.invoke(cli, ["--force", "cache", "clear", "--metadata-only", "--messages-only"])
    assert conflicting.exit_code == 2


def test_watch_minimum_validated_before_network(runner, mocker):
    client = mocker.patch.object(cache_commands, "_get_client")
    result = runner.invoke(cli, ["sync", "--watch", "1"])
    assert result.exit_code == 2
    client.assert_not_called()


@pytest.mark.parametrize("arguments", [["--after", "nonsense"], ["--after", "2026-10-01", "--before", "2026-09-01"]])
def test_bad_search_dates_fail_before_network(arguments, runner, mocker):
    client = mocker.patch.object(search_commands, "_get_client")
    result = runner.invoke(cli, ["search", "test", *arguments])
    assert result.exit_code == 2
    client.assert_not_called()


def test_live_search_context_is_additive(runner, mocker, make_message):
    msg = make_message(msg_id="anchor")
    msg.display_num = 1
    client = mocker.Mock()
    client.search_messages.return_value = [msg]
    client.get_message_context.return_value = [msg]
    mocker.patch.object(search_commands, "_get_client", return_value=client)
    result = runner.invoke(cli, ["search", "test", "--context", "2", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "1.0"
    assert payload["data"][0]["id"] == "anchor"
    assert payload["data"][0]["context"][0]["id"] == "anchor"
    client.get_message_context.assert_called_once_with("1", before=2, after=2)


def test_read_context_preserves_message_shape(runner, mocker, make_message):
    import teams_cli.commands.chat as chat_commands
    msg = make_message(msg_id="old")
    client = mocker.Mock()
    client.get_message_detail.return_value = msg
    client.get_message_context.return_value = [msg]
    mocker.patch.object(chat_commands, "_get_client", return_value=client)
    result = runner.invoke(cli, ["read", "1", "--context", "1", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)["data"]
    assert data["id"] == "old" and data["context"][0]["id"] == "old"


def test_cache_status_without_login(runner, mocker):
    mocker.patch.object(cache_commands, "_get_client", side_effect=AssertionError("live"))
    result = runner.invoke(cli, ["cache", "status", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)["data"]
    assert data["messages"] == 0 and not data["live"]


def test_sync_command_only_invokes_read_sync(runner, mocker, fake_tokens):
    client = mocker.Mock()
    client._tokens = fake_tokens
    mocker.patch.object(cache_commands, "_get_client", return_value=client)
    sync = mocker.patch.object(cache_commands, "sync_history", return_value={"indexed": 2, "chats": 1, "path": "private"})
    result = runner.invoke(cli, ["--no-input", "sync", "--chats", "1", "--days", "30", "--max-pages", "2", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["data"]["indexed"] == 2
    assert sync.call_args.kwargs == {"chats": 1, "days": 30, "max_pages": 2}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1])
def test_invalid_jitter_is_rejected(value):
    from teams_cli.anti_detection import BrowserSession
    with pytest.raises(ValueError, match="finite"):
        BrowserSession(read_jitter_base=value)


def test_local_search_pipe_retains_envelope(runner, mocker, isolated_paths, fake_tokens, make_message):
    import teams_cli.commands._common as common
    isolated_paths["tokens_file"].write_text(json.dumps(fake_tokens))
    with HistoryIndex(account_key(fake_tokens)) as index:
        index.upsert([make_message(text_content="meeting")])
    mocker.patch.object(common, "is_piped", return_value=True)
    result = runner.invoke(cli, ["search", "meeting", "--local"])
    assert result.exit_code == 0
    assert json.loads(result.output)["ok"]


def test_missing_identity_fails_closed():
    from teams_cli.exceptions import AuthRequiredError
    for tokens in ({}, [], {"ic3": "invalid", "user_id": "fallback"}):
        with pytest.raises(AuthRequiredError):
            account_key(tokens)


def test_jitter_config_does_not_mutate_defaults(tmp_path):
    from teams_cli.config import load_config, DEFAULTS
    p = tmp_path / "config.yaml"
    p.write_text("jitter:\n  read_base: 0.03\n")
    assert load_config(p)["jitter"]["read_base"] == 0.03
    assert DEFAULTS["jitter"]["read_base"] == 0.3


def test_jitter_monotonic_and_write_delay(mocker):
    from teams_cli.anti_detection import BrowserSession
    session = BrowserSession(read_jitter_base=0, write_jitter_base=2)
    mocker.patch("teams_cli.anti_detection.random.uniform", return_value=1)
    clock = mocker.patch("teams_cli.anti_detection.time.monotonic", return_value=100)
    sleep = mocker.patch("teams_cli.anti_detection.time.sleep")
    try:
        session.jitter()
        sleep.assert_not_called()
        session.jitter(is_write=True)
        sleep.assert_called_once_with(2)
    finally:
        session.close()
