"""Tab completion on request, never as a side effect; examples in every --help.

`lium` used to append a completion line to the shell rc file the first time it
ran, whoever ran it — a CI job or an agent included. Installing is now an
explicit `lium completion --install`, the silent path only runs for a person
at a terminal, and every command's help shows how it is used.
"""

import re
import sys
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


def test_completion_install_shows_a_home_with_brackets_literally(rc_home, monkeypatch):
    # `[work]` reads as Rich markup: unescaped, the tag is eaten and the path is not what the shell needs
    rc = rc_home / "home [work]" / ".bashrc"
    rc.parent.mkdir()
    monkeypatch.setitem(completion.SHELLS, "bash", (str(rc), completion.SHELLS["bash"][1]))

    first = CliRunner().invoke(cli, ["completion", "bash", "--install"])
    second = CliRunner().invoke(cli, ["completion", "bash", "--install"])

    # Rich wraps the long tmp path at the console width: compare with newlines removed
    assert first.exit_code == 0 and "[work]/.bashrc" in first.output.replace("\n", ""), first.output
    assert second.exit_code == 0 and "already in" in second.output and "[work]/.bashrc" in second.output.replace("\n", ""), second.output


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


def test_ensure_completion_names_a_home_with_brackets_literally(rc_home, monkeypatch, capsys):
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr("lium.cli.interactive.is_interactive", lambda: True)
    rc = rc_home / "home [work]" / ".zshrc"
    rc.parent.mkdir()
    monkeypatch.setitem(completion.SHELLS, "zsh", (str(rc), completion.SHELLS["zsh"][1]))

    completion.ensure_completion()

    assert "[work]/.zshrc" in capsys.readouterr().err.replace("\n", "")


def test_ensure_completion_appends_once_and_is_quiet_when_the_line_is_there(rc_home, monkeypatch, capsys):
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr("lium.cli.interactive.is_interactive", lambda: True)
    rc = rc_home / ".zshrc"
    rc.write_text("# mine\n" + completion.completion_script("zsh") + "\n")

    completion.ensure_completion()

    assert rc.read_text().count("_LIUM_COMPLETE") == 1
    assert (rc_home / ".lium_completion_installed").exists()
    assert "configured" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["lium", "completion", "bash"],
        ["lium", "-w", "team", "completion", "bash"],
        ["lium", "-wteam", "completion", "bash"],
        ["lium", "--workspace=team", "completion", "bash"],
    ],
)
def test_main_does_not_run_the_silent_install_for_the_completion_command(rc_home, monkeypatch, argv):
    """`lium completion bash >> ~/.bashrc` on a fresh install must write the line once, not twice —
    also when the group's `-w NAME` / `--workspace=NAME` comes first."""
    from lium.cli import cli as cli_module

    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.delenv("_LIUM_COMPLETE", raising=False)
    monkeypatch.setattr("lium.cli.interactive.is_interactive", lambda: True)
    monkeypatch.setattr(cli_module, "maybe_perform_startup_update", lambda: None)
    monkeypatch.setattr(sys, "argv", argv)
    printed = []
    monkeypatch.setattr(cli_module, "cli", lambda: printed.append(CliRunner().invoke(cli, ["completion", "bash"]).output))

    cli_module.main()

    assert not (rc_home / ".bashrc").exists(), "the startup path edited the rc file"
    assert not (rc_home / ".lium_completion_installed").exists()
    assert "_LIUM_COMPLETE=bash_source" in printed[0]


# --- help ----------------------------------------------------------------------------------

def _leaf_commands(group, prefix=()):
    for name, command in sorted(group.commands.items()):
        if isinstance(command, type(cli)) and command.commands:
            yield prefix + (name,), command
            yield from _leaf_commands(command, prefix + (name,))
        else:
            yield prefix + (name,), command


TOP_LEVEL_WITHOUT_EXAMPLES_YET = {
    # provider-side tooling with its own docs; not part of the pod workflow
    "gpu-splitting", "mine", "provider",
}

# An example is an "Examples:" block or, as `templates` and `workspaces` write it, an indented `lium …` line.
_EXAMPLE_LINE = re.compile(r"^\s+lium\b", re.MULTILINE)


def test_every_pod_workflow_command_help_has_examples():
    missing = []
    for path, command in _leaf_commands(cli):
        if len(path) != 1 or path[0] in TOP_LEVEL_WITHOUT_EXAMPLES_YET:
            continue
        result = CliRunner().invoke(cli, [*path, "--help"])
        if "example" not in result.output.lower() and not _EXAMPLE_LINE.search(result.output):
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
