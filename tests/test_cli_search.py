from __future__ import annotations

import json
from pathlib import Path

import teams_cli.cli as cli_mod
import teams_cli.commands.attachments as cmd_attachments
import teams_cli.commands.search as cmd_search


def test_search_passes_filters_and_outputs_json(runner, mocker, make_message):
    message = make_message(msg_id="m-search")

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def search_messages(
            self,
            query: str,
            top: int = 25,
            chat_num: str | None = None,
            from_filter: str | None = None,
            after: str | None = None,
            before: str | None = None,
        ):
            self.calls.append((query, top, chat_num, from_filter, after, before))
            return [message]

    fake_client = FakeClient()
    mocker.patch.object(cmd_search, "_get_client", return_value=fake_client)

    result = runner.invoke(
        cli_mod.cli,
        [
            "search",
            "deploy",
            "--max",
            "5",
            "--chat",
            "3",
            "--from",
            "alice",
            "--after",
            "2026-03-01",
            "--before",
            "2026-03-31",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert fake_client.calls == [
        ("deploy", 5, "3", "alice", "2026-03-01", "2026-03-31")
    ]
    assert json.loads(result.output)["data"][0]["id"] == "m-search"


def test_user_search_nonempty_calls_formatter(runner, mocker, make_user):
    user = make_user(display_name="Alice", email="alice@example.com")

    class FakeClient:
        def search_users(self, query: str):
            assert query == "alice"
            return [user]

    print_users = mocker.patch.object(cmd_search, "print_users")
    mocker.patch.object(cmd_search, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["user-search", "alice"])

    assert result.exit_code == 0
    print_users.assert_called_once_with([user])


def test_user_search_empty_prints_error(runner, console_capture, mocker):
    class FakeClient:
        def search_users(self, query: str):
            return []

    mocker.patch.object(cmd_search, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["user-search", "nobody"])

    assert result.exit_code == 0
    assert "No users found matching 'nobody'" in console_capture.getvalue()


def test_attachments_json_outputs_payload(runner, mocker, make_attachment):
    attachment = make_attachment(name="report.pdf", content_type="application/pdf")

    class FakeClient:
        def get_attachments(self, msg_num: str):
            assert msg_num == "4"
            return [attachment]

    mocker.patch.object(cmd_attachments, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["attachments", "4", "--json"])

    assert result.exit_code == 0
    envelope = json.loads(result.output)
    assert envelope["ok"] is True
    payload = envelope["data"]
    assert payload[0]["name"] == "report.pdf"


def test_attachments_download_saves_files(runner, console_capture, mocker, make_attachment, tmp_path: Path):
    attachment = make_attachment(
        name="inline.png",
        content_type="image/png",
        content_url="https://files.example/inline.png",
        size=12,
        is_inline=True,
    )

    class FakeClient:
        def get_attachments(self, msg_num: str):
            return [attachment]

        def download_attachment(self, att):
            assert att is attachment
            return b"png-data"

    mocker.patch.object(cmd_attachments, "_get_client", return_value=FakeClient())

    result = runner.invoke(
        cli_mod.cli,
        ["attachments", "4", "--download", "--save-to", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert (tmp_path / "inline.png").read_bytes() == b"png-data"
    rendered = console_capture.getvalue()
    assert "inline.png" in rendered
    assert "Saved:" in rendered


def test_attachments_download_reports_failures(runner, console_capture, mocker, make_attachment, tmp_path: Path):
    attachment = make_attachment(name="broken.txt", content_type="text/plain")

    class FakeClient:
        def get_attachments(self, msg_num: str):
            return [attachment]

        def download_attachment(self, att):
            raise RuntimeError("boom")

    mocker.patch.object(cmd_attachments, "_get_client", return_value=FakeClient())

    result = runner.invoke(
        cli_mod.cli,
        ["attachments", "4", "--download", "--save-to", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert "Failed: broken.txt" in console_capture.getvalue()
