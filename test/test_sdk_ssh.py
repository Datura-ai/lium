import warnings
from types import SimpleNamespace

import pytest

from lium.sdk import Config, Lium, LiumHostKeyError, PodInfo
from lium.sdk import client as sdk_client


def _pod():
    return PodInfo(
        id="pod-123",
        name="backup-test",
        huid="swift-fox-c8",
        status="RUNNING",
        ssh_cmd="ssh root@203.0.113.10 -p 20299",
        ports={},
        created_at="2026-05-14T00:00:00Z",
        updated_at="2026-05-14T00:00:00Z",
        executor=None,
        template={},
        removal_scheduled_at=None,
        jupyter_installation_status=None,
        jupyter_url=None,
    )


def test_ssh_connection_falls_back_to_agent_when_key_file_cannot_be_loaded(monkeypatch, tmp_path):
    key_path = tmp_path / "id_ed25519"
    key_path.write_text("encrypted-key")
    connect_calls = []

    def raise_ssh_exception(*args, **kwargs):
        raise sdk_client.paramiko.SSHException("encrypted key")

    monkeypatch.setattr(sdk_client.paramiko.Ed25519Key, "from_private_key_file", raise_ssh_exception)
    monkeypatch.setattr(sdk_client.paramiko.RSAKey, "from_private_key_file", raise_ssh_exception)
    monkeypatch.setattr(sdk_client.paramiko.ECDSAKey, "from_private_key_file", raise_ssh_exception)

    class FakeSSHClient:
        def set_missing_host_key_policy(self, policy):
            self.policy = policy

        def load_host_keys(self, filename):
            self.host_keys_file = filename

        def connect(self, **kwargs):
            connect_calls.append(kwargs)

        def close(self):
            self.closed = True

    monkeypatch.setattr(sdk_client.paramiko, "SSHClient", FakeSSHClient)
    monkeypatch.setenv("HOME", str(tmp_path))

    client = Lium(Config(api_key="test", ssh_key_path=key_path))

    with client.ssh_connection(_pod()) as ssh_client:
        assert isinstance(ssh_client, FakeSSHClient)

    assert connect_calls == [
        {
            "hostname": "203.0.113.10",
            "port": 20299,
            "username": "root",
            "timeout": 30,
            "look_for_keys": False,
            "key_filename": str(key_path),
            "allow_agent": True,
        }
    ]


class _PinningSSHClient(sdk_client.paramiko.SSHClient):
    """SSHClient whose connect() replays paramiko's host-key check against a fake server key."""

    server_key = None
    connects = 0

    def connect(self, hostname, port, **kwargs):
        type(self).connects += 1
        lookup = f"[{hostname}]:{port}"
        known = self._host_keys.lookup(lookup)
        key = type(self).server_key
        if known is None:
            self._policy.missing_host_key(self, lookup, key)
        elif known.get(key.get_name()) != key:
            raise sdk_client.paramiko.BadHostKeyException(lookup, key, known[key.get_name()])


def _install_pinning_client(monkeypatch, tmp_path, server_key):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LIUM_SSH_INSECURE", raising=False)
    monkeypatch.setattr(sdk_client.paramiko.Ed25519Key, "from_private_key_file", lambda *a, **k: object())
    monkeypatch.setattr(sdk_client.paramiko, "SSHClient", _PinningSSHClient)
    _PinningSSHClient.server_key = server_key
    _PinningSSHClient.connects = 0
    key_path = tmp_path / "id_ed25519"
    key_path.write_text("key")
    return Lium(Config(api_key="test", ssh_key_path=key_path))


def test_ssh_connection_pins_host_key_on_first_use_and_accepts_it_afterwards(monkeypatch, tmp_path):
    server_key = sdk_client.paramiko.ECDSAKey.generate()
    client = _install_pinning_client(monkeypatch, tmp_path, server_key)
    pod = _pod()
    hosts_file = tmp_path / ".lium" / "known_hosts" / "pod-123"

    with pytest.warns(UserWarning, match="Pinning ecdsa-sha2-nistp256 host key"):
        with client.ssh_connection(pod):
            pass

    assert hosts_file.exists()
    assert (hosts_file.stat().st_mode & 0o777) == 0o600
    saved = sdk_client.paramiko.HostKeys(str(hosts_file)).lookup(f"[{pod.host}]:{pod.ssh_port}")
    assert saved["ecdsa-sha2-nistp256"] == server_key

    # Second connection to the same pod with the same key: no warning, no change.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with client.ssh_connection(pod):
            pass
    assert _PinningSSHClient.connects == 2


