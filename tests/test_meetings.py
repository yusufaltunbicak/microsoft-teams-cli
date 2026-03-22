from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

import teams_cli.cli as cli_mod
import teams_cli.commands.meetings as cmd_meetings
from teams_cli.client import TeamsClient
from teams_cli.models import Meeting, Recording, Transcript, _parse_dt


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


SAMPLE_GRAPH_EVENT = {
    "id": "event-abc",
    "subject": "Weekly standup",
    "start": {"dateTime": "2026-03-23T09:00:00.0000000", "timeZone": "UTC"},
    "end": {"dateTime": "2026-03-23T09:30:00.0000000", "timeZone": "UTC"},
    "organizer": {
        "emailAddress": {"name": "Alice", "address": "alice@example.com"},
    },
    "attendees": [
        {"emailAddress": {"name": "Bob", "address": "bob@example.com"}},
        {"emailAddress": {"name": "Carol", "address": "carol@example.com"}},
    ],
    "isOnlineMeeting": True,
    "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/abc"},
    "location": {"displayName": "Room 42"},
}


def test_meeting_from_graph_event():
    m = Meeting.from_graph_event(SAMPLE_GRAPH_EVENT)
    assert m.id == "event-abc"
    assert m.subject == "Weekly standup"
    assert m.organizer == "Alice"
    assert m.organizer_email == "alice@example.com"
    assert m.attendees == ["Bob", "Carol"]
    assert m.join_url == "https://teams.microsoft.com/l/meetup-join/abc"
    assert m.location == "Room 42"
    assert m.is_online is True
    assert m.start_time.year == 2026
    assert m.start_time.month == 3
    assert m.start_time.day == 23


def test_meeting_from_graph_event_no_subject():
    event = {
        "id": "e1",
        "start": {"dateTime": ""},
        "end": {"dateTime": ""},
        "organizer": {"emailAddress": {}},
        "isOnlineMeeting": True,
        "onlineMeeting": None,
    }
    m = Meeting.from_graph_event(event)
    assert m.subject == "(No subject)"
    assert m.join_url == ""
    assert m.attendees == []


def test_recording_from_api():
    data = {
        "id": "rec-1",
        "createdDateTime": "2026-03-23T10:00:00Z",
        "recordingContentUrl": "https://graph.microsoft.com/rec/content",
    }
    r = Recording.from_api(data)
    assert r.id == "rec-1"
    assert r.created_time.year == 2026
    assert r.recording_content_url == "https://graph.microsoft.com/rec/content"


def test_transcript_from_api():
    data = {
        "id": "tr-1",
        "createdDateTime": "2026-03-23T10:30:00Z",
        "transcriptContentUrl": "https://graph.microsoft.com/tr/content",
    }
    t = Transcript.from_api(data)
    assert t.id == "tr-1"
    assert t.created_time.year == 2026


# ---------------------------------------------------------------------------
# Client method tests
# ---------------------------------------------------------------------------


def test_get_meetings_filters_online_only(teams_client: TeamsClient, mocker):
    events = {
        "value": [
            {**SAMPLE_GRAPH_EVENT, "id": "e1", "isOnlineMeeting": True},
            {"id": "e2", "subject": "Lunch", "isOnlineMeeting": False,
             "start": {"dateTime": ""}, "end": {"dateTime": ""},
             "organizer": {"emailAddress": {}}},
            {**SAMPLE_GRAPH_EVENT, "id": "e3", "isOnlineMeeting": True},
        ]
    }
    mocker.patch.object(teams_client, "_graph_get", return_value=events)

    result = teams_client.get_meetings("2026-03-23T00:00:00Z", "2026-03-24T00:00:00Z")

    assert len(result) == 2
    assert result[0].id == "e1"
    assert result[1].id == "e3"
    assert result[0].display_num == 1
    assert result[1].display_num == 2


def test_resolve_online_meeting_id_success(teams_client: TeamsClient, mocker):
    mocker.patch.object(
        teams_client, "_graph_get",
        return_value={"value": [{"id": "om-123"}]},
    )
    result = teams_client._resolve_online_meeting_id("https://teams.microsoft.com/l/meetup-join/abc")
    assert result == "om-123"


