"""`lium exec --detach`: start a long job on a pod and come back at once.

Plain `exec` holds the SSH session open until every child of the command has
exited, so a training run started with `nohup ... &` still blocks the caller
unless it also uses `setsid` and closes stdin. Callers ended up wrapping every
command by hand; the CLI can do it once, correctly.
"""

import base64
import json
import shlex
import subprocess
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.commands import exec as exec_module
from lium.cli.commands.exec import (
    DetachedExecution,
    build_detached_script_command,
    parse_detached_pid,
)
from lium.cli.utils import EXIT_CONFIGURATION_ERROR, EXIT_GENERAL_ERROR
from lium.sdk import Lium
from lium.sdk.detach import build_detached_command, default_detach_log_path, detach_token

TOKEN = "20260101T120000Z-abc123"
LOG = f"/workspace/logs/exec-{TOKEN}.log"
# what Lium.login_shell_env puts in front of the command inside the detached login shell
PRELUDE = 'eval "$LIUM_JOB_ENV" || exit 1; unset LIUM_JOB_ENV; '


def _pod(huid: str = "eager-wolf-aa", name: str = "my-pod") -> SimpleNamespace:
    return SimpleNamespace(id=f"pod-{huid}", huid=huid, name=name)


class _FakeLium:
    """Records the command lines sent to each pod and answers with a PID."""

    pods: list = []
    stdout: str = "4242\n"
    stderr: str = ""
    exit_code: int = 0
    sent: list[tuple[str, str]] = []
    envs: list = []  # the env= each exec() call received, in order

    def __init__(self, *args, **kwargs):
        pass

    def ps(self):
        return list(self.pods)

    login_shell_env = Lium.login_shell_env

    def exec(self, pod, command=None, env=None):
        _FakeLium.sent.append((pod.huid, command))
        _FakeLium.envs.append(env)
        return {
            "success": self.exit_code == 0,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }

    def exec_all(self, pods, command=None, env=None):
        return [self.exec(pod, command=command, env=env) for pod in pods]


@pytest.fixture
def fake_lium(monkeypatch):
    _FakeLium.pods = [_pod()]
    _FakeLium.stdout = "4242\n"
    _FakeLium.stderr = ""
    _FakeLium.exit_code = 0
    _FakeLium.sent = []
    _FakeLium.envs = []
    monkeypatch.setattr(exec_module, "Lium", _FakeLium)
    monkeypatch.setattr(exec_module, "detach_token", lambda now=None: TOKEN)
    return _FakeLium


def _run(args):
    return CliRunner().invoke(cli, ["exec", *args])


def test_detached_command_uses_nohup_setsid_and_closes_stdin():
    remote = build_detached_command("python train.py --epochs 3", "/workspace/logs/run.log")

    assert "mkdir -p /workspace/logs || exit 1; " in remote
    assert "nohup setsid bash -lc 'python train.py --epochs 3'" in remote
    assert remote.endswith("> /workspace/logs/run.log 2>&1 < /dev/null & echo $!")


def test_detached_command_checks_for_setsid_and_bash_before_forking():
    """`echo $!` prints a PID as soon as the shell forks, so a missing setsid would
    otherwise report "Started" with the failure buried in the log."""
    remote = build_detached_command("true", "/workspace/logs/run.log")

    assert remote.index("command -v") < remote.index("mkdir -p") < remote.index("nohup")
    assert "setsid" in remote[: remote.index("mkdir -p")]
    assert "bash" in remote[: remote.index("mkdir -p")]


def test_a_missing_setsid_prints_no_pid_and_says_why(tmp_path):
    """Run the launcher for real with nothing on PATH: stdout must stay empty."""
    remote = build_detached_command("true", str(tmp_path / "run.log"))

    result = subprocess.run(["/bin/sh", "-c", remote], capture_output=True, text=True, env={"PATH": str(tmp_path)})

    assert result.stdout == ""
    assert parse_detached_pid(result.stdout) is None
    assert "setsid not found on the pod" in result.stderr
    assert result.returncode == 127


def test_detached_command_quotes_the_users_command_as_one_argument():
    """Quotes inside the command must survive the trip through bash -lc."""
    command = "echo \"it's\" done; sleep 1"

    remote = build_detached_command(command, "/workspace/logs/run.log")

    assert f"bash -lc {shlex.quote(command)}" in remote


def test_detached_command_quotes_the_log_path():
    remote = build_detached_command("true", "/workspace/my logs/run.log")

    assert "mkdir -p '/workspace/my logs'" in remote
    assert "> '/workspace/my logs/run.log'" in remote


def test_default_log_path_is_under_workspace_logs():
    token = detach_token(now=1767268800)  # 2026-01-01T12:00:00Z

    assert token.startswith("20260101T120000Z-")
    assert len(token) == len(TOKEN)
    assert default_detach_log_path(token) == f"/workspace/logs/exec-{token}.log"


def test_two_launches_in_the_same_second_get_different_logs():
    a, b = detach_token(now=1767268800), detach_token(now=1767268800)

    assert a != b


def test_detached_script_is_copied_before_it_is_started():
    """The script must exist on the pod as a file before anything runs from it."""
    script = "#!/bin/bash\necho 'hello' \"world\" $1\n"

    remote = build_detached_script_command(script, "/workspace/logs/run.log", TOKEN)

    encoded = base64.b64encode(script.encode()).decode()
    upload = f"printf %s {encoded} | base64 -d > /tmp/lium-exec-{TOKEN}.sh"
    assert remote.startswith(upload)
    assert remote.index(f"chmod +x /tmp/lium-exec-{TOKEN}.sh") < remote.index("nohup")
    assert f"bash -lc /tmp/lium-exec-{TOKEN}.sh" in remote


