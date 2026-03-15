from __future__ import annotations

import teams_cli.cli as cli_mod
import teams_cli.commands.message_manage as cmd_message_manage


def test_edit_with_yes_calls_client(runner, mocker):
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def edit_message(self, msg_num: str, new_text: str):
            self.calls.append((msg_num, new_text))

    fake_client = FakeClient()
    mocker.patch.object(cmd_message_manage, "_get_client", return_value=fake_client)

    result = runner.invoke(cli_mod.cli, ["edit", "7", "updated text", "-y"])

    assert result.exit_code == 0
    assert fake_client.calls == [("7", "updated text")]


def test_delete_with_yes_calls_client(runner, mocker):
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def delete_message(self, msg_num: str):
            self.calls.append(msg_num)

    fake_client = FakeClient()
    mocker.patch.object(cmd_message_manage, "_get_client", return_value=fake_client)

    result = runner.invoke(cli_mod.cli, ["delete", "3", "-y"])

    assert result.exit_code == 0
    assert fake_client.calls == ["3"]


def test_edit_without_yes_prompts_confirmation(runner, mocker):
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def edit_message(self, msg_num: str, new_text: str):
            self.calls.append((msg_num, new_text))

    fake_client = FakeClient()
    mocker.patch.object(cmd_message_manage, "_get_client", return_value=fake_client)

    # Answer 'y' to confirmation prompt
    result = runner.invoke(cli_mod.cli, ["edit", "7", "updated text"], input="y\n")

    assert result.exit_code == 0
    assert fake_client.calls == [("7", "updated text")]


def test_delete_without_yes_prompts_confirmation(runner, mocker):
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def delete_message(self, msg_num: str):
            self.calls.append(msg_num)

    fake_client = FakeClient()
    mocker.patch.object(cmd_message_manage, "_get_client", return_value=fake_client)

    # Answer 'y' to confirmation prompt
    result = runner.invoke(cli_mod.cli, ["delete", "3"], input="y\n")

    assert result.exit_code == 0
    assert fake_client.calls == ["3"]


def test_edit_abort_on_no_confirmation(runner, mocker):
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def edit_message(self, msg_num: str, new_text: str):
            self.calls.append((msg_num, new_text))

    fake_client = FakeClient()
    mocker.patch.object(cmd_message_manage, "_get_client", return_value=fake_client)

    # Answer 'n' to abort
    result = runner.invoke(cli_mod.cli, ["edit", "7", "updated text"], input="n\n")

    assert result.exit_code != 0
    assert fake_client.calls == []


def test_delete_abort_on_no_confirmation(runner, mocker):
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def delete_message(self, msg_num: str):
            self.calls.append(msg_num)

    fake_client = FakeClient()
    mocker.patch.object(cmd_message_manage, "_get_client", return_value=fake_client)

    # Answer 'n' to abort
    result = runner.invoke(cli_mod.cli, ["delete", "3"], input="n\n")

    assert result.exit_code != 0
    assert fake_client.calls == []