def test_resolve_online_meeting_id_not_found(teams_client: TeamsClient, mocker):
    mocker.patch.object(teams_client, "_graph_get", return_value={"value": []})
    with pytest.raises(ValueError, match="No online meeting found"):
        teams_client._resolve_online_meeting_id("https://teams.microsoft.com/l/meetup-join/abc")


def test_get_recordings_resolves_and_fetches(teams_client: TeamsClient, mocker):
    teams_client._id_map["meetings"] = {
        "1": {"event_id": "e1", "join_url": "https://teams.microsoft.com/join/abc"},
    }

    call_log = []

    def fake_graph_get(path, params=None, beta=False):
        call_log.append(path)
        if "onlineMeetings" in path and "recordings" in path:
            return {"value": [{"id": "rec-1", "createdDateTime": "2026-03-23T10:00:00Z"}]}
        if "onlineMeetings" in path:
            return {"value": [{"id": "om-1"}]}
        return {}

    mocker.patch.object(teams_client, "_graph_get", side_effect=fake_graph_get)

    recordings = teams_client.get_recordings("1")
    assert len(recordings) == 1
    assert recordings[0].id == "rec-1"


def test_get_transcripts_resolves_and_fetches(teams_client: TeamsClient, mocker):
    teams_client._id_map["meetings"] = {
        "1": {"event_id": "e1", "join_url": "https://teams.microsoft.com/join/abc"},
    }

    def fake_graph_get(path, params=None, beta=False):
        if "transcripts" in path:
            return {"value": [{"id": "tr-1", "createdDateTime": "2026-03-23T10:00:00Z"}]}
        if "onlineMeetings" in path:
            return {"value": [{"id": "om-1"}]}
        return {}

    mocker.patch.object(teams_client, "_graph_get", side_effect=fake_graph_get)

    transcripts = teams_client.get_transcripts("1")
    assert len(transcripts) == 1
    assert transcripts[0].id == "tr-1"


def test_get_transcript_content_returns_text(teams_client: TeamsClient, mocker):
    teams_client._id_map["meetings"] = {
        "1": {"event_id": "e1", "join_url": "https://teams.microsoft.com/join/abc"},
    }

    mocker.patch.object(
        teams_client, "_graph_get",
        return_value={"value": [{"id": "om-1"}]},
    )

    fake_resp = httpx.Response(
        200,
        text="WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nHello world",
        request=httpx.Request("GET", "https://example.com"),
    )
    mocker.patch.object(teams_client, "_graph_get_content", return_value=fake_resp)

    content = teams_client.get_transcript_content("1", "tr-1")
    assert "WEBVTT" in content
    assert "Hello world" in content


# ---------------------------------------------------------------------------
# ID map tests
# ---------------------------------------------------------------------------


def test_assign_meeting_nums(teams_client: TeamsClient):
    m1 = Meeting(
        id="e1", subject="A", start_time=datetime(2026, 3, 23, tzinfo=timezone.utc),
        end_time=datetime(2026, 3, 23, 1, tzinfo=timezone.utc),
        organizer="Alice", organizer_email="alice@example.com",
        join_url="https://join/1",
    )
    m2 = Meeting(
        id="e2", subject="B", start_time=datetime(2026, 3, 24, tzinfo=timezone.utc),
        end_time=datetime(2026, 3, 24, 1, tzinfo=timezone.utc),
        organizer="Bob", organizer_email="bob@example.com",
        join_url="https://join/2",
    )

    teams_client._assign_meeting_nums([m1, m2])

    assert m1.display_num == 1
    assert m2.display_num == 2
    assert teams_client._id_map["meetings"]["1"]["event_id"] == "e1"
    assert teams_client._id_map["meetings"]["2"]["join_url"] == "https://join/2"


def test_resolve_meeting_id(teams_client: TeamsClient):
    teams_client._id_map["meetings"] = {
        "1": {"event_id": "e1", "join_url": "https://join/1"},
    }
    info = teams_client._resolve_meeting_id("1")
    assert info["event_id"] == "e1"
    assert info["join_url"] == "https://join/1"


def test_resolve_meeting_id_unknown(teams_client: TeamsClient):
    with pytest.raises(ValueError, match="Unknown meeting #99"):
        teams_client._resolve_meeting_id("99")


def test_normalize_id_map_includes_meetings():
    result = TeamsClient._normalize_id_map(None)
    assert "meetings" in result
    assert result["meetings"] == {}


