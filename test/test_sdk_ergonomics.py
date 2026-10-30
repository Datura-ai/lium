"""SDK ergonomics for programs: rent-and-clean-up, wait, timeouts, detach, GPU stats, to_dict.

A script that rents a pod has to write the same twenty lines every time: poll
`ps` until RUNNING, remember to `rm` in a `finally`, wrap long commands in
`nohup setsid ... < /dev/null &`, parse `nvidia-smi`. These belong in the SDK.
"""

import inspect
import time
from types import SimpleNamespace

import pytest

from lium.sdk import Config, ExecutorInfo, Lium, LiumError, PodInfo


def _pod(status: str = "RUNNING", ssh_cmd: str | None = "ssh root@1.2.3.4 -p 20299") -> PodInfo:
    return PodInfo(
        id="pod-1", name="job", huid="swift-fox-c8", status=status, ssh_cmd=ssh_cmd,
        ports={"22": 20299}, created_at="2026-01-01T00:00:00Z", updated_at="",
        executor=_executor(), template={"id": "tpl-1"}, removal_scheduled_at=None,
        jupyter_installation_status=None, jupyter_url=None,
    )


def _executor() -> ExecutorInfo:
    return ExecutorInfo(
        id="exec-1", huid="brave-otter-11", machine_name="NVIDIA H100 80GB HBM3", gpu_type="H100",
        gpu_count=1, price_per_hour=2.0, price_per_gpu=2.0, location={"country": "US"},
        specs={"gpu": {"driver": "550.0", "details": [{"name": "H100 80GB HBM3"}]}},
        status="active", docker_in_docker=False, ip="1.2.3.4",
    )


class _Client(Lium):
    """A client whose rent, listing and removal are recorded rather than sent."""

    def __init__(self, ps_sequence=None):
        super().__init__(Config(api_key="test"))
        self.calls: list = []
        self._ps_sequence = list(ps_sequence or [])

    def _rent(self, **kwargs):
        # a fake that took any keyword set hid a rental() that no longer matched the real
        # _rent() (main's `image`, lium#161): every call is bound against the real signature
        inspect.signature(Lium._rent).bind(self, **kwargs)
        self.calls.append(("rent", kwargs))
        return {"id": "pod-1", "name": kwargs.get("name")}

    def ps(self):
        self.calls.append(("ps",))
        if len(self._ps_sequence) > 1:
            return self._ps_sequence.pop(0)
        return self._ps_sequence[0] if self._ps_sequence else []

    def _request(self, method, endpoint, **kwargs):
        self.calls.append((method, endpoint))
        return SimpleNamespace(json=lambda: {})


# --- up(wait=True), pod_by_name, rental() -----------------------------------------------------

def test_up_without_wait_returns_the_rent_payload_unchanged():
    client = _Client()

    created = client.up(executor_id="exec-1", name="job")

    assert created == {"id": "pod-1", "name": "job"}


def test_up_with_wait_returns_the_ready_pod():
    client = _Client(ps_sequence=[[_pod("PENDING", None)], [_pod("RUNNING")]])

    pod = client.up(executor_id="exec-1", name="job", wait=True)

    assert isinstance(pod, PodInfo)
    assert pod.status == "RUNNING" and pod.ssh_cmd


def test_up_verify_gpus_checks_visible_5090(monkeypatch):
    pod = _pod()
    pod.gpu_count = 1
    client = _Client(ps_sequence=[[pod]])
    seen = []
    monkeypatch.setattr(client, "exec", lambda ready, *, command: (
        seen.append((ready.id, command)) or {
            "success": True, "exit_code": 0,
            "stdout": "GPU 0: NVIDIA GeForce RTX 5090 (UUID: GPU-abc)\n", "stderr": "",
        }
    ))

    assert client.up(executor_id="exec-1", wait=True, gpu_count=1, verify_gpus=True) == pod
    assert seen == [("pod-1", "nvidia-smi -L")]


def test_up_verify_gpus_rejects_billed_count_before_ssh(monkeypatch):
    pod = _pod()
    pod.gpu_count = 1
    client = _Client(ps_sequence=[[pod]])
    monkeypatch.setattr(client, "exec", lambda *args, **kwargs: pytest.fail("SSH should not run"))

    with pytest.raises(LiumError, match="requested 2.*billed for 1.*pod-1"):
        client.up(executor_id="exec-1", wait=True, gpu_count=2, verify_gpus=True)


def test_up_verify_gpus_reports_visible_mismatch_and_billing(monkeypatch):
    pod = _pod()
    pod.gpu_count = 2
    client = _Client(ps_sequence=[[pod]])
    monkeypatch.setattr(client, "exec", lambda *args, **kwargs: {
        "success": True, "exit_code": 0,
        "stdout": "GPU 0: NVIDIA GeForce RTX 5090 (UUID: GPU-abc)\n", "stderr": "",
    })

    with pytest.raises(LiumError, match="billed for 2.*nvidia-smi reports 1.*pod-1"):
        client.up(executor_id="exec-1", wait=True, gpu_count=2, verify_gpus=True)


def test_up_verify_gpus_uses_split_pod_count_not_host_count(monkeypatch):
    pod = _pod()
    pod.gpu_count = 2
    pod.executor.gpu_count = 8
    client = _Client(ps_sequence=[[pod]])
    monkeypatch.setattr(client, "exec", lambda *args, **kwargs: {
        "success": True, "exit_code": 0,
        "stdout": "GPU 0: RTX 5090\nGPU 1: RTX 5090\n",
    })

    assert client.up(executor_id="exec-1", wait=True, gpu_count=2, verify_gpus=True) == pod


