"""Environment names are checked before anything is sent; exec_all failures name the pod.

The quoting itself is DAH-2984 (``Lium._env_exports``; ``exec()`` sends the
block over stdin, ``stream_exec`` keeps it inline through ``_prep_command``).
This module covers what DAH-2894 adds on top: ``_env_exports`` refuses a name
``export`` would reject on the pod, ``exec()`` refuses it before the ssh
session is opened (nothing is sent, the command does not run), the CLI refuses
it before building the SDK client, the inline form still round-trips values
through a real ``sh``, and ``exec_all`` reports a failed pod in the same shape
as a successful one.
"""

import shlex
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.commands import exec as exec_module
from lium.cli.utils import EXIT_CONFIGURATION_ERROR
from lium.sdk import Lium
from test_exec_env_stdin import LocalShellSSHClient


def _client() -> Lium:
    return Lium.__new__(Lium)  # no config, no network: _prep_command is pure


# --- _prep_command -------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "plain",
    "has spaces",
    'say "hi"',
    "it's",
    "$HOME and ${USER}",
    "`whoami`",
    "$(id)",
    "a;b && c | d",
    '{"key": "value", "n": 1}',
    "",
])
def test_values_survive_the_shell_unchanged(value):
    prepared = _client()._prep_command("printenv V", env={"V": value})

    # Run the exported prefix through a real shell and read the variable back.
    out = subprocess.run(["sh", "-c", prepared], capture_output=True, text=True, check=True)
    assert out.stdout == value + "\n"


def test_prefix_is_exports_joined_before_the_command():
    prepared = _client()._prep_command("python train.py", env={"A": "1", "B": "two words"})

    assert prepared == f"export A=1 && export B={shlex.quote('two words')} && python train.py"


def test_no_env_leaves_the_command_alone():
    assert _client()._prep_command("ls", env=None) == "ls"
    assert _client()._prep_command("ls", env={}) == "ls"


INVALID_NAMES = ["1ABC", "A-B", "A B", "A=B", "", "$X", "FOO\n"]


@pytest.mark.parametrize("name", INVALID_NAMES)
def test_prep_command_rejects_invalid_names(name):
    with pytest.raises(ValueError, match="Invalid environment variable name"):
        _client()._prep_command("ls", env={name: "x"})


def test_non_string_values_are_stringified():
    assert _client()._prep_command("ls", env={"N": 3}) == "export N=3 && ls"


# --- exec(): the check runs before the session is opened ---------------------------------------

@pytest.fixture
def ssh(monkeypatch):
    """#167's stand-in for paramiko: the "remote" command runs in a local ``sh``."""
    fake = LocalShellSSHClient()

    @contextmanager
    def fake_connection(self, pod, timeout=30):
        fake.opened = True
        yield fake

    fake.opened = False
    monkeypatch.setattr(Lium, "ssh_connection", fake_connection)
    return fake


@pytest.mark.parametrize("name", INVALID_NAMES)
def test_exec_rejects_invalid_names_before_anything_is_sent(ssh, tmp_path, name):
    marker = tmp_path / "started"
    pod = SimpleNamespace(id="pod-1", name="my-pod", huid="eager-wolf-aa")

    with pytest.raises(ValueError, match="Invalid environment variable name"):
        _client().exec(pod, command=f"echo started > {shlex.quote(str(marker))}", env={name: "x"})

    # Fails on the previous shape: the command had been sent (and had run with no
    # environment) before the name check raised.
    assert ssh.commands == []
    assert ssh.opened is False
    assert not marker.exists()


def test_exec_with_valid_names_still_sends_the_command_once(ssh, tmp_path):
    marker = tmp_path / "started"
    pod = SimpleNamespace(id="pod-1", name="my-pod", huid="eager-wolf-aa")

    result = _client().exec(pod, command=f"printenv V > {shlex.quote(str(marker))}", env={"V": "one two"})

    assert result["success"] is True
    assert len(ssh.commands) == 1
    assert marker.read_text() == "one two\n"


# --- exec_all ------------------------------------------------------------------------------

def test_exec_all_failure_entries_carry_the_pod_id_like_successes(monkeypatch):
    client = _client()
    good = SimpleNamespace(id="pod-good")
    bad = SimpleNamespace(id="pod-bad")

    def fake_exec(pod, *, command, env=None):
        if pod is bad:
            raise OSError("ssh: connect to host: Connection refused")
        return {"stdout": "ok\n", "stderr": "", "exit_code": 0, "success": True}

    monkeypatch.setattr(client, "exec", fake_exec)

    results = client.exec_all([good, bad], command="true")

    by_pod = {r["pod"]: r for r in results}
    assert set(by_pod) == {"pod-good", "pod-bad"}
    assert by_pod["pod-good"]["success"] is True
    assert by_pod["pod-bad"] == {
        "pod": "pod-bad",
        "error": "ssh: connect to host: Connection refused",
        "success": False,
    }


def test_exec_all_rejects_an_invalid_name_before_any_pod_is_contacted(monkeypatch):
    client = _client()
    touched = []

    def fake_exec(pod, *, command, env=None):
        touched.append(pod.id)
        return {"stdout": "", "stderr": "", "exit_code": 0, "success": True}

    monkeypatch.setattr(client, "exec", fake_exec)

    # Fails on the previous shape: exec_all caught the ValueError from every pod's
    # exec() and returned one {"pod": ..., "error": "Invalid environment variable
    # name ...", "success": False} entry per pod instead of raising.
    with pytest.raises(ValueError, match="Invalid environment variable name"):
        client.exec_all([SimpleNamespace(id="pod-1"), SimpleNamespace(id="pod-2")], command="true", env={"1BAD": "x"})

    assert touched == []


# --- lium exec -e ----------------------------------------------------------------------------

def test_cli_rejects_an_invalid_env_name_with_a_configuration_error(monkeypatch):
    class _Lium:
        def __init__(self, *a, **k):
            raise AssertionError("must fail before touching the SDK")

    monkeypatch.setattr(exec_module, "Lium", _Lium)

    result = CliRunner().invoke(cli, ["exec", "my-pod", "-e", "1BAD=x", "echo hi"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert "Invalid env name '1BAD'" in result.output


def test_cli_passes_values_with_shell_characters_through_untouched(monkeypatch):
    seen = {}

    class _Lium:
        def __init__(self, *a, **k):
            pass

        def ps(self):
            return [SimpleNamespace(id="pod-uuid-1", huid="eager-wolf-aa", name="my-pod")]

        def exec(self, pod, command=None, env=None):
            seen.update(env)
            return {"stdout": "", "stderr": "", "exit_code": 0, "success": True}

    monkeypatch.setattr(exec_module, "Lium", _Lium)

    result = CliRunner().invoke(
        cli, ["exec", "my-pod", "-e", "TOKEN=abc$def`x`", "-e", "JSON={\"a\": 1}", "echo hi"]
    )

    assert result.exit_code == 0, result.output
    assert seen == {"TOKEN": "abc$def`x`", "JSON": '{"a": 1}'}
