"""`lium rm` without a terminal needs `--yes`; a pipe is not approval (DAH-3332).

Before this, `lium rm my-pod` piped from a script, an agent or `echo y |` removed
the pod with no prompt and no flag — DAH-2883 had closed that only for `--all`
and row numbers. Now every `rm` that nobody can prompt fails with
`confirmation_required` (exit 2), names the pods it would have removed and the
command to re-run, and removes or schedules nothing. On a terminal nothing
changes; with `--yes` nothing changes.

Every run goes through the real parser (``CliRunner().invoke(cli, ["rm", …])``);
``_run`` tells ``interactive.stdin_is_terminal`` whether a terminal is attached
(``terminal=False`` is the default: the pipe) and fails the test if a prompt is
ever shown.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli import interactive, ui, utils
from lium.cli.cli import cli
from lium.cli.rm import command as rm_module
from lium.cli.utils import EXIT_CONFIGURATION_ERROR, EXIT_POD_NOT_FOUND, store_pod_selection
from lium.sdk import PodInfo


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(interactive.NONINTERACTIVE_ENV, raising=False)
    monkeypatch.delenv("LIUM_OUTPUT", raising=False)
    monkeypatch.delenv("LIUM_NO_POD_INDEX", raising=False)


def _pod(huid: str, name: str | None = None) -> PodInfo:
    return PodInfo(
        id=f"id-{huid}",
        name=name or huid,
        status="RUNNING",
        huid=huid,
        ssh_cmd="ssh user@203.0.113.10 -p 20000",
        ports={},
        created_at="2026-09-05T10:00:00Z",
        updated_at="2026-09-05T10:00:00Z",
        executor=None,
        template={},
        removal_scheduled_at=None,
        jupyter_installation_status=None,
        jupyter_url=None,
    )


TRAIN = _pod("eager-wolf-aa", "train")
EVAL = _pod("brave-otter-11", "eval")


class _RecordingLium:
    """The two calls `rm` makes to the API, recorded: `ps` answers with `pods`; `rm` and
    `schedule_termination` only record — the test asserts they were or were not reached."""

    pods: list[PodInfo] = []
    removed: list[str] = []
    scheduled: list[tuple[str, str]] = []
    # a server without workspaces: `rm` reads it for its workspace line (lium#183)
    workspaces = SimpleNamespace(current=lambda: None)

    def __init__(self, *args, **kwargs):
        pass

    def ps(self):
        return list(self.pods)

    def rm(self, pod):
        _RecordingLium.removed.append(pod.huid)

    def schedule_termination(self, pod, termination_time=None):
        _RecordingLium.scheduled.append((pod.huid, termination_time))


def _run(monkeypatch, pods, args, *, terminal: bool = False, input: str | None = None, env=None):
    _RecordingLium.pods = pods
    _RecordingLium.removed = []
    _RecordingLium.scheduled = []
    monkeypatch.setattr(rm_module, "Lium", _RecordingLium)
    monkeypatch.setattr(interactive, "stdin_is_terminal", lambda: terminal)
    monkeypatch.setattr(ui.Confirm, "ask", lambda *a, **k: pytest.fail("a prompt was shown"))
    return CliRunner().invoke(cli, ["rm", *args], input=input, env=env)


def _text(result) -> str:
    """The output with Rich's 80-column soft wrap undone: one message, whatever the width."""
    return " ".join(result.output.split())


def _refused(result) -> None:
    assert result.exit_code == EXIT_CONFIGURATION_ERROR, result.output
    assert "nothing done because stdin is not a terminal" in _text(result)
    assert _RecordingLium.removed == []
    assert _RecordingLium.scheduled == []


# --- piped, no --yes: refused, nothing removed ------------------------------------

def test_a_piped_rm_of_a_named_pod_is_refused_and_names_the_pod(monkeypatch):
    result = _run(monkeypatch, [TRAIN, EVAL], ["eager-wolf-aa"])

    _refused(result)
    assert "Would remove 1 pod(s): eager-wolf-aa" in _text(result)
    assert "Re-run with --yes: lium rm eager-wolf-aa --yes" in _text(result)