def test_ssh_connection_rejects_changed_host_key(monkeypatch, tmp_path):
    server_key = sdk_client.paramiko.ECDSAKey.generate()
    client = _install_pinning_client(monkeypatch, tmp_path, server_key)
    pod = _pod()
    with pytest.warns(UserWarning):
        with client.ssh_connection(pod):
            pass

    new_key = sdk_client.paramiko.ECDSAKey.generate()
    _PinningSSHClient.server_key = new_key
    with pytest.raises(LiumHostKeyError, match="Host key for pod backup-test .* changed") as exc:
        with client.ssh_connection(pod):
            pass
    assert "known_hosts" in str(exc.value)
    assert "LIUM_SSH_INSECURE=1" in str(exc.value)
    # the platform reboots pods on its own too (tasks/pod.py retries failed pods), so the text says so
    assert "platform restarting the pod on its own" in str(exc.value)
    # fingerprints in the form `ssh-keygen -lf` prints, so the user can compare them
    assert sdk_client.host_key_fingerprint(new_key) in str(exc.value)
    assert sdk_client.host_key_fingerprint(server_key) in str(exc.value)


def test_host_key_fingerprint_matches_ssh_keygen(tmp_path):
    import shutil
    import subprocess

    key = sdk_client.paramiko.ECDSAKey.generate()
    fp = sdk_client.host_key_fingerprint(key)
    assert fp.startswith("SHA256:") and "=" not in fp
    if shutil.which("ssh-keygen"):
        pub = tmp_path / "k.pub"
        pub.write_text(f"{key.get_name()} {key.get_base64()}\n")
        out = subprocess.run(["ssh-keygen", "-lf", str(pub)], capture_output=True, text=True, check=True).stdout
        assert fp in out


def test_ssh_connection_known_hosts_are_scoped_per_pod(monkeypatch, tmp_path):
    """A new pod on a recycled host:port must not be reported as a changed key."""
    client = _install_pinning_client(monkeypatch, tmp_path, sdk_client.paramiko.ECDSAKey.generate())
    with pytest.warns(UserWarning):
        with client.ssh_connection(_pod()):
            pass

    other = _pod()
    other.id = "pod-456"
    _PinningSSHClient.server_key = sdk_client.paramiko.ECDSAKey.generate()
    with pytest.warns(UserWarning):
        with client.ssh_connection(other):
            pass
    assert sorted(p.name for p in (tmp_path / ".lium" / "known_hosts").iterdir()) == ["pod-123", "pod-456"]


