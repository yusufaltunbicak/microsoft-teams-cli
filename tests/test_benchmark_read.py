from __future__ import annotations

import base64
import json
from pathlib import Path
import stat
import subprocess
import time

import pytest

from scripts import benchmark_read as benchmark


SECRET = "private-message-Bearer-secret-cookie"


def token(expires: float) -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"exp": expires}).encode()).decode().rstrip("=")
    return f"header.{claims}.signature"


def test_command_allowlist_contains_only_reads():
    commands = benchmark.build_commands("/fake/teams", "Person", "word")
    assert tuple(commands) == (*benchmark.COMMAND_NAMES, "search-local")
    assert commands["version"] == ["/fake/teams", "--version"]
    for name, argv in commands.items():
        if name != "version":
            assert argv[1] == "--no-input"
            assert argv[2] in {"chats", "chat", "unread", "summary", "user-search", "search"}
            assert argv[-1] == "--json"
    assert all(word not in args for args in commands.values() for word in ("send", "mark-read", "set-status", "schedule-run", "--force"))


@pytest.mark.parametrize("command", ["send", "reply", "react", "mark-read", "schedule-run", "set-status", "group-chat"])
def test_mutating_command_is_rejected_before_execution(command, monkeypatch):
    monkeypatch.setattr(benchmark.subprocess, "run", lambda *a, **k: pytest.fail("subprocess invoked"))
    with pytest.raises(SystemExit) as error:
        benchmark.main(["--label", "test", "--commands", command])
    assert error.value.code == 2


def test_response_contents_are_reduced_to_counts():
    payload = json.dumps({"ok": True, "schema_version": "1.0", "data": [{"content": SECRET, "sender": SECRET}]})
    result = benchmark.classify_output(payload, SECRET, 0)
    assert result == {"ok": True, "result_count": 1, "error": None}
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize(("returncode", "text", "label"), [(4, SECRET, "auth_required"), (7, SECRET, "rate_limited"), (2, SECRET, "usage_error"), (1, SECRET, "invalid_output")])
def test_error_text_is_never_retained(returncode, text, label):
    result = benchmark.classify_output("", text, returncode)
    assert result["error"] == label
    assert SECRET not in json.dumps(result)


def test_failure_envelope_does_not_retain_error_text():
    result = benchmark.classify_output(json.dumps({"ok": False, "error": SECRET}), "", 1)
    assert result["error"] == "command_error"
    assert SECRET not in json.dumps(result)


def test_summary_reduction_accepts_only_integer_counts():
    result = benchmark.classify_output(json.dumps({"ok": True, "data": {"recent": [{"last_message": SECRET}], "unread_count": 5, "unread_messages": SECRET}}), "", 0)
    assert result == {"ok": True, "result_count": 1, "error": None, "unread_count": 5}


def test_subprocess_is_captured_without_interactive_stdin(monkeypatch):
    def fake_run(argv, **kwargs):
        assert argv == ["/fake/teams", "--version"]
        assert kwargs == {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL, "timeout": 8, "check": False}
        return subprocess.CompletedProcess(argv, 0, f"teams, version 0.1.2\n{SECRET}", SECRET)

    monkeypatch.setattr(benchmark.subprocess, "run", fake_run)
    result = benchmark.run_sample(["/fake/teams", "--version"], 8)
    assert result["version"] == "0.1.2"
    assert result["ok"] is True
    assert SECRET not in json.dumps(result)


