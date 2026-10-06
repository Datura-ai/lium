"""`lium exec -e KEY=VALUE` must not echo secret values into the terminal.

Passing a token with ``-e`` is a common pattern; the human-readable status line
used to print the full ``KEY=VALUE``, dropping the secret into terminal logs and
agent transcripts (DAH-2898 / B-24). The value must still reach the pod unchanged.
"""
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from lium.cli.commands.exec import exec_command


def _pod():
    # parse_targets matches on id/name/huid; print_execution reads huid.
    return SimpleNamespace(id="pod-1", name="test-pod", huid="eager-wolf-aa")


def _run(args):
    runner = CliRunner()
    with patch("lium.cli.commands.exec.Lium") as Client:
        inst = Client.return_value
        inst.ps.return_value = [_pod()]
        inst.exec.return_value = {"stdout": "", "stderr": "", "exit_code": 0, "success": True}
        result = runner.invoke(exec_command, args)
    return result, inst


def test_exec_env_values_are_masked_in_text_output():
    result, inst = _run(
        ["test-pod", "true", "-e", "SECRET=supersecret", "-e", "TOKEN=abc123"]
    )
    assert result.exit_code == 0
    # names are still shown so the caller can see what was set ...
    assert "SECRET=****" in result.output
    assert "TOKEN=****" in result.output
    # ... but the values never appear in the terminal output
    assert "supersecret" not in result.output
    assert "abc123" not in result.output
    # the unmasked values still reach the pod (SDK receives the real env)
    _, kwargs = inst.exec.call_args
    assert kwargs["env"] == {"SECRET": "supersecret", "TOKEN": "abc123"}


def test_exec_without_env_prints_no_environment_line():
    result, _ = _run(["test-pod", "true"])
    assert result.exit_code == 0
    assert "Environment:" not in result.output