# ---------------------------------------------------------------------------
# CLI command tests
# ---------------------------------------------------------------------------


def test_meetings_command_json(runner, mocker):
    meeting = Meeting(
        id="e1", subject="Standup", display_num=1,
        start_time=datetime(2026, 3, 23, 9, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 3, 23, 9, 30, tzinfo=timezone.utc),
        organizer="Alice", organizer_email="alice@example.com",
        attendees=["Bob"], join_url="https://join/1",
    )

    class FakeClient:
        def get_meetings(self, start_dt, end_dt, limit=20):
            return [meeting]

    mocker.patch.object(cmd_meetings, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["meetings", "--json"])
    assert result.exit_code == 0
    envelope = json.loads(result.output)
    assert envelope["ok"] is True
    assert len(envelope["data"]) == 1
    assert envelope["data"][0]["subject"] == "Standup"


def test_meetings_command_empty(runner, console_capture, mocker):
    class FakeClient:
        def get_meetings(self, start_dt, end_dt, limit=20):
            return []

    mocker.patch.object(cmd_meetings, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["meetings"])
    assert result.exit_code == 0
    assert "No upcoming meetings found" in console_capture.getvalue()


def test_meetings_command_past_flag(runner, mocker):
    class FakeClient:
        def __init__(self):
            self.calls = []

        def get_meetings(self, start_dt, end_dt, limit=20):
            self.calls.append(("start_dt", start_dt, "end_dt", end_dt))
            return []

    fake = FakeClient()
    mocker.patch.object(cmd_meetings, "_get_client", return_value=fake)

    result = runner.invoke(cli_mod.cli, ["meetings", "--past", "--json"])
    assert result.exit_code == 0
    assert len(fake.calls) == 1


def test_meetings_command_calls_formatter(runner, mocker):
    meeting = Meeting(
        id="e1", subject="Standup", display_num=1,
        start_time=datetime(2026, 3, 23, 9, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 3, 23, 9, 30, tzinfo=timezone.utc),
        organizer="Alice", organizer_email="",
        join_url="",
    )

    class FakeClient:
        def get_meetings(self, start_dt, end_dt, limit=20):
            return [meeting]

    mocker.patch.object(cmd_meetings, "_get_client", return_value=FakeClient())
    pm = mocker.patch.object(cmd_meetings, "print_meetings")

    result = runner.invoke(cli_mod.cli, ["meetings"])
    assert result.exit_code == 0
    pm.assert_called_once_with([meeting])


def test_recordings_command_json(runner, mocker):
    rec = Recording(
        id="rec-1",
        created_time=datetime(2026, 3, 23, 10, 0, tzinfo=timezone.utc),
    )

    class FakeClient:
        def get_recordings(self, meeting_num):
            return [rec]

    mocker.patch.object(cmd_meetings, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["recordings", "1", "--json"])
    assert result.exit_code == 0
    envelope = json.loads(result.output)
    assert envelope["ok"] is True
    assert len(envelope["data"]) == 1


def test_recordings_command_empty(runner, console_capture, mocker):
    class FakeClient:
        def get_recordings(self, meeting_num):
            return []

    mocker.patch.object(cmd_meetings, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["recordings", "1"])
    assert result.exit_code == 0
    assert "No recordings found" in console_capture.getvalue()


def test_transcripts_command_json(runner, mocker):
    tr = Transcript(
        id="tr-1",
        created_time=datetime(2026, 3, 23, 10, 0, tzinfo=timezone.utc),
    )

    class FakeClient:
        def get_transcripts(self, meeting_num):
            return [tr]

    mocker.patch.object(cmd_meetings, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["transcripts", "1", "--json"])
    assert result.exit_code == 0
    envelope = json.loads(result.output)
    assert envelope["ok"] is True
    assert len(envelope["data"]) == 1


def test_transcripts_command_view(runner, mocker):
    tr = Transcript(
        id="tr-1",
        created_time=datetime(2026, 3, 23, 10, 0, tzinfo=timezone.utc),
    )

    class FakeClient:
        def get_transcripts(self, meeting_num):
            return [tr]

        def get_transcript_content(self, meeting_num, transcript_id):
            return "WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nHello"

    mocker.patch.object(cmd_meetings, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["transcripts", "1", "--view"])
    assert result.exit_code == 0
    assert "WEBVTT" in result.output
    assert "Hello" in result.output