def test_the_refusal_is_one_message_with_no_separate_hint(monkeypatch):
    """The re-run command is the message's last clause, so `handle_errors` prints no second hint line."""
    result = _run(monkeypatch, [TRAIN], ["eager-wolf-aa"])

    assert _text(result) == (
        "Would remove 1 pod(s): eager-wolf-aa — nothing done because stdin is not a terminal. "
        "Re-run with --yes: lium rm eager-wolf-aa --yes"
    )


def test_a_piped_rm_of_several_pods_names_every_one(monkeypatch):
    result = _run(monkeypatch, [TRAIN, EVAL], ["eager-wolf-aa,brave-otter-11"])

    _refused(result)
    assert "Would remove 2 pod(s): eager-wolf-aa, brave-otter-11" in _text(result)
    assert "lium rm eager-wolf-aa,brave-otter-11 --yes" in _text(result)


def test_a_piped_rm_all_is_refused_and_lists_the_account(monkeypatch):
    result = _run(monkeypatch, [TRAIN, EVAL], ["--all"])

    _refused(result)
    assert "Would remove 2 pod(s): eager-wolf-aa, brave-otter-11" in _text(result)
    assert "Re-run with --yes: lium rm --all --yes" in _text(result)


def test_a_piped_rm_by_row_number_is_refused_and_names_the_pod_behind_it(monkeypatch, tmp_path):
    """The row-number path lost its direct-call test in test_cli_noninteractive.py; this is it through the CLI."""
    monkeypatch.setattr(utils.config, "config_dir", tmp_path)   # the `lium ps` snapshot, never the real ~/.lium
    store_pod_selection([TRAIN, EVAL], now=datetime.now(timezone.utc))

    result = _run(monkeypatch, [TRAIN, EVAL], ["1"])

    _refused(result)
    assert "Would remove 1 pod(s): eager-wolf-aa" in _text(result)
    assert "lium rm 1 --yes" in _text(result)


def test_a_piped_yes_is_not_approval(monkeypatch):
    """The negative control for the old rule: `echo y | lium rm my-pod` removed the pod."""
    result = _run(monkeypatch, [TRAIN], ["eager-wolf-aa"], input="y\n")

    _refused(result)


def test_a_piped_scheduled_removal_is_refused_and_schedules_nothing(monkeypatch):
    result = _run(monkeypatch, [TRAIN], ["eager-wolf-aa", "--in", "6h"])

    _refused(result)
    assert "Would schedule removal of 1 pod(s): eager-wolf-aa" in _text(result)
    assert "lium rm eager-wolf-aa --in 6h --yes" in _text(result)


def test_the_opt_out_env_var_refuses_even_on_a_terminal(monkeypatch):
    monkeypatch.setenv(interactive.NONINTERACTIVE_ENV, "1")

    result = _run(monkeypatch, [TRAIN], ["eager-wolf-aa"], terminal=True)

    assert result.exit_code == EXIT_CONFIGURATION_ERROR, result.output
    assert f"nothing done because {interactive.NONINTERACTIVE_ENV} is set" in _text(result)
    assert _RecordingLium.removed == []


def test_the_refusal_is_a_json_envelope_for_a_machine_reader(monkeypatch):
    result = _run(monkeypatch, [TRAIN], ["eager-wolf-aa"], env={"LIUM_OUTPUT": "json"})

    assert result.exit_code == EXIT_CONFIGURATION_ERROR, result.output
    envelope = json.loads(result.output.strip().splitlines()[-1])
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "confirmation_required"
    assert envelope["error"]["hint"] == "Re-run with --yes: lium rm eager-wolf-aa --yes"
    assert _RecordingLium.removed == []


