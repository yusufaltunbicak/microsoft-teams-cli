from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from teams_cli.auth import get_tokens, verify_tokens
from teams_cli.client import TeamsClient

pytestmark = pytest.mark.live


def _require_live_opt_in() -> None:
    if os.environ.get("TEAMS_LIVE_SMOKE") != "1":
        pytest.skip("Set TEAMS_LIVE_SMOKE=1 to run live smoke tests.")


@contextmanager
def _timeout(seconds: int, label: str):
    def handler(signum, frame):  # pragma: no cover - signal path depends on runtime
        raise TimeoutError(f"{label} timed out after {seconds}s")

    if not hasattr(signal, "SIGALRM"):
        yield
        return

    previous = signal.signal(signal.SIGALRM, handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _call_with_timeout(seconds: int, label: str, fn, *args, **kwargs):
    try:
        with _timeout(seconds, label):
            return fn(*args, **kwargs)
    except TimeoutError as exc:  # pragma: no cover - only in live execution
        pytest.skip(str(exc))


def _find_message_by_token(client: TeamsClient, conv_id: str, token: str):
    for _ in range(5):
        messages = _call_with_timeout(20, "chat read-back", client.get_chat_messages, conv_id, 25)
        for message in reversed(messages):
            if token in message.content or token in message.text_content:
                return message
        time.sleep(2)
    return None


def _find_search_result_by_token(client: TeamsClient, token: str):
    for _ in range(8):
        results = _call_with_timeout(20, "search read-back", client.search_messages, token, 5)
        for result in results:
            if token in result.content or token in result.text_content:
                return result
        time.sleep(2)
    return None


def _live_client() -> TeamsClient:
    client = TeamsClient(get_tokens())
    client._session.jitter = lambda is_write=False: None
    return client


def _run_cli(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        ["teams", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _parse_json_stdout(result: subprocess.CompletedProcess[str]):
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def test_live_read_only_smoke():
    _require_live_opt_in()
    print("live: loading tokens", flush=True)
    tokens = get_tokens()

    print("live: verifying tokens", flush=True)
    assert _call_with_timeout(20, "token verification", verify_tokens, tokens) is True

    client = _live_client()
    print("live: reading profile", flush=True)
    me = _call_with_timeout(20, "whoami", client.get_me)
    print("live: listing chats", flush=True)
    chats = _call_with_timeout(20, "chat listing", client.get_chats, 3)

    assert me["displayName"]
    assert me.get("mail")
    assert isinstance(chats, list)


def test_live_self_notes_send_search_read_react_flow():
    _require_live_opt_in()
    client = _live_client()
    print("live: reading profile for self-target", flush=True)
    me = _call_with_timeout(20, "whoami", client.get_me)
    email = me.get("mail")
    if not email:
        pytest.skip("Authenticated user has no email address available for self-lookup.")

    print("live: resolving self user lookup", flush=True)
    users = _call_with_timeout(20, "self user lookup", client.search_users, email, 5)
    if not any(user.id == client._user_id for user in users):
        pytest.skip("Self lookup did not resolve back to the authenticated user.")

    print("live: resolving self conversation", flush=True)
    conv_id = "48:notes"
    _call_with_timeout(
        20,
        "self notes preflight",
        client._ic3_get,
        f"/users/ME/conversations/{conv_id}/messages",
        params={"view": "msnp24Equivalent|supportsMessageProperties", "pageSize": 5},
    )

    token = uuid.uuid4().hex[:10]
    content = f"[teams-cli live test] self smoke {token}"
    print("live: sending self message", flush=True)
    _call_with_timeout(20, "self message send", client.send_message, conv_id, content)

    print("live: reading back message", flush=True)
    message = _find_message_by_token(client, conv_id, token)
    if message is None:
        pytest.fail("Sent message could not be read back from the self conversation.")

    print("live: searching for sent message", flush=True)
    search_result = _find_search_result_by_token(client, token)
    assert search_result is not None
    assert token in search_result.content or token in search_result.text_content

    print("live: applying reaction", flush=True)
    _call_with_timeout(20, "add reaction", client.add_reaction, str(message.display_num), "like")
    print("live: removing reaction", flush=True)
    _call_with_timeout(20, "remove reaction", client.remove_reaction, str(message.display_num), "like")

    print("live: reading message detail", flush=True)
    detail = _call_with_timeout(20, "message detail read", client.get_message_detail, str(message.display_num))
    assert token in detail.content or token in detail.text_content


def test_live_cli_surface_and_known_limits():
    _require_live_opt_in()

    print("live: running teams --json", flush=True)
    teams_result = _run_cli("teams", "--json")
    assert teams_result.returncode == 0, teams_result.stderr
    teams = _parse_json_stdout(teams_result)
    assert isinstance(teams, list)
    assert teams

    team_num = str(teams[0]["display_num"])

    print("live: running channels --json", flush=True)
    channels_result = _run_cli("channels", team_num, "--json")
    assert channels_result.returncode == 0, channels_result.stderr
    channels = _parse_json_stdout(channels_result)
    assert isinstance(channels, list)

    print("live: running status --json", flush=True)
    status_result = _run_cli("status", "--json")
    assert status_result.returncode == 0, status_result.stderr
    status_payload = _parse_json_stdout(status_result)
    assert status_payload is not None
    assert status_payload.get("availability")

    if channels:
        preferred = next((channel for channel in channels if channel.get("name") == "General"), channels[0])
        chan_num = str(preferred["display_num"])
        print("live: running channel --json", flush=True)
        channel_result = _run_cli("channel", team_num, chan_num, "--json")
        assert channel_result.returncode == 0, channel_result.stderr
        channel_messages = _parse_json_stdout(channel_result)
        assert isinstance(channel_messages, list)


def test_live_cli_send_file_and_attachment_download(tmp_path: Path):
    _require_live_opt_in()

    token = uuid.uuid4().hex[:10]
    upload_path = tmp_path / f"teams-cli-live-file-{token}.txt"
    upload_path.write_text(f"teams-cli live file smoke {token}\n", encoding="utf-8")

    print("live: sending file through CLI", flush=True)
    send_result = _run_cli(
        "send-file",
        "48:notes",
        str(upload_path),
        "--message",
        f"[teams-cli live file] {token}",
        "-y",
        timeout=120,
    )
    assert send_result.returncode == 0, send_result.stderr
    assert "sent to chat #48:notes" in (send_result.stdout + send_result.stderr)

    client = _live_client()
    message = None
    for _ in range(8):
        messages = _call_with_timeout(20, "file send read-back", client.get_chat_messages, "48:notes", 25)
        for candidate in reversed(messages):
            if token in candidate.content or token in candidate.text_content:
                message = candidate
                break
            if any(token in attachment.name for attachment in candidate.attachments):
                message = candidate
                break
        if message is not None:
            break
        time.sleep(2)

    if message is None:
        pytest.fail("Sent file could not be read back from self notes.")

    print("live: listing attachments through CLI", flush=True)
    attachments_result = _run_cli("attachments", str(message.display_num), "--json")
    assert attachments_result.returncode == 0, attachments_result.stderr
    attachments_payload = _parse_json_stdout(attachments_result)
    assert isinstance(attachments_payload, list)
    assert any(token in attachment["name"] for attachment in attachments_payload)

    download_dir = tmp_path / "downloads"
    print("live: downloading attachments through CLI", flush=True)
    download_result = _run_cli(
        "attachments",
        str(message.display_num),
        "--download",
        "--save-to",
        str(download_dir),
        timeout=120,
    )
    assert download_result.returncode == 0, download_result.stderr
    downloaded = list(download_dir.glob(f"*{token}*"))
    assert downloaded
    assert downloaded[0].read_text(encoding="utf-8").strip() == f"teams-cli live file smoke {token}"