def test_transcripts_command_download(runner, mocker, tmp_path):
    tr = Transcript(
        id="tr-1",
        created_time=datetime(2026, 3, 23, 10, 0, tzinfo=timezone.utc),
    )
    out_file = str(tmp_path / "transcript.vtt")

    class FakeClient:
        def get_transcripts(self, meeting_num):
            return [tr]

        def get_transcript_content(self, meeting_num, transcript_id):
            return "WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nHello"

    mocker.patch.object(cmd_meetings, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["transcripts", "1", "--download", out_file])
    assert result.exit_code == 0
    with open(out_file, "r") as f:
        content = f.read()
        assert "WEBVTT" in content
        assert "Hello" in content


def test_transcripts_command_empty(runner, console_capture, mocker):
    class FakeClient:
        def get_transcripts(self, meeting_num):
            return []

    mocker.patch.object(cmd_meetings, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["transcripts", "1"])
    assert result.exit_code == 0
    assert "No transcripts found" in console_capture.getvalue()


def test_recording_from_drive_item():
    data = {
        "id": "drive-item-1",
        "createdDateTime": "2026-03-23T10:00:00Z",
        "name": "Tech standup-20260323_100000-Meeting Recording.mp4",
        "size": 52428800,
        "@microsoft.graph.downloadUrl": "https://download.example.com/rec",
    }
    r = Recording.from_drive_item(data)
    assert r.id == "drive-item-1"
    assert r.name == "Tech standup-20260323_100000-Meeting Recording.mp4"
    assert r.size == 52428800
    assert r.download_url == "https://download.example.com/rec"


def test_transcript_from_drive_item():
    data = {
        "id": "drive-item-2",
        "createdDateTime": "2026-03-23T10:30:00Z",
        "name": "Tech standup-20260323_103000-Meeting Transcript.mp4",
        "size": 4096,
    }
    t = Transcript.from_drive_item(data)
    assert t.id == "drive-item-2"
    assert t.name == "Tech standup-20260323_103000-Meeting Transcript.mp4"
    assert t.size == 4096
    assert t.download_url == ""


def test_get_recordings_falls_back_to_onedrive(teams_client: TeamsClient, mocker):
    teams_client._id_map["meetings"] = {
        "1": {
            "event_id": "e1",
            "join_url": "https://teams.microsoft.com/join/abc",
            "subject": "Tech standup",
            "start_time": "2026-03-23T13:30:00+00:00",
        },
    }

    call_count = {"resolve": 0, "onedrive": 0}

    def fake_graph_get(path, params=None, beta=False):
        if "onlineMeetings" in path and "recordings" not in path:
            call_count["resolve"] += 1
            return {"value": []}
        if "Recordings" in path:
            call_count["onedrive"] += 1
            return {"value": [
                {
                    "id": "d1",
                    "createdDateTime": "2026-03-23T13:32:00Z",
                    "name": "Tech standup-20260323_133200UTC-Meeting Recording.mp4",
                    "size": 50000000,
                },
                {
                    "id": "d2",
                    "createdDateTime": "2026-03-23T13:32:00Z",
                    "name": "Other meeting-20260323_133200UTC-Meeting Recording.mp4",
                    "size": 10000000,
                },
            ]}
        return {}

    mocker.patch.object(teams_client, "_graph_get", side_effect=fake_graph_get)

    recordings = teams_client.get_recordings("1")
    assert len(recordings) == 1
    assert recordings[0].name == "Tech standup-20260323_133200UTC-Meeting Recording.mp4"
    assert call_count["onedrive"] == 1


def test_onedrive_file_matches_meeting():
    item = {"name": "Tech standup-20260323_133200-Meeting Recording.mp4", "createdDateTime": "2026-03-23T13:32:00Z"}
    assert TeamsClient._onedrive_file_matches_meeting(item, "Tech standup", "2026-03-23T13:30:00+00:00")
    assert not TeamsClient._onedrive_file_matches_meeting(item, "Other meeting", "2026-03-23T13:30:00+00:00")
    assert not TeamsClient._onedrive_file_matches_meeting(item, "Tech standup", "2026-03-24T13:30:00+00:00")
    assert not TeamsClient._onedrive_file_matches_meeting(item, "", "2026-03-23T13:30:00+00:00")