def test_a_typo_is_still_pod_not_found_not_a_refusal(monkeypatch):
    """Resolution runs first: a name that matches nothing keeps its own exit code."""
    result = _run(monkeypatch, [TRAIN], ["no-such-pod-zz"])

    assert result.exit_code == EXIT_POD_NOT_FOUND, result.output
    assert "No pods match targets: no-such-pod-zz" in result.output
    assert _RecordingLium.removed == []


# --- piped, with --yes: proceeds ------------------------------------------------------

@pytest.mark.parametrize("flag", ["--yes", "-y"])
def test_a_piped_rm_with_yes_removes_the_pod(monkeypatch, flag):
    result = _run(monkeypatch, [TRAIN, EVAL], ["eager-wolf-aa", flag])

    assert result.exit_code == 0, result.output
    assert _RecordingLium.removed == ["eager-wolf-aa"]
    assert "Removed 1 pod(s): eager-wolf-aa" in result.output


def test_a_piped_rm_all_with_yes_removes_every_pod(monkeypatch):
    result = _run(monkeypatch, [TRAIN, EVAL], ["--all", "--yes"])

    assert result.exit_code == 0, result.output
    assert _RecordingLium.removed == ["eager-wolf-aa", "brave-otter-11"]


def test_a_piped_scheduled_removal_with_yes_schedules(monkeypatch):
    result = _run(monkeypatch, [TRAIN], ["eager-wolf-aa", "--in", "6h", "-y"])

    assert result.exit_code == 0, result.output
    assert [huid for huid, _ in _RecordingLium.scheduled] == ["eager-wolf-aa"]


# --- on a terminal: unchanged ---------------------------------------------------------

def test_on_a_terminal_a_named_pod_is_removed_without_a_prompt(monkeypatch):
    """What a human at a shell had before this change: no prompt for a named pod."""
    result = _run(monkeypatch, [TRAIN, EVAL], ["eager-wolf-aa"], terminal=True)

    assert result.exit_code == 0, result.output
    assert _RecordingLium.removed == ["eager-wolf-aa"]


def test_on_a_terminal_rm_all_still_asks_and_no_means_no(monkeypatch):
    _RecordingLium.pods = [TRAIN, EVAL]
    _RecordingLium.removed = []
    monkeypatch.setattr(rm_module, "Lium", _RecordingLium)
    monkeypatch.setattr(interactive, "stdin_is_terminal", lambda: True)
    questions: list[str] = []
    monkeypatch.setattr(ui.Confirm, "ask", lambda message, default=False: questions.append(message) or False)

    result = CliRunner().invoke(cli, ["rm", "--all"])

    assert result.exit_code == 0, result.output
    assert questions == ["Remove all 2 pods (eager-wolf-aa, brave-otter-11)?"]
    assert _RecordingLium.removed == []


def test_on_a_terminal_rm_all_with_yes_asks_nothing(monkeypatch):
    result = _run(monkeypatch, [TRAIN, EVAL], ["--all", "-y"], terminal=True)

    assert result.exit_code == 0, result.output
    assert _RecordingLium.removed == ["eager-wolf-aa", "brave-otter-11"]


# --- the command line the refusal prints ----------------------------------------------

def test_rerun_line_carries_every_option_and_quotes_what_needs_it():
    line = rm_module.rerun_with_yes("a,b", False, None, "tomorrow 09:00", True)

    assert line == "lium rm a,b --at 'tomorrow 09:00' --name-only --yes"


def test_rerun_line_for_all_has_no_targets():
    assert rm_module.rerun_with_yes(None, True, "45m", None, False) == "lium rm --all --in 45m --yes"


def test_rerun_line_knows_every_rm_option():
    """A new `rm` option (lium#218 adds --format) has to be carried by rerun_with_yes too, or the
    printed command is not the one the caller ran. This pins the option list; extend both together."""
    assert {param.name for param in rm_module.rm_command.params} == {
        "targets", "remove_all", "yes", "in_duration", "at_time", "name_only",
    }