@pytest.mark.parametrize(
    "stdout, expected",
    [("4242\n", 4242), ("nohup: ignoring input\n4242\n", 4242), ("", None), ("bash: setsid: not found\n", None)],
)
def test_parse_detached_pid(stdout, expected):
    assert parse_detached_pid(stdout) == expected


def test_detach_prints_pid_and_log_and_exits_zero(fake_lium):
    result = _run(["my-pod", "-d", "python train.py"])

    assert result.exit_code == 0, result.output
    assert "PID 4242" in result.output
    assert LOG in result.output
    # Rich wraps long lines at 80 columns under CliRunner; compare on one line.
    assert f'follow with: lium exec eager-wolf-aa "tail -n 200 {LOG}"' in " ".join(result.output.split())
    [(huid, remote)] = fake_lium.sent
    assert huid == "eager-wolf-aa"
    assert remote == build_detached_command("python train.py", LOG)


def test_detach_env_is_applied_inside_the_login_shell_and_never_in_the_line(fake_lium):
    """`-e` with `-d`: the value goes to exec(env=) as one variable (stdin, not argv)
    and the detached `bash -lc` re-applies it after its profile, so the given value wins."""
    result = _run(["my-pod", "-d", "-e", "HF_HOME=/workspace/hf", "python train.py"])

    assert result.exit_code == 0, result.output
    [(_, remote)] = fake_lium.sent
    assert "/workspace/hf" not in remote
    assert remote == build_detached_command(PRELUDE + "python train.py", LOG)
    assert fake_lium.envs == [{"LIUM_JOB_ENV": "export HF_HOME=/workspace/hf"}]


def test_detach_honours_an_explicit_log_path(fake_lium):
    result = _run(["my-pod", "--detach", "--log", "/workspace/train.log", "python train.py"])

    assert result.exit_code == 0, result.output
    assert "/workspace/train.log" in result.output
    [(_, remote)] = fake_lium.sent
    assert "> /workspace/train.log 2>&1" in remote
    assert "mkdir -p /workspace" in remote


def test_detach_json_carries_pid_and_log(fake_lium):
    result = _run(["my-pod", "-d", "python train.py", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == {
        "ok": True,
        "results": [{
            "pod": "eager-wolf-aa",
            "pid": 4242,
            "log": LOG,
            "error": None,
        }],
    }


def test_detach_with_script_uploads_then_starts_it(fake_lium, tmp_path):
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/bash\necho from-script\n")

    result = _run(["my-pod", "-d", "--script", str(script)])

    assert result.exit_code == 0, result.output
    [(_, remote)] = fake_lium.sent
    assert remote == build_detached_script_command("#!/bin/bash\necho from-script\n", LOG, TOKEN)
    assert fake_lium.envs == [None]


def test_detach_with_script_and_env_applies_the_exports_before_the_script(fake_lium, tmp_path):
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/bash\necho $HF_HOME\n")

    result = _run(["my-pod", "-d", "-e", "HF_HOME=/workspace/hf", "--script", str(script)])

    assert result.exit_code == 0, result.output
    [(_, remote)] = fake_lium.sent
    assert "/workspace/hf" not in remote
    assert remote == build_detached_script_command("#!/bin/bash\necho $HF_HOME\n", LOG, TOKEN, prelude=PRELUDE)
    assert f"bash -lc '{PRELUDE}/tmp/lium-exec-{TOKEN}.sh'" in remote
    assert fake_lium.envs == [{"LIUM_JOB_ENV": "export HF_HOME=/workspace/hf"}]


def test_detach_fails_when_no_pid_came_back(fake_lium):
    """A launcher that printed nothing did not start the job; that is not success."""
    fake_lium.stdout = ""
    fake_lium.stderr = "bash: setsid: command not found\n"
    fake_lium.exit_code = 127

    result = _run(["my-pod", "-d", "python train.py"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "setsid: command not found" in result.output


def test_detach_json_reports_a_failed_start(fake_lium):
    fake_lium.stdout = ""
    fake_lium.exit_code = 1

    result = _run(["my-pod", "-d", "python train.py", "--json"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["results"][0]["pid"] is None
    assert payload["results"][0]["error"]


def test_detach_on_several_pods_starts_on_each(fake_lium):
    fake_lium.pods = [_pod("eager-wolf-aa", "a"), _pod("brave-fox-3a", "b")]

    result = _run(["all", "-d", "python train.py"])

    assert result.exit_code == 0, result.output
    assert [huid for huid, _ in fake_lium.sent] == ["eager-wolf-aa", "brave-fox-3a"]
    assert result.output.count("PID 4242") == 2


def test_log_without_detach_is_rejected(fake_lium):
    result = _run(["my-pod", "--log", "/workspace/x.log", "echo hi"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert fake_lium.sent == []


def test_plain_exec_is_unchanged(fake_lium):
    """Without --detach the command goes to the pod exactly as typed."""
    fake_lium.stdout = "ok\n"

    result = _run(["my-pod", "echo hi"])

    assert result.exit_code == 0, result.output
    assert fake_lium.sent == [("eager-wolf-aa", "echo hi")]


def test_detached_execution_uses_stderr_as_the_error_when_there_is_no_pid():
    execution = DetachedExecution.from_sdk_result(
        _pod(), {"stdout": "", "stderr": "boom\n", "exit_code": 1}, "/workspace/logs/x.log"
    )

    assert execution.succeeded is False
    assert execution.error == "boom"
