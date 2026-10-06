"""`lium ssh` / `lium up` run OpenSSH from an argument list built from the pod, with the pod's host key pinned (DAH-3215).

Properties: the argv names only the pod's user, address and port (any other `ssh_connect_cmd` is refused);
`subprocess.run` gets a list and no shell; the host-key options are the pinned ones unless LIUM_SSH_INSECURE=1.
"""

import stat
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.ssh import actions as ssh_actions
from lium.cli.ssh import command as ssh_command
from lium.cli.utils import CliFailure, EXIT_SSH_ERROR
from lium.sdk import Config, Lium, PodInfo
from lium.sdk.client import ssh_target


def _pod(ssh_cmd="ssh root@203.0.113.7 -p 20299"):
    return PodInfo(
        id="pod-123",
        name="my-pod",
        huid="swift-fox-c8",
        status="RUNNING",
        ssh_cmd=ssh_cmd,
        ports={"22": 20299},
        created_at="2026-09-08T00:00:00Z",
        updated_at="2026-09-08T00:00:00Z",
        executor=None,
        template={},
        removal_scheduled_at=None,
        jupyter_installation_status=None,
        jupyter_url=None,
    )


def _client(tmp_path, monkeypatch, key_name="id_ed25519"):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LIUM_SSH_INSECURE", raising=False)
    key_path = tmp_path / key_name
    key_path.write_text("key")
    return Lium(Config(api_key="test", ssh_key_path=key_path)), key_path


@pytest.mark.parametrize(
    "ssh_cmd, expected",
    [
        ("ssh root@203.0.113.7 -p 20299", ("root", "203.0.113.7", 20299)),
        ("ssh root@203.0.113.7", ("root", "203.0.113.7", 22)),
        ("ssh ubuntu@node-3.example.net -p 22", ("ubuntu", "node-3.example.net", 22)),
        ("ssh root@2001:db8::7 -p 2200", ("root", "2001:db8::7", 2200)),
    ],
)
def test_ssh_target_reads_the_shape_the_api_sends(ssh_cmd, expected):
    assert ssh_target(ssh_cmd) == expected


@pytest.mark.parametrize(
    "ssh_cmd",
    [
        None,
        "",
        "ssh root@203.0.113.7 -p 20299; touch /tmp/pwned",
        "ssh root@203.0.113.7 -p 20299 -o ProxyCommand=touch\\ /tmp/pwned",
        "ssh -o ProxyCommand=id root@203.0.113.7",
        "ssh root@-oProxyCommand=id -p 22",
        "ssh 'root@203.0.113.7; id'",
        "ssh root@`id` -p 22",
        "ssh root@203.0.113.7 -p None",
        "ssh root@203.0.113.7 -p 0",
        "ssh root@203.0.113.7 -p 70000",
        "ssh root@203.0.113.7 -q 20299",
        "ssh 203.0.113.7 -p 20299",
        "scp root@203.0.113.7 -p 20299",
        "ssh root@203.0.113.7 -p 20299 'unterminated",
        "ssh root@" + "a" * 254 + " -p 22",
        "ssh " + "u" * 33 + "@203.0.113.7 -p 22",
        "ssh -V@203.0.113.7 -p 22",
        "ssh root@fe80::1%eth0 -p 22",
        "ssh root@203.0.113.7 -p \u0661\u0662",
        "ssh 'root@203.0.113.7\n' -p 22",
    ],
)
def test_ssh_target_refuses_everything_but_user_host_port(ssh_cmd):
    with pytest.raises(ValueError):
        ssh_target(ssh_cmd)


