"""Tab completion on request, never as a side effect; examples in every --help.

`lium` used to append a completion line to the shell rc file the first time it
ran, whoever ran it — a CI job or an agent included. Installing is now an
explicit `lium completion --install`, the silent path only runs for a person
at a terminal, and every command's help shows how it is used.
"""

from pathlib import Path

import pytest
from click.testing import CliRunner

from lium.cli import completion
from lium.cli.cli import cli
from lium.cli.utils import EXIT_CONFIGURATION_ERROR


@pytest.fixture
def rc_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(completion, "SHELLS", {
        "bash": (str(tmp_path / ".bashrc"), completion.SHELLS["bash"][1]),
        "zsh": (str(tmp_path / ".zshrc"), completion.SHELLS["zsh"][1]),
        "fish": (str(tmp_path / ".config/fish/config.fish"), completion.SHELLS["fish"][1]),
    })
    return tmp_path


# --- lium completion -----------------------------------------------------------------------

@pytest.mark.parametrize("shell, needle", [
    ("bash", "_LIUM_COMPLETE=bash_source"),
    ("zsh", "_LIUM_COMPLETE=zsh_source"),
    ("fish", "_LIUM_COMPLETE=fish_source"),
])
def test_completion_prints_the_line_for_the_named_shell(rc_home, shell, needle):
    result = CliRunner().invoke(cli, ["completion", shell])

    assert result.exit_code == 0, result.output
    assert needle in result.output and result.output.strip().count("\n") == 0


def test_completion_defaults_to_the_current_shell(rc_home, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/zsh")

    result = CliRunner().invoke(cli, ["completion"])

    assert result.exit_code == 0 and "zsh_source" in result.output


def test_completion_with_an_unknown_shell_exits_configuration_error(rc_home, monkeypatch):
    monkeypatch.setenv("SHELL", "/usr/bin/tcsh")

    result = CliRunner().invoke(cli, ["completion"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert "bash" in result.output and "zsh" in result.output


def test_completion_install_appends_once(rc_home, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/bash")
    rc = rc_home / ".bashrc"
    rc.write_text("# mine\n")

    first = CliRunner().invoke(cli, ["completion", "--install"])
    second = CliRunner().invoke(cli, ["completion", "--install"])

    assert first.exit_code == 0 and "Added" in first.output
    assert second.exit_code == 0 and "already" in second.output
    assert rc.read_text().count("_LIUM_COMPLETE") == 1
    assert rc.read_text().startswith("# mine\n")


def test_completion_install_creates_the_fish_config_directory(rc_home):
    result = CliRunner().invoke(cli, ["completion", "fish", "--install"])

    assert result.exit_code == 0, result.output
    assert "_LIUM_COMPLETE=fish_source" in (rc_home / ".config/fish/config.fish").read_text()


# --- silent install at startup -------------------------------------------------------------

def test_ensure_completion_does_nothing_without_a_terminal(rc_home, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr("lium.cli.interactive.is_interactive", lambda: False)

    completion.ensure_completion()

    assert not (rc_home / ".zshrc").exists()
    assert not (rc_home / ".lium_completion_installed").exists()


def test_ensure_completion_installs_for_a_person(rc_home, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr("lium.cli.interactive.is_interactive", lambda: True)

    completion.ensure_completion()

    assert "_LIUM_COMPLETE=zsh_source" in (rc_home / ".zshrc").read_text()
    assert (rc_home / ".lium_completion_installed").exists()


# --- help ----------------------------------------------------------------------------------

def _leaf_commands(group, prefix=()):
    for name, command in sorted(group.commands.items()):
        if isinstance(command, type(cli)) and command.commands:
            yield prefix + (name,), command
            yield from _leaf_commands(command, prefix + (name,))
        else:
            yield prefix + (name,), command


TOP_LEVEL_WITHOUT_EXAMPLES_YET = {
    # third-party or operator tooling with its own docs; not part of the pod workflow
    "gpu-splitting", "mine", "provider", "logs", "update", "signup", "topup", "port-forward",
    # examples arrive with the branches that rework these commands (ls filters, ps sort/filter,
    # rsync options, templates arch); listed here so this test does not conflict with them
    "ls", "ps", "rsync", "templates",
}


def test_every_pod_workflow_command_help_has_examples():
    missing = []
    for path, command in _leaf_commands(cli):
        if len(path) != 1 or path[0] in TOP_LEVEL_WITHOUT_EXAMPLES_YET:
            continue
        result = CliRunner().invoke(cli, [*path, "--help"])
        if "example" not in result.output.lower():
            missing.append(" ".join(path))
    assert not missing, f"--help without examples: {missing}"


def test_reboot_accepts_yes_for_symmetry(monkeypatch):
    from lium.cli.reboot import command as reboot_module

    class _Lium:
        def __init__(self, *a, **k):
            pass

        def ps(self):
            return []

    monkeypatch.setattr(reboot_module, "Lium", _Lium)

    result = CliRunner().invoke(cli, ["reboot", "--all", "--yes"])

    assert "no such option" not in result.output.lower()