def test_up_verify_gpus_reports_missing_billed_count(monkeypatch):
    pod = _pod()
    client = _Client(ps_sequence=[[pod]])
    monkeypatch.setattr(client, "exec", lambda *args, **kwargs: pytest.fail("SSH should not run"))

    with pytest.raises(LiumError, match="did not report a billed GPU count.*pod-1"):
        client.up(executor_id="exec-1", wait=True, verify_gpus=True)


def test_up_verify_gpus_rejects_missing_billed_count_with_requested_count(monkeypatch):
    pod = _pod()
    client = _Client(ps_sequence=[[pod]])
    monkeypatch.setattr(client, "exec", lambda *args, **kwargs: pytest.fail("SSH should not run"))

    with pytest.raises(LiumError, match="did not report a billed GPU count.*pod-1"):
        client.up(executor_id="exec-1", wait=True, gpu_count=1, verify_gpus=True)


def test_up_verify_gpus_reports_uncheckable_pod_without_claiming_mismatch(monkeypatch):
    pod = _pod()
    pod.gpu_count = 1
    client = _Client(ps_sequence=[[pod]])
    monkeypatch.setattr(client, "exec", lambda *args, **kwargs: {
        "success": False, "exit_code": 127, "stdout": "",
        "stderr": "nvidia-smi: command not found\n",
    })

    with pytest.raises(LiumError, match="could not verify.*pod-1.*still billing"):
        client.up(executor_id="exec-1", wait=True, verify_gpus=True)


def test_up_verify_gpus_retries_ssh_before_accepting_pod(monkeypatch):
    pod = _pod()
    pod.gpu_count = 1
    client = _Client(ps_sequence=[[pod]])
    calls = []

    def eventually_ready(*args, **kwargs):
        calls.append(kwargs["command"])
        if len(calls) == 1:
            raise OSError("connection refused")
        return {"success": True, "exit_code": 0, "stdout": "GPU 0: RTX 5090\n"}

    monkeypatch.setattr(client, "exec", eventually_ready)

    assert client.up(executor_id="exec-1", wait=True, verify_gpus=True) == pod
    assert calls == ["nvidia-smi -L", "nvidia-smi -L"]


def test_up_verify_gpus_rejects_empty_success_output(monkeypatch):
    pod = _pod()
    pod.gpu_count = 1
    client = _Client(ps_sequence=[[pod]])
    monkeypatch.setattr(client, "exec", lambda *args, **kwargs: {
        "success": True, "exit_code": 0, "stdout": "No devices were found\n",
    })

    with pytest.raises(LiumError, match="listed no GPUs.*pod-1"):
        client.up(executor_id="exec-1", wait=True, verify_gpus=True)


def test_up_verify_gpus_requires_wait_before_renting():
    client = _Client()

    with pytest.raises(ValueError, match="wait=True"):
        client.up(executor_id="exec-1", verify_gpus=True)
    assert client.calls == []


def test_up_with_wait_names_the_billing_pod_when_it_never_becomes_ready(monkeypatch):
    client = _Client(ps_sequence=[[_pod("PENDING", None)]])
    clock = iter([0, 0, 1000, 1000, 1000])
    monkeypatch.setattr(time, "time", lambda: next(clock))

    with pytest.raises(LiumError, match="pod-1"):
        client.up(executor_id="exec-1", name="job", wait=True, timeout=5)


def test_pod_by_name_matches_name_huid_or_id():
    client = _Client(ps_sequence=[[_pod()]])

    assert client.pod_by_name("job").id == "pod-1"
    assert client.pod_by_name("swift-fox-c8").id == "pod-1"
    assert client.pod_by_name("pod-1").id == "pod-1"
    assert client.pod_by_name("nope") is None


def test_rental_removes_the_pod_after_the_block():
    client = _Client(ps_sequence=[[_pod()]])

    with client.rental(executor_id="exec-1", name="job") as pod:
        assert pod.status == "RUNNING"

    assert ("DELETE", "/pods/pod-1") in client.calls


def test_up_and_rental_pass_the_backup_opt_out_through_to_the_rent():
    client = _Client(ps_sequence=[[_pod("RUNNING")]])

    client.up(executor_id="exec-1", name="job", enable_backups=False)
    with client.rental(executor_id="exec-1", name="job", enable_backups=False):
        pass

    rents = [call[1] for call in client.calls if call[0] == "rent"]
    assert [r["enable_backups"] for r in rents] == [False, False]


def test_rental_removes_the_pod_when_the_block_raises():
    client = _Client(ps_sequence=[[_pod()]])

    with pytest.raises(RuntimeError, match="job failed"):
        with client.rental(executor_id="exec-1"):
            raise RuntimeError("job failed")

    assert client.calls[-1] == ("DELETE", "/pods/pod-1")


def test_rental_that_failed_before_the_rent_post_leaves_a_concurrent_same_name_pod():
    """A failure before the POST (here the template lookup) cannot have created a pod: a
    same-name pod that appeared meanwhile on the node is another worker's and stays."""
    other = _pod()
    other.id, other.name = "pod-other", "Your Pod"

    class _LookupFails(_Client):
        def _rent(self, **kwargs):
            return Lium._rent(self, **kwargs)  # the real one, up to its failed lookup

        def get_executor(self, executor_id):
            return _executor()

        def get_template(self, template_id):
            return None

    client = _LookupFails(ps_sequence=[[], [other]])

    with pytest.raises(LiumError, match="Could not look up template"):
        with client.rental(executor_id="exec-1", template_id="tpl-x", ssh_keys=["ssh-ed25519 AAA"]):
            pass

    assert not [c for c in client.calls if c[0] == "DELETE"]


# --- exec(timeout=), exec(detach=True) ---------------------------------------------------


# --- gpu_stats ------------------------------------------------------------------------------


# --- to_dict, schema ---------------------------------------------------------------------------