def test_ssh_argv_pins_the_host_key_and_names_only_the_pod(tmp_path, monkeypatch):
    client, key_path = _client(tmp_path, monkeypatch)
    hosts_file = tmp_path / ".lium" / "known_hosts" / "pod-123"

    argv = client.ssh_argv(_pod())

    assert argv == [
        "ssh",
        "-i", str(key_path),
        "-p", "20299",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f'UserKnownHostsFile="{hosts_file}"',
        "root@203.0.113.7",
    ]
    assert hosts_file.exists()
    assert stat.S_IMODE(hosts_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(hosts_file.parent.stat().st_mode) == 0o700


def test_ssh_argv_keeps_a_home_with_a_space_as_one_known_hosts_file(tmp_path, monkeypatch):
    home = tmp_path / "First Last"
    home.mkdir()
    client, _ = _client(home, monkeypatch)

    argv = client.ssh_argv(_pod())

    hosts_file = home / ".lium" / "known_hosts" / "pod-123"
    assert argv[argv.index("StrictHostKeyChecking=accept-new") + 2] == f'UserKnownHostsFile="{hosts_file}"'
    assert hosts_file.exists()


def test_ssh_argv_insecure_env_restores_the_old_options(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("LIUM_SSH_INSECURE", "1")

    argv = client.ssh_argv(_pod())

    assert argv[-5:] == ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "root@203.0.113.7"]
    assert not (tmp_path / ".lium" / "known_hosts").exists()


def test_ssh_argv_without_a_configured_key_leaves_key_choice_to_ssh(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LIUM_SSH_INSECURE", raising=False)
    client = Lium(Config(api_key="test", ssh_key_path=None))

    argv = client.ssh_argv(_pod())

    assert "-i" not in argv
    assert argv[:3] == ["ssh", "-p", "20299"]
    assert argv[-1] == "root@203.0.113.7"


def test_ssh_argv_refuses_an_ssh_cmd_of_another_shape(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="Unexpected ssh command"):
        client.ssh_argv(_pod("ssh root@203.0.113.7 -p 20299 -o ProxyCommand=id"))


def test_ssh_string_is_the_quoted_argv(tmp_path, monkeypatch):
    (tmp_path / "my keys").mkdir()
    client, key_path = _client(tmp_path, monkeypatch, key_name="my keys/id_ed25519")

    command = client.ssh(_pod())

    assert command.startswith(f"ssh -i '{key_path}' -p 20299 -o StrictHostKeyChecking=accept-new ")
    assert command.endswith(" root@203.0.113.7")
    assert "StrictHostKeyChecking=no" not in command


def test_ssh_action_runs_the_argv_without_a_shell(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=3)

    monkeypatch.setattr(ssh_actions.subprocess, "run", fake_run)

    result = ssh_actions.SshAction().execute({"lium": client, "pod": _pod()})

    assert result.ok and result.data == {"exit_code": 3}
    (argv, kwargs), = calls
    assert isinstance(argv, list) and argv[0] == "ssh" and argv[-1] == "root@203.0.113.7"
    assert "shell" not in kwargs
    assert "StrictHostKeyChecking=no" not in argv


def test_ssh_action_refuses_a_hostile_ssh_cmd_before_running_anything(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(ssh_actions.subprocess, "run", lambda *a, **k: calls.append(a))

    result = ssh_actions.SshAction().execute(
        {"lium": client, "pod": _pod("ssh root@203.0.113.7 -p 20299; touch /tmp/pwned")}
    )

    assert not result.ok
    assert "Unexpected ssh command" in result.error
    assert calls == []


def _fake_lium_factory(client, pod):
    class _Fake:
        def __new__(cls, *args, **kwargs):
            client.ps = lambda: [pod]
            return client

    return _Fake


def test_get_ssh_method_and_pod_returns_the_argv(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    pod = _pod()
    monkeypatch.setattr(ssh_command, "Lium", _fake_lium_factory(client, pod))
    monkeypatch.setattr(ssh_command.shutil, "which", lambda name: "/usr/bin/ssh")

    argv, found = ssh_command.get_ssh_method_and_pod("my-pod")

    assert found is pod
    assert argv[0] == "ssh" and argv[-1] == "root@203.0.113.7"
    assert "StrictHostKeyChecking=no" not in argv


def test_get_ssh_method_and_pod_reports_a_hostile_ssh_cmd_as_unavailable(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    pod = _pod("ssh root@203.0.113.7 -p 20299 -o ProxyCommand=id")
    monkeypatch.setattr(ssh_command, "Lium", _fake_lium_factory(client, pod))
    monkeypatch.setattr(ssh_command.shutil, "which", lambda name: "/usr/bin/ssh")

    with pytest.raises(CliFailure) as raised:
        ssh_command.get_ssh_method_and_pod("my-pod")

    assert raised.value.code == "ssh_unavailable"
    assert raised.value.exit_code == EXIT_SSH_ERROR


def test_ssh_session_connected_runs_the_argv_without_a_shell(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ssh_command.subprocess, "run", fake_run)

    assert ssh_command.ssh_session_connected(["ssh", "-p", "20299", "root@203.0.113.7"]) is True
    (argv, kwargs), = calls
    assert argv == ["ssh", "-p", "20299", "root@203.0.113.7"]
    assert "shell" not in kwargs


def test_ssh_command_exits_4_on_an_unusable_ssh_cmd(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    pod = _pod("ssh root@203.0.113.7 -p 20299; touch /tmp/pwned")
    monkeypatch.setattr(ssh_command, "Lium", _fake_lium_factory(client, pod))
    monkeypatch.setattr(ssh_command.shutil, "which", lambda name: "/usr/bin/ssh")
    calls = []
    monkeypatch.setattr(ssh_actions.subprocess, "run", lambda *a, **k: calls.append(a))

    result = CliRunner().invoke(cli, ["ssh", "my-pod"])

    assert result.exit_code == EXIT_SSH_ERROR
    assert "Unexpected ssh command" in result.output
    assert calls == []