def test_recordings_download(runner, console_capture, mocker, tmp_path):
    rec = Recording(
        id="rec-1",
        created_time=datetime(2026, 3, 23, 10, 0, tzinfo=timezone.utc),
    )

    class FakeClient:
        def get_recordings(self, meeting_num):
            return [rec]

        def download_recording(self, meeting_num, recording_id, output_path):
            with open(output_path, "wb") as f:
                f.write(b"fake-video-data")
            return 15

    mocker.patch.object(cmd_meetings, "_get_client", return_value=FakeClient())
    mocker.patch("os.getcwd", return_value=str(tmp_path))

    result = runner.invoke(cli_mod.cli, ["recordings", "1", "--download"])
    assert result.exit_code == 0
    assert "Saved" in console_capture.getvalue()


# ---------------------------------------------------------------------------
# Safety: transcript content must be text, never binary media
# ---------------------------------------------------------------------------


def _make_client_with_meeting(mocker) -> TeamsClient:
    """Helper to build a TeamsClient wired up with a meeting in the ID map."""
    client = TeamsClient.__new__(TeamsClient)
    client._id_map = {
        "chats": {},
        "messages": {},
        "meetings": {
            "1": {
                "event_id": "e1",
                "join_url": "",
                "subject": "Standup",
                "start_time": "2026-03-23T09:00:00+00:00",
            },
        },
    }
    return client


def test_transcript_content_rejects_video_content_type(mocker):
    """OneDrive returns video/mp4 → ValueError, no silent data return."""
    client = _make_client_with_meeting(mocker)

    mp4_resp = httpx.Response(
        200,
        content=b"\x00\x00\x00\x1cftypisom" + b"\x00" * 1024,
        headers={"content-type": "video/mp4", "content-length": "1048"},
        request=httpx.Request("GET", "https://graph.microsoft.com"),
    )
    mocker.patch.object(client, "_graph_get_content", return_value=mp4_resp)

    with pytest.raises(ValueError, match="media file"):
        client.get_transcript_content("1", "drive-item-999")


def test_transcript_content_rejects_octet_stream(mocker):
    """OneDrive returns application/octet-stream → ValueError."""
    client = _make_client_with_meeting(mocker)

    binary_resp = httpx.Response(
        200,
        content=b"\x00" * 2048,
        headers={"content-type": "application/octet-stream", "content-length": "2048"},
        request=httpx.Request("GET", "https://graph.microsoft.com"),
    )
    mocker.patch.object(client, "_graph_get_content", return_value=binary_resp)

    with pytest.raises(ValueError, match="media file"):
        client.get_transcript_content("1", "drive-item-999")


def test_transcript_content_rejects_oversized_file(mocker):
    """File larger than MAX_TRANSCRIPT_TEXT_SIZE → ValueError even if content-type is ok."""
    client = _make_client_with_meeting(mocker)

    big_resp = httpx.Response(
        200,
        content=b"x" * 100,
        headers={
            "content-type": "text/plain",
            "content-length": str(20 * 1024 * 1024),
        },
        request=httpx.Request("GET", "https://graph.microsoft.com"),
    )
    mocker.patch.object(client, "_graph_get_content", return_value=big_resp)

    with pytest.raises(ValueError, match="unexpectedly large"):
        client.get_transcript_content("1", "drive-item-999")


def test_transcript_content_rejects_mp4_magic_bytes(mocker):
    """File with MP4 magic bytes but neutral content-type → ValueError."""
    client = _make_client_with_meeting(mocker)

    # MP4 files start with 4 size bytes then 'ftyp'
    mp4_magic = b"\x00\x00\x00\x1c" + b"ftyp" + b"isom" + b"\x00" * 100
    sneaky_resp = httpx.Response(
        200,
        content=mp4_magic,
        headers={"content-type": "text/plain", "content-length": str(len(mp4_magic))},
        request=httpx.Request("GET", "https://graph.microsoft.com"),
    )
    mocker.patch.object(client, "_graph_get_content", return_value=sneaky_resp)

    with pytest.raises(ValueError, match="MP4 video"):
        client.get_transcript_content("1", "drive-item-999")


