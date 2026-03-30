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
from teams_cli.client import TeamsClient, TokenExpiredError

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
    print("live: running whoami --json", flush=True)
    whoami_result = _run_cli("whoami", "--json")
    assert whoami_result.returncode == 0, whoami_result.stderr
    whoami_payload = _parse_json_stdout(whoami_result)
    assert whoami_payload is not None

    print("live: listing chats", flush=True)
    chats_result = _run_cli("chats", "--json")
    assert chats_result.returncode == 0, chats_result.stderr
    chats_payload = _parse_json_stdout(chats_result)
    assert chats_payload is not None

    me = whoami_payload["data"]
    chats = chats_payload["data"]
    assert me["displayName"]
    assert isinstance(chats, list)


def test_live_cli_dry_run_and_no_input_are_safe_for_send():
    _require_live_opt_in()
    client = _live_client()
    print("live: reading profile for safe send flags", flush=True)
    me = _call_with_timeout(20, "whoami", client.get_me)
    email = me.get("mail")
    token = uuid.uuid4().hex[:8]
    if email:
        dry_run_args = ["--dry-run", "send", email, f"[teams-cli live dry-run] {token}"]
        no_input_args = ["--no-input", "send", email, f"[teams-cli live no-input] {token}"]
        refusal = "Refusing to send a direct message without confirmation"
    else:
        dry_run_args = ["--dry-run", "chat-send", "48:notes", f"[teams-cli live dry-run] {token}"]
        no_input_args = ["--no-input", "chat-send", "48:notes", f"[teams-cli live no-input] {token}"]
        refusal = "Refusing to send a message to a chat without confirmation"

    print(f"live: running {' '.join(dry_run_args[:3])}", flush=True)
    dry_run = _run_cli(*dry_run_args)
    assert dry_run.returncode == 0, dry_run.stderr
    assert "dry_run" in dry_run.stdout or "Dry run:" in (dry_run.stdout + dry_run.stderr)

    print(f"live: running {' '.join(no_input_args[:3])}", flush=True)
    no_input = _run_cli(*no_input_args)
    assert no_input.returncode == 2, no_input.stderr
    assert refusal in (no_input.stdout + no_input.stderr)


def test_live_auth_status_matches_whoami():
    _require_live_opt_in()

    print("live: running whoami --json", flush=True)
    whoami_result = _run_cli("whoami", "--json")
    assert whoami_result.returncode == 0, whoami_result.stderr
    whoami_payload = _parse_json_stdout(whoami_result)
    assert whoami_payload is not None

    print("live: running auth-status --json", flush=True)
    auth_status_result = _run_cli("auth-status", "--json")
    assert auth_status_result.returncode == 0, auth_status_result.stderr
    auth_status_payload = _parse_json_stdout(auth_status_result)
    assert auth_status_payload is not None

    print("live: running auth-status --check --json", flush=True)
    auth_check_result = _run_cli("auth-status", "--check", "--json")
    assert auth_check_result.returncode == 0, auth_check_result.stderr
    auth_check_payload = _parse_json_stdout(auth_check_result)
    assert auth_check_payload is not None

    whoami_data = whoami_payload["data"]
    auth_status_data = auth_status_payload["data"]
    auth_check_data = auth_check_payload["data"]

    assert auth_status_data["identity"]["region"] == whoami_data["region"]
    assert auth_status_data["identity"]["user_id"] == whoami_data["user_id"]
    assert isinstance(auth_check_data["tokens"]["ic3"], bool)
    assert isinstance(auth_check_data["ic3"]["valid"], bool)


def test_live_cli_exit_code_contract():
    _require_live_opt_in()

    print("live: running success exit-code probe", flush=True)
    success = _run_cli("auth-status", "--json")
    assert success.returncode == 0, success.stderr

    print("live: running usage exit-code probe", flush=True)
    usage = _run_cli("auth-status", "--bogus")
    assert usage.returncode == 2, usage.stderr

    print("live: running refusal exit-code probe", flush=True)
    refusal = _run_cli("--no-input", "chat-send", "48:notes", f"[teams-cli live refusal] {uuid.uuid4().hex[:8]}")
    assert refusal.returncode == 2, refusal.stderr


def test_live_cli_command_allowlist():
    _require_live_opt_in()

    print("live: running allowlisted read-only command", flush=True)
    allowed = _run_cli("--enable-commands", "auth-status,status", "auth-status", "--json")
    assert allowed.returncode == 0, allowed.stderr

    print("live: running denied command probe", flush=True)
    denied = _run_cli("--enable-commands", "status", "auth-status")
    assert denied.returncode == 2, denied.stderr

    print("live: running allowlisted dry-run write probe", flush=True)
    dry_run = _run_cli(
        "--enable-commands",
        "chat-send",
        "--dry-run",
        "chat-send",
        "48:notes",
        f"[teams-cli live allowlist] {uuid.uuid4().hex[:8]}",
    )
    assert dry_run.returncode == 0, dry_run.stderr


def test_live_self_notes_send_search_read_react_flow():
    _require_live_opt_in()
    token = uuid.uuid4().hex[:10]
    content = f"[teams-cli live test] self smoke {token}"
    print("live: sending self message through CLI", flush=True)
    send_result = _run_cli("chat-send", "48:notes", content, "-y")
    assert send_result.returncode == 0, send_result.stderr

    print("live: searching for sent message through CLI", flush=True)
    search_result = None
    for _ in range(8):
        candidate = _run_cli("search", token, "--max", "5", "--json")
        assert candidate.returncode == 0, candidate.stderr
        payload = _parse_json_stdout(candidate)
        assert payload is not None
        matches = payload["data"]
        if matches:
            search_result = matches[0]
            break
        time.sleep(2)

    if search_result is None:
        pytest.fail("Sent self-notes message could not be found via CLI search.")

    msg_num = str(search_result["display_num"])
    assert token in search_result.get("content", "") or token in search_result.get("text_content", "")

    print("live: reading message detail through CLI", flush=True)
    read_result = _run_cli("read", msg_num, "--json")
    assert read_result.returncode == 0, read_result.stderr
    read_payload = _parse_json_stdout(read_result)
    assert read_payload is not None
    detail = read_payload["data"]
    assert token in detail.get("content", "") or token in detail.get("text_content", "")

    print("live: applying reaction through CLI", flush=True)
    react_result = _run_cli("react", "like", msg_num, "-y")
    assert react_result.returncode == 0, react_result.stderr

    print("live: removing reaction through CLI", flush=True)
    unreact_result = _run_cli("unreact", "like", msg_num, "-y")
    assert unreact_result.returncode == 0, unreact_result.stderr


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
