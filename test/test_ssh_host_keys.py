"""DAH-2566: the ssh command a caller copies from the SDK or the JSON views must survive a pod restart.

The API's `ssh_connect_cmd` carries no host-key options, so anyone who runs it as
returned gets the pod's key pinned in `~/.ssh/known_hosts` and, after the first
restart, "REMOTE HOST IDENTIFICATION HAS CHANGED". DAH-2904 pins pod host keys per
pod under `~/.lium/known_hosts/<pod-id>` (dropped on reboot/edit/switch_template/rm)
and DAH-3215 makes `lium ssh` / `Lium.ssh()` use that file. This slice adds the two
things left: the same command in `lium ps --format json` / `lium describe --json`
(`ssh_command`, no key, no pin file written), and `refresh=True` / `refresh_pod()` for a caller whose
`PodInfo` predates the restart (host and port may have moved).
"""

import shlex

import pytest

from lium.cli.describe import display as describe_display
from lium.cli.ps import display as ps_display
from lium.sdk import Config, Lium, LiumNotFoundError, PodInfo, pod_ssh_command
from lium.sdk.client import openssh_host_key_options

RAW = "ssh user@pod.example -p 40499"


def _pod(ssh_cmd: str | None = RAW, pod_id: str = "pod-1") -> PodInfo:
    return PodInfo(
        id=pod_id,
        name="train",
        status="RUNNING",
        huid="eager-wolf-aa",
        ssh_cmd=ssh_cmd,
        ports={"22": 40499},
        created_at="2026-09-05T10:00:00Z",
        updated_at="2026-09-05T10:00:00Z",
        executor=None,
        template={},
        removal_scheduled_at=None,
        jupyter_installation_status=None,
        jupyter_url=None,
    )


@pytest.fixture
def home(monkeypatch, tmp_path):
    """The pinned-key files live under $HOME; keep the tests out of the real one."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LIUM_SSH_INSECURE", raising=False)
    return tmp_path


def _options(pod: PodInfo) -> str:
    """The DAH-3215 options, as they read once shell-quoted."""
    return shlex.join(openssh_host_key_options(pod))


# --- pod_ssh_command: the command the JSON views print ------------------------------------------


def test_pod_ssh_command_is_the_pinned_form_of_the_api_command_without_a_key(home):
    pod = _pod()

    assert pod_ssh_command(pod) == f"ssh -p 40499 {_options(pod)} user@pod.example"
    assert "-i " not in pod_ssh_command(pod)
    assert "StrictHostKeyChecking=no" not in pod_ssh_command(pod)


def test_pod_ssh_command_points_at_the_pods_own_pin_file(home):
    assert str(home / ".lium" / "known_hosts" / "pod-1") in pod_ssh_command(_pod())
    assert str(home / ".lium" / "known_hosts" / "pod-2") in pod_ssh_command(_pod(pod_id="pod-2"))


def test_pod_ssh_command_creates_the_directory_but_no_pin_file(home):
    """A listing must not leave one file per pod behind; ssh writes the pin on the first connection."""
    pod_ssh_command(_pod())

    assert (home / ".lium" / "known_hosts").is_dir()
    assert not (home / ".lium" / "known_hosts" / "pod-1").exists()


def test_pod_ssh_command_is_the_insecure_pair_only_under_the_opt_out(home, monkeypatch):
    monkeypatch.setenv("LIUM_SSH_INSECURE", "1")

    assert pod_ssh_command(_pod()) == "ssh -p 40499 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null user@pod.example"


def test_pod_ssh_command_is_none_without_an_ssh_command_or_for_an_unexpected_one(home):
    assert pod_ssh_command(_pod(ssh_cmd=None)) is None
    assert pod_ssh_command(_pod(ssh_cmd="ssh user@pod.example -p 22; touch $HOME/x")) is None


def test_pod_ssh_command_and_lium_ssh_agree_on_everything_but_the_key(home):
    pod = _pod()
    with_key = Lium(Config(api_key="test", ssh_key_path="/keys/id_ed25519")).ssh(pod)

    assert with_key == f"ssh -i /keys/id_ed25519 -p 40499 {_options(pod)} user@pod.example"
    assert pod_ssh_command(pod) == with_key.replace("-i /keys/id_ed25519 ", "")


# --- refresh --------------------------------------------------------------------------------


def _client(pods=None, key_path="/keys/id_ed25519"):
    client = Lium(Config(api_key="test", ssh_key_path=key_path))
    client.ps = lambda: list(pods or [])
    return client


def test_sdk_ssh_refresh_uses_the_pod_as_it_is_now(home):
    """After a restart the port moved; the caller's PodInfo still has the old one."""
    stale = _pod("ssh user@pod.example -p 40499")
    current = _pod("ssh user@pod.example -p 41000")

    cmd = _client(pods=[current]).ssh(stale, refresh=True)

    assert "-p 41000" in cmd and "-p 40499" not in cmd


def test_sdk_ssh_without_refresh_does_not_touch_the_api(home):
    client = _client()
    client.ps = lambda: pytest.fail("ps must not be called without refresh=True")

    assert client.ssh(_pod()).startswith("ssh -i ")


def test_sdk_ssh_refresh_raises_when_the_pod_is_gone(home):
    with pytest.raises(LiumNotFoundError):
        _client(pods=[_pod(pod_id="other")]).ssh(_pod(), refresh=True)


def test_refresh_pod_returns_the_current_record():
    current = _pod("ssh user@pod.example -p 41000")

    assert _client(pods=[current]).refresh_pod(_pod()).ssh_cmd == current.ssh_cmd
    assert _client(pods=[current]).refresh_pod("pod-1").ssh_cmd == current.ssh_cmd
    assert _client(pods=[current]).refresh_pod("eager-wolf-aa").ssh_cmd == current.ssh_cmd


def test_refresh_pod_raises_when_the_pod_is_gone():
    with pytest.raises(LiumNotFoundError):
        _client(pods=[_pod(pod_id="other")]).refresh_pod(_pod())


# --- the JSON views -------------------------------------------------------------------------


def test_ps_json_offers_a_restart_safe_ssh_command_next_to_the_raw_one(home):
    pod = _pod()
    row = ps_display.compact_pod(pod)

    assert row["ssh_cmd"] == RAW, "the raw API value stays for compatibility"
    assert row["ssh_command"] == pod_ssh_command(pod)


def test_describe_manifest_ssh_command_is_restart_safe_and_keeps_the_raw_one(home):
    pod = _pod()
    access = describe_display.build_manifest(pod)["access"]

    assert access["ssh_command"] == pod_ssh_command(pod)
    assert access["ssh_cmd"] == RAW


def test_describe_table_ssh_row_is_the_restart_safe_command(home):
    pod = _pod()
    table = describe_display.build_manifest_table(describe_display.build_manifest(pod))

    rows = {str(label): str(value) for label, value in zip(table.columns[0]._cells, table.columns[1]._cells)}
    assert rows["SSH"] == pod_ssh_command(pod)


def test_describe_without_ssh_keeps_none(home):
    access = describe_display.build_manifest(_pod(ssh_cmd=None))["access"]

    assert access["ssh_command"] is None and access["ssh_cmd"] is None