def test_transcript_content_allows_valid_vtt(mocker):
    """Legitimate VTT text passes all guards and is returned."""
    client = _make_client_with_meeting(mocker)

    vtt_text = "WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nHello, welcome.\n"
    vtt_resp = httpx.Response(
        200,
        content=vtt_text.encode("utf-8"),
        headers={"content-type": "text/vtt", "content-length": str(len(vtt_text))},
        request=httpx.Request("GET", "https://graph.microsoft.com"),
    )
    mocker.patch.object(client, "_graph_get_content", return_value=vtt_resp)

    result = client.get_transcript_content("1", "tr-1")
    assert result == vtt_text


def test_transcript_content_allows_plain_text(mocker):
    """Plain-text transcript (no VTT headers) is still accepted if it's small text."""
    client = _make_client_with_meeting(mocker)

    plain = "Speaker 1: Hello\nSpeaker 2: Hi there\n"
    plain_resp = httpx.Response(
        200,
        content=plain.encode("utf-8"),
        headers={"content-type": "text/plain", "content-length": str(len(plain))},
        request=httpx.Request("GET", "https://graph.microsoft.com"),
    )
    mocker.patch.object(client, "_graph_get_content", return_value=plain_resp)

    result = client.get_transcript_content("1", "tr-1")
    assert "Hello" in result


def test_transcripts_view_rejects_media_file(runner, console_capture, mocker):
    """'transcripts --view' shows error when get_transcript_content raises."""
    tr = Transcript(
        id="tr-1",
        created_time=datetime(2026, 3, 23, 10, 0, tzinfo=timezone.utc),
    )

    class FakeClient:
        def get_transcripts(self, meeting_num):
            return [tr]

        def get_transcript_content(self, meeting_num, transcript_id):
            raise ValueError("Transcript is stored as a media file, not text.")

    mocker.patch.object(cmd_meetings, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["transcripts", "1", "--view"])
    assert result.exit_code != 0 or "media file" in console_capture.getvalue() or "media file" in result.output


def test_transcripts_download_rejects_media_file(runner, console_capture, mocker, tmp_path):
    """'transcripts --download' refuses to save when content is media."""
    tr = Transcript(
        id="tr-1",
        created_time=datetime(2026, 3, 23, 10, 0, tzinfo=timezone.utc),
    )
    out_file = str(tmp_path / "transcript.vtt")

    class FakeClient:
        def get_transcripts(self, meeting_num):
            return [tr]

        def get_transcript_content(self, meeting_num, transcript_id):
            raise ValueError("Transcript is stored as a media file, not text.")

    mocker.patch.object(cmd_meetings, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["transcripts", "1", "--download", out_file])
    import os
    assert not os.path.exists(out_file), "Media file should NOT have been written to disk"


# ---------------------------------------------------------------------------
# SharePoint transcript fallback tests
# ---------------------------------------------------------------------------


def test_get_sharepoint_transcripts_returns_transcripts(teams_client: TeamsClient, mocker):
    """SharePoint media/transcripts API returns transcript list."""
    meeting_info = {
        "event_id": "e1",
        "join_url": "",
        "subject": "Standup",
        "start_time": "2026-03-23T09:00:00+00:00",
    }
    rec = Recording(
        id="drive-item-1",
        created_time=datetime(2026, 3, 23, 9, 0, tzinfo=timezone.utc),
        name="Standup-20260323_090000-Meeting Recording.mp4",
    )
    mocker.patch.object(
        teams_client, "_search_onedrive_recordings", return_value=[rec],
    )
    mocker.patch.object(
        teams_client, "_derive_sharepoint_host",
        return_value="mysunlighten-my.sharepoint.com",
    )
    mocker.patch.object(
        teams_client, "_get_onedrive_drive_id", return_value="drive-abc",
    )
    mocker.patch.object(
        teams_client, "_sharepoint_api_call",
        return_value={
            "value": [
                {
                    "id": "sp-tr-1",
                    "displayName": "Transcript.vtt",
                    "createdDateTime": "2026-03-23T09:30:00Z",
                    "temporaryDownloadUrl": "https://download.example.com/vtt",
                }
            ]
        },
    )

    result = teams_client._get_sharepoint_transcripts(meeting_info)
    assert len(result) == 1
    assert result[0].id == "sp-tr-1"
    assert result[0].name == "Transcript.vtt"
    assert result[0]._recording_item_id == "drive-item-1"


def test_get_sharepoint_transcripts_no_recordings(teams_client: TeamsClient, mocker):
    """Returns empty when no recordings match."""
    mocker.patch.object(teams_client, "_search_onedrive_recordings", return_value=[])

    result = teams_client._get_sharepoint_transcripts({"subject": "X", "start_time": ""})
    assert result == []


