"""SDK ergonomics for programs: rent-and-clean-up, wait, timeouts, detach, GPU stats, to_dict.

A script that rents a pod has to write the same twenty lines every time: poll
`ps` until RUNNING, remember to `rm` in a `finally`, wrap long commands in
`nohup setsid ... < /dev/null &`, parse `nvidia-smi`. These belong in the SDK.
"""

import inspect
import time
from types import SimpleNamespace

import pytest

from lium.sdk import Config, ExecutorInfo, Lium, PodInfo


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


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


def test_rental_removes_the_pod_after_the_block():
    client = _Client(ps_sequence=[[_pod()]])

    with client.rental(executor_id="exec-1", name="job") as pod:
        assert pod.status == "RUNNING"

    assert ("DELETE", "/pods/pod-1") in client.calls


def test_rental_removes_the_pod_when_the_block_raises():
    client = _Client(ps_sequence=[[_pod()]])

    with pytest.raises(RuntimeError, match="job failed"):
        with client.rental(executor_id="exec-1"):
            raise RuntimeError("job failed")

    assert client.calls[-1] == ("DELETE", "/pods/pod-1")