def test_timeout_discards_partial_secret_output(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(["/fake/teams"], 1, output=SECRET, stderr=SECRET)

    monkeypatch.setattr(benchmark.subprocess, "run", timeout)
    result = benchmark.run_sample(["/fake/teams", "chats"], 1)
    assert result["error"] == "timeout"
    assert SECRET not in json.dumps(result)


def test_expired_secondary_tokens_stop_legacy_preflight(tmp_path, monkeypatch):
    monkeypatch.delenv("TEAMS_IC3_TOKEN", raising=False)
    (tmp_path / "tokens.json").write_text(json.dumps({"ic3": token(time.time() + 3600), "substrate": token(time.time() - 1)}))
    assert benchmark.preflight_tokens(tmp_path) is False


def test_valid_tokens_allow_preflight(tmp_path, monkeypatch):
    monkeypatch.delenv("TEAMS_IC3_TOKEN", raising=False)
    (tmp_path / "tokens.json").write_text(json.dumps({"ic3": token(time.time() + 3600), "substrate": token(time.time() + 3600)}))
    assert benchmark.preflight_tokens(tmp_path) is True


def test_env_ic3_override_supported_without_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAMS_IC3_TOKEN", token(time.time() + 3600))
    assert benchmark.preflight_tokens(tmp_path) is True


@pytest.mark.parametrize("contents", [SECRET, "[]", "{}", '{"ic3": "not-a-jwt"}'])
def test_broken_cache_preflight_returns_no_details(tmp_path, contents, monkeypatch):
    monkeypatch.delenv("TEAMS_IC3_TOKEN", raising=False)
    (tmp_path / "tokens.json").write_text(contents)
    assert benchmark.preflight_tokens(tmp_path) is False


def test_cold_metadata_preserves_auth_and_restores_original_on_failure(tmp_path):
    auth_names = ("tokens.json", "browser-state.json", "id_map.json", "scheduled.json", "user_profile.json")
    for name in (*auth_names, "metadata.json"):
        (tmp_path / name).write_text(f"original-{name}")
    with pytest.raises(RuntimeError):
        with benchmark.cold_metadata(tmp_path, True) as clear:
            clear()
            assert not (tmp_path / "metadata.json").exists()
            (tmp_path / "metadata.json").write_text(SECRET)
            clear()
            assert not (tmp_path / "metadata.json").exists()
            raise RuntimeError("simulated interruption")
    for name in (*auth_names, "metadata.json"):
        assert (tmp_path / name).read_text() == f"original-{name}"
    assert stat.S_IMODE((tmp_path / "metadata.json").stat().st_mode) == 0o600
    assert not any(p.name.startswith("benchmark-metadata-") for p in tmp_path.iterdir())


def test_cold_cache_without_original_leaves_no_metadata(tmp_path):
    with benchmark.cold_metadata(tmp_path, True) as clear:
        clear()
        (tmp_path / "metadata.json").write_text(SECRET)
    assert not (tmp_path / "metadata.json").exists()


def test_cold_metadata_refuses_symlink(tmp_path):
    original = tmp_path / "tokens.json"
    original.write_text(SECRET)
    (tmp_path / "metadata.json").symlink_to(original)
    with pytest.raises(ValueError, match="unsafe_metadata_path"):
        with benchmark.cold_metadata(tmp_path, True):
            pytest.fail("unsafe path accepted")
    assert original.read_text() == SECRET


def test_report_is_private_and_refuses_symlink(tmp_path):
    report = tmp_path / "report.json"
    report.write_text("old")
    report.chmod(0o644)
    benchmark.secure_json_write(report, {"ok": True})
    assert json.loads(report.read_text()) == {"ok": True}
    assert stat.S_IMODE(report.stat().st_mode) == 0o600
    linked = tmp_path / "linked.json"
    linked.symlink_to(report)
    with pytest.raises(OSError):
        benchmark.secure_json_write(linked, {"ok": False})
    assert json.loads(report.read_text()) == {"ok": True}


def test_failed_preflight_runs_no_subprocess_and_prints_no_contents(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TEAMS_CLI_CACHE", str(tmp_path))
    monkeypatch.delenv("TEAMS_IC3_TOKEN", raising=False)
    (tmp_path / "tokens.json").write_text(SECRET)
    monkeypatch.setattr(benchmark.subprocess, "run", lambda *a, **k: pytest.fail("subprocess invoked"))
    result = benchmark.main(["--teams", "/fake/teams", "--label", "baseline", "--runs", "2"])
    assert result == 3
    captured = capsys.readouterr()
    assert "auth preflight failed" in captured.err
    assert SECRET not in captured.out + captured.err


def test_full_mocked_benchmark_never_retains_message_or_query(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TEAMS_CLI_CACHE", str(tmp_path))
    (tmp_path / "metadata.json").write_text("original")

    def fake_run(argv, **kwargs):
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "teams, version 0.1.2", "")
        assert argv[1] == "--no-input"
        assert not (tmp_path / "metadata.json").exists()
        (tmp_path / "metadata.json").write_text(SECRET)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "data": [{"content": SECRET}]}), SECRET)

    monkeypatch.setattr(benchmark.subprocess, "run", fake_run)
    result = benchmark.main(["--teams", "/fake/teams", "--label", "test", "--skip-preflight", "--cache-mode", "cold", "--runs", "3", "--user-query", SECRET, "--search-query", SECRET])
    assert result == 0
    report_path = tmp_path / "benchmark-test-cold.json"
    report = json.loads(report_path.read_text())
    assert len(report["samples"]) == 3 * len(benchmark.COMMAND_NAMES)
    assert all(summary["successful_runs"] == 3 for summary in report["summary"].values())
    assert SECRET not in report_path.read_text() + capsys.readouterr().out
    assert (tmp_path / "metadata.json").read_text() == "original"
