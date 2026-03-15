from __future__ import annotations

from datetime import datetime, timedelta, timezone

import teams_cli.scheduler as scheduler


def test_load_scheduled_returns_empty_for_invalid_json():
    scheduler.SCHEDULED_FILE.write_text("{not-json")

    assert scheduler.load_scheduled() == []


def test_add_scheduled_persists_entry():
    entry = scheduler.add_scheduled(
        conv_id="conv-1",
        content="Hello",
        send_at="2026-03-11T10:00:00Z",
        chat_title="Chat #1",
    )

    stored = scheduler.load_scheduled()
    assert stored[0]["conv_id"] == "conv-1"
    assert stored[0]["status"] == "pending"
    assert entry["chat_title"] == "Chat #1"


def test_cancel_scheduled_marks_entry_cancelled():
    scheduler.save_scheduled(
        [
            {
                "conv_id": "conv-1",
                "content": "Hello",
                "send_at": "2026-03-11T10:00:00Z",
                "chat_title": "Chat #1",
                "created_at": "2026-03-11T09:00:00Z",
                "status": "pending",
            }
        ]
    )

    cancelled = scheduler.cancel_scheduled(1)

    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
    assert "cancelled_at" in cancelled


def test_get_pending_filters_due_entries():
    now = datetime.now(timezone.utc)
    scheduler.save_scheduled(
        [
            {
                "conv_id": "conv-due",
                "content": "Hello",
                "send_at": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "chat_title": "Chat #1",
                "created_at": "2026-03-11T09:00:00Z",
                "status": "pending",
            },
            {
                "conv_id": "conv-future",
                "content": "Later",
                "send_at": (now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "chat_title": "Chat #2",
                "created_at": "2026-03-11T09:10:00Z",
                "status": "pending",
            },
            {
                "conv_id": "conv-sent",
                "content": "Done",
                "send_at": (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "chat_title": "Chat #3",
                "created_at": "2026-03-11T08:55:00Z",
                "status": "sent",
            },
        ]
    )

    pending = scheduler.get_pending()

    assert [entry["conv_id"] for entry in pending] == ["conv-due"]


def test_mark_sent_updates_entry():
    scheduler.save_scheduled(
        [
            {
                "conv_id": "conv-1",
                "content": "Hello",
                "send_at": "2026-03-11T10:00:00Z",
                "chat_title": "Chat #1",
                "created_at": "2026-03-11T09:00:00Z",
                "status": "pending",
            }
        ]
    )

    scheduler.mark_sent(0)

    stored = scheduler.load_scheduled()
    assert stored[0]["status"] == "sent"
    assert "sent_at" in stored[0]