def test_get_sharepoint_transcripts_no_sp_host(teams_client: TeamsClient, mocker):
    """Returns empty when SharePoint host cannot be derived."""
    rec = Recording(id="d1", created_time=datetime(2026, 3, 23, tzinfo=timezone.utc))
    mocker.patch.object(teams_client, "_search_onedrive_recordings", return_value=[rec])
    mocker.patch.object(teams_client, "_derive_sharepoint_host", return_value="")

    result = teams_client._get_sharepoint_transcripts({"subject": "X", "start_time": ""})
    assert result == []


def test_derive_sharepoint_host_from_graph(teams_client: TeamsClient, mocker):
    """Derives SP host from Graph /me/drive/root webUrl."""
    mocker.patch.object(
        teams_client, "_graph_get",
        return_value={"webUrl": "https://mysunlighten-my.sharepoint.com/personal/asami_sunlighten_com/Documents"},
    )
    assert teams_client._derive_sharepoint_host() == "mysunlighten-my.sharepoint.com"


def test_derive_sharepoint_host_fallback(teams_client: TeamsClient, mocker):
    """Returns empty string when Graph call fails."""
    mocker.patch.object(teams_client, "_graph_get", side_effect=Exception("fail"))
    assert teams_client._derive_sharepoint_host() == ""


def test_sharepoint_api_call_no_browser_state(teams_client: TeamsClient):
    """Returns None when no browser state file exists."""
    result = teams_client._sharepoint_api_call("https://sp.example.com", "/api/test")
    assert result is None


def test_get_sharepoint_transcript_content(teams_client: TeamsClient, mocker):
    """Downloads VTT content via SharePoint temporary URL."""
    rec = Recording(
        id="drive-item-1",
        created_time=datetime(2026, 3, 23, 9, 0, tzinfo=timezone.utc),
        name="Standup-20260323_090000-Meeting Recording.mp4",
    )
    mocker.patch.object(teams_client, "_search_onedrive_recordings", return_value=[rec])
    mocker.patch.object(
        teams_client, "_derive_sharepoint_host",
        return_value="mysunlighten-my.sharepoint.com",
    )
    mocker.patch.object(teams_client, "_get_onedrive_drive_id", return_value="drive-abc")
    mocker.patch.object(
        teams_client, "_sharepoint_api_call",
        return_value={
            "value": [
                {
                    "id": "sp-tr-1",
                    "displayName": "Transcript.vtt",
                    "temporaryDownloadUrl": "https://download.example.com/vtt",
                }
            ]
        },
    )

    fake_resp = httpx.Response(
        200,
        text="WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nHello",
        request=httpx.Request("GET", "https://download.example.com/vtt"),
    )
    mocker.patch.object(teams_client._session.client, "request", return_value=fake_resp)

    meeting_info = {"subject": "Standup", "start_time": "2026-03-23T09:00:00+00:00"}
    content = teams_client._get_sharepoint_transcript_content(meeting_info, "sp-tr-1")
    assert content is not None
    assert "WEBVTT" in content


def test_get_transcripts_sharepoint_fallback(teams_client: TeamsClient, mocker):
    """get_transcripts uses SharePoint fallback when Graph onlineMeetings fails."""
    teams_client._id_map["meetings"] = {
        "1": {
            "event_id": "e1",
            "join_url": "https://teams.microsoft.com/join/abc",
            "subject": "Standup",
            "start_time": "2026-03-23T09:00:00+00:00",
        },
    }

    # Graph onlineMeetings lookup fails (no permission)
    def fake_graph_get(path, params=None, beta=False):
        if "onlineMeetings" in path:
            return {"value": []}
        return {}

    mocker.patch.object(teams_client, "_graph_get", side_effect=fake_graph_get)

    sp_transcript = Transcript(
        id="sp-tr-1",
        created_time=datetime(2026, 3, 23, 10, 0, tzinfo=timezone.utc),
        name="Transcript.vtt",
    )
    mocker.patch.object(
        teams_client, "_get_sharepoint_transcripts", return_value=[sp_transcript],
    )

    result = teams_client.get_transcripts("1")
    assert len(result) == 1
    assert result[0].id == "sp-tr-1"
