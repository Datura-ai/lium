from lium.sdk import Config, Lium, PodInfo
from lium.sdk import client as sdk_client


def _pod():
    return PodInfo(
        id="pod-123",
        name="backup-test",
        huid="swift-fox-c8",
        status="RUNNING",
        ssh_cmd="ssh root@38.80.122.244 -p 20299",
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

        def connect(self, **kwargs):
            connect_calls.append(kwargs)

        def close(self):
            self.closed = True

    monkeypatch.setattr(sdk_client.paramiko, "SSHClient", FakeSSHClient)
    monkeypatch.setattr(sdk_client.paramiko, "AutoAddPolicy", lambda: object())

    client = Lium(Config(api_key="test", ssh_key_path=key_path))

    with client.ssh_connection(_pod()) as ssh_client:
        assert isinstance(ssh_client, FakeSSHClient)

    assert connect_calls == [
        {
            "hostname": "38.80.122.244",
            "port": 20299,
            "username": "root",
            "timeout": 30,
            "look_for_keys": False,
            "key_filename": str(key_path),
            "allow_agent": True,
        }
    ]
