from __future__ import annotations

import json

import teams_cli.cli as cli_mod
import teams_cli.commands.teams_channels as cmd_teams


def test_teams_json_outputs_joined_teams(runner, mocker, make_team):
    team = make_team(name="Platform")

    class FakeClient:
        def get_joined_teams(self):
            return [team]

    mocker.patch.object(cmd_teams, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["teams", "--json"])

    assert result.exit_code == 0
    envelope = json.loads(result.output)
    assert envelope["ok"] is True
    payload = envelope["data"]
    assert payload[0]["name"] == "Platform"


def test_channels_empty_state_prints_message(runner, console_capture, mocker):
    class FakeClient:
        def get_channels(self, team_num: str):
            assert team_num == "3"
            return []

    mocker.patch.object(cmd_teams, "_get_client", return_value=FakeClient())

    result = runner.invoke(cli_mod.cli, ["channels", "3"])

    assert result.exit_code == 0
    assert "No channels in team #3." in console_capture.getvalue()
