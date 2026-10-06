"""`Lium.exec(env=...)` keeps environment values out of the pod's argv.

The exports used to be spelled out in the remote command
(``export KEY="value" && cmd``), so every ``-e`` value sat in the argv of the
shell running the job and was readable by any process on the pod with
``ps -o args`` for as long as the job ran (DAH-2984). They now travel over the
ssh session's stdin and are evaluated by the remote shell.
"""

import subprocess
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from lium.sdk import Lium


class _ProcessFile:
    """Just enough of a paramiko ChannelFile for exec(): write/close/read/channel."""

    def __init__(self, proc, stream):
        self.proc = proc
        self.stream = stream
        self.written = b""
        self.channel = self

    def write(self, data):
        self.written += data
        self.stream.write(data)

    def close(self):
        self.stream.close()

    def read(self):
        return self.stream.read()

    def recv_exit_status(self):
        return self.proc.wait()


class LocalShellSSHClient:
    """Stands in for paramiko: runs the "remote" command in a local sh, with stdin
    connected to whatever exec() writes, so the round trip is real."""

    def __init__(self):
        self.commands = []
        self.stdin = None

    def exec_command(self, command):
        self.commands.append(command)
        proc = subprocess.Popen(
            ["sh", "-c", command],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.stdin = _ProcessFile(proc, proc.stdin)
        return self.stdin, _ProcessFile(proc, proc.stdout), _ProcessFile(proc, proc.stderr)


@pytest.fixture
def ssh(monkeypatch):
    fake = LocalShellSSHClient()

    @contextmanager
    def fake_connection(self, pod, timeout=30):
        yield fake

    monkeypatch.setattr(Lium, "ssh_connection", fake_connection)
    return fake


def _client() -> Lium:
    return Lium.__new__(Lium)  # no config, no network: exec() only needs ssh_connection


_POD = SimpleNamespace(id="pod-1", name="my-pod", huid="eager-wolf-aa")


def test_remote_command_names_no_value_and_stdin_carries_it(ssh):
    result = _client().exec(_POD, command="printenv SECRET", env={"SECRET": "canary-9f3a"})

    assert result["success"], result
    [command] = ssh.commands
    assert "canary-9f3a" not in command
    assert command == 'eval "$(cat)" && printenv SECRET'
    assert b"canary-9f3a" in ssh.stdin.written
    assert result["stdout"] == "canary-9f3a\n"


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
    "line one\nline two\n",
    "",
])
def test_values_round_trip_unchanged(ssh, value):
    result = _client().exec(_POD, command="printenv V", env={"V": value})

    assert result["stdout"] == value + "\n"
    assert value == "" or value not in ssh.commands[0]


def test_several_variables_all_arrive(ssh):
    result = _client().exec(
        _POD, command='echo "$A|$B|$C"', env={"A": "1", "B": "two words", "C": "x=y"}
    )

    assert result["stdout"] == "1|two words|x=y\n"


def test_child_processes_inherit_the_value_without_it_in_any_argv(ssh):
    # what a detached launcher (`nohup setsid sh -c '<cmd>'`) relies on: the
    # launching shell exports over stdin, the child inherits the environment,
    # and no process — parent or child — carries the value in its argv
    result = _client().exec(
        _POD,
        command="sh -c 'printenv SECRET; ps -o args= -p $$ -p $PPID'",
        env={"SECRET": "canary-7c21"},
    )

    assert result["success"], result
    value, *argvs = result["stdout"].splitlines()
    assert value == "canary-7c21"
    assert argvs and all("canary-7c21" not in argv for argv in argvs)
    assert "canary-7c21" not in ssh.commands[0]


def test_no_env_leaves_the_command_and_stdin_alone(ssh):
    result = _client().exec(_POD, command="echo hi")

    assert ssh.commands == ["echo hi"]
    assert ssh.stdin.written == b""
    assert result["stdout"] == "hi\n"


def test_inline_form_for_stream_exec_quotes_the_value():
    prepared = _client()._prep_command("ls", env={"V": "a b", "W": "$HOME"})

    assert prepared == "export V='a b' && export W='$HOME' && ls"