def test_known_hosts_path_sanitises_pod_id(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    pod = _pod()
    pod.id = "../etc/passwd"
    assert sdk_client.known_hosts_path(pod) == tmp_path / ".lium" / "known_hosts" / ".._etc_passwd"


def test_ssh_insecure_env_installs_the_accept_any_key_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LIUM_SSH_INSECURE", "1")
    monkeypatch.setattr(sdk_client.paramiko.Ed25519Key, "from_private_key_file", lambda *a, **k: object())
    seen = {}

    class FakeSSHClient:
        def set_missing_host_key_policy(self, policy):
            seen["policy"] = policy

        def load_host_keys(self, filename):
            seen["loaded"] = filename

        def connect(self, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(sdk_client.paramiko, "SSHClient", FakeSSHClient)
    key_path = tmp_path / "id_ed25519"
    key_path.write_text("key")
    client = Lium(Config(api_key="test", ssh_key_path=key_path))
    with client.ssh_connection(_pod()):
        pass

    assert isinstance(seen["policy"], sdk_client._InsecureAcceptPolicy)
    assert isinstance(seen["policy"], sdk_client.paramiko.MissingHostKeyPolicy)
    assert "loaded" not in seen
    assert not (tmp_path / ".lium" / "known_hosts").exists()


def test_insecure_policy_accepts_the_key_for_the_session_and_warns(monkeypatch, tmp_path):
    """Same effect as paramiko's AutoAddPolicy, but the acceptance is named, not silent."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh_client = sdk_client.paramiko.SSHClient()
    key = sdk_client.paramiko.ECDSAKey.generate()
    fp = sdk_client.host_key_fingerprint(key)

    with pytest.warns(UserWarning) as record:
        sdk_client._InsecureAcceptPolicy().missing_host_key(ssh_client, "[203.0.113.7]:20299", key)

    message = str(record[0].message)
    assert "[203.0.113.7]:20299" in message
    assert fp in message
    assert "LIUM_SSH_INSECURE=1 disabled host key verification" in message
    # The key is trusted for this client only: paramiko will not raise on connect ...
    assert ssh_client.get_host_keys().lookup("[203.0.113.7]:20299")["ecdsa-sha2-nistp256"] == key
    # ... and nothing is pinned on disk.
    assert not (tmp_path / ".lium").exists()


def test_rsync_uses_pinned_known_hosts_unless_insecure(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LIUM_SSH_INSECURE", raising=False)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(sdk_client.subprocess, "run", fake_run)
    key_path = tmp_path / "id_ed25519"
    key_path.write_text("key")
    client = Lium(Config(api_key="test", ssh_key_path=key_path))
    pod = _pod()

    client.rsync(pod, local="./out", remote="/workspace/out")
    ssh_opt = calls[0][calls[0].index("-e") + 1]  # the rsync options (DAH-2888) come before -e
    hosts_file = tmp_path / ".lium" / "known_hosts" / "pod-123"
    assert "-o StrictHostKeyChecking=accept-new" in ssh_opt
    assert f"-o 'UserKnownHostsFile=\"{hosts_file}\"'" in ssh_opt
    assert "StrictHostKeyChecking=no" not in ssh_opt
    assert hosts_file.exists()

    monkeypatch.setenv("LIUM_SSH_INSECURE", "1")
    client.rsync(pod, local="./out", remote="/workspace/out")
    assert "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null" in calls[1][calls[1].index("-e") + 1]


def test_rsync_refuses_an_ssh_cmd_of_another_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(sdk_client.subprocess, "run", fake_run)
    key_path = tmp_path / "id_ed25519"
    key_path.write_text("key")
    client = Lium(Config(api_key="test", ssh_key_path=key_path))
    pod = _pod()
    pod.ssh_cmd = "ssh root@203.0.113.7 -p 20299 -o ProxyCommand=id"

    with pytest.raises(ValueError, match="Unexpected ssh command"):
        client.rsync(pod, local="./out", remote="/workspace/out")
    assert calls == []
    assert not (tmp_path / ".lium").exists()


@pytest.mark.parametrize("method, verb, path", [
    ("reboot", "POST", "/pods/pod-123/reboot"),
    ("down", "DELETE", "/pods/pod-123"),
])
def test_reboot_and_down_forget_the_pinned_host_key(monkeypatch, tmp_path, method, verb, path):
    monkeypatch.setenv("HOME", str(tmp_path))
    pod = _pod()
    hosts_file = sdk_client.known_hosts_path(pod)
    hosts_file.parent.mkdir(parents=True)
    hosts_file.write_text("[host]:1 ssh-ed25519 AAAA\n")
    seen = []

    class Resp:
        def json(self):
            return {"ok": True}

    def fake_request(self, m, p, **kwargs):
        seen.append((m, p))
        return Resp()

    monkeypatch.setattr(Lium, "_request", fake_request)
    client = Lium(Config(api_key="test"))
    assert getattr(client, method)(pod) == {"ok": True}
    assert seen == [(verb, path)]
    assert not hosts_file.exists()
    getattr(client, method)(pod)  # idempotent when there is nothing to forget


def test_edit_forgets_the_pinned_host_key(monkeypatch, tmp_path):
    # PUT /templates/{id} on a pod's template is routed by the backend into reboot_rental_container
    # (pod_service.edit_pod): the container, and its host key, are replaced — the pin must go with them
    monkeypatch.setenv("HOME", str(tmp_path))
    pod = _pod()
    hosts_file = sdk_client.known_hosts_path(pod)
    hosts_file.parent.mkdir(parents=True)
    hosts_file.write_text("[host]:1 ssh-ed25519 AAAA\n")
    seen = []

    class Resp:
        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def fake_request(self, m, p, **kwargs):
        seen.append((m, p, kwargs.get("json")))
        if m == "GET":
            return Resp({"id": pod.id, "pod_name": pod.name, "template": {"id": "tpl-1", "docker_image": "a"}})
        return Resp({"id": "tpl-1", "docker_image": "a", "startup_commands": "python main.py"})

    monkeypatch.setattr(Lium, "_request", fake_request)
    client = Lium(Config(api_key="test"))
    result = client.edit(pod.id, startup_commands="python main.py")

    assert result["startup_commands"] == "python main.py"
    assert seen == [
        ("GET", f"/pods/{pod.id}", None),
        ("PUT", "/templates/tpl-1", {"id": "tpl-1", "docker_image": "a", "startup_commands": "python main.py"}),
    ]
    assert not hosts_file.exists()


def test_ssh_session_reuses_one_connection_for_every_operation_inside(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LIUM_SSH_INSECURE", "1")  # no known_hosts file to load in this fake
    key_path = tmp_path / "id_ed25519"
    key_path.write_text("key")
    monkeypatch.setattr(sdk_client.paramiko.Ed25519Key, "from_private_key_file", lambda p: object())
    connects, closes = [], []

    class FakeSSHClient:
        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            connects.append(kwargs["hostname"])

        def close(self):
            closes.append(True)

    monkeypatch.setattr(sdk_client.paramiko, "SSHClient", FakeSSHClient)
    client = Lium(Config(api_key="test", ssh_key_path=key_path))
    pod = _pod()

    with client.ssh_session(pod) as held:
        with client.ssh_connection(pod) as a, client.ssh_connection(pod) as b:
            assert a is held and b is held
        assert connects == ["203.0.113.10"] and closes == []
    assert closes == [True]                       # closed once, when the session ends
    assert client._ssh_sessions == {}

    with client.ssh_connection(pod):              # outside a session: a fresh connection again
        pass
    assert connects == ["203.0.113.10", "203.0.113.10"] and closes == [True, True]
