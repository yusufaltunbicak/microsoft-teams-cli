from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import teams_cli.cli as cli_mod
import teams_cli.commands._common as commands_common
import teams_cli.commands.schedule as cmd_schedule
import teams_cli.scheduler as scheduler


def test_parse_schedule_time_relative_offset(monkeypatch: pytest.MonkeyPatch):
    fixed_now = datetime(2026, 3, 11, 10, 0, tzinfo=timezone.utc)

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr(commands_common, "datetime", FakeDateTime)

    result = commands_common._parse_schedule_time("+1h30m")

    assert result == fixed_now + timedelta(hours=1, minutes=30)


def test_schedule_command_adds_entry(runner, mocker):
    send_at = datetime(2026, 3, 12, 9, 0, tzinfo=timezone.utc)

    class FakeClient:
        def _resolve_chat_id(self, chat_num: str) -> str:
            assert chat_num == "5"
            return "conv-5"

    mocker.patch.object(cmd_schedule, "_parse_schedule_time", return_value=send_at)
    mocker.patch.object(cmd_schedule, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["schedule", "5", "Deploy later", "+1h", "-y"])

    assert result.exit_code == 0
    entries = scheduler.load_scheduled()
    assert len(entries) == 1
    assert entries[0]["conv_id"] == "conv-5"
    assert entries[0]["content"] == "Deploy later"
    assert entries[0]["status"] == "pending"


def test_schedule_list_json_returns_pending_only(runner):
    scheduler.save_scheduled(
        [
            {
                "conv_id": "conv-1",
                "content": "Due soon",
                "send_at": "2026-03-11T10:00:00Z",
                "chat_title": "Chat #1",
                "created_at": "2026-03-11T09:00:00Z",
                "status": "pending",
            },
            {
                "conv_id": "conv-2",
                "content": "Cancelled",
                "send_at": "2026-03-11T11:00:00Z",
                "chat_title": "Chat #2",
                "created_at": "2026-03-11T09:10:00Z",
                "status": "cancelled",
            },
        ]
    )

    result = runner.invoke(cli_mod.cli, ["schedule-list", "--json"])

    assert result.exit_code == 0
    envelope = json.loads(result.output)
    assert envelope["ok"] is True
    payload = envelope["data"]
    assert [entry["conv_id"] for entry in payload] == ["conv-1"]


def test_schedule_cancel_marks_pending_entry(runner):
    scheduler.save_scheduled(
        [
            {
                "conv_id": "conv-1",
                "content": "First",
                "send_at": "2026-03-11T10:00:00Z",
                "chat_title": "Chat #1",
                "created_at": "2026-03-11T09:00:00Z",
                "status": "pending",
            }
        ]
    )

    result = runner.invoke(cli_mod.cli, ["schedule-cancel", "1", "-y"])

    assert result.exit_code == 0
    assert scheduler.load_scheduled()[0]["status"] == "cancelled"


def test_schedule_cancel_invalid_index_prints_error(runner, console_capture):
    scheduler.save_scheduled([])

    result = runner.invoke(cli_mod.cli, ["schedule-cancel", "2", "-y"])

    assert result.exit_code == 5
    assert "Invalid index #2" in result.output


def test_schedule_run_sends_due_messages_and_marks_sent(runner, mocker):
    now = datetime.now(timezone.utc)
    scheduler.save_scheduled(
        [
            {
                "conv_id": "conv-due",
                "content": "Due now",
                "send_at": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "chat_title": "Chat #1",
                "created_at": "2026-03-11T09:00:00Z",
                "status": "pending",
            },
            {
                "conv_id": "conv-later",
                "content": "Later",
                "send_at": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "chat_title": "Chat #2",
                "created_at": "2026-03-11T09:10:00Z",
                "status": "pending",
            },
        ]
    )

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def send_message(self, conv_id: str, content: str):
            self.calls.append((conv_id, content))

    fake_client = FakeClient()
    mocker.patch.object(cmd_schedule, "_get_client", return_value=fake_client)

    result = runner.invoke(cli_mod.cli, ["schedule-run"])

    assert result.exit_code == 0
    assert fake_client.calls == [("conv-due", "Due now")]
    entries = scheduler.load_scheduled()
    assert entries[0]["status"] == "sent"
    assert entries[1]["status"] == "pending"
