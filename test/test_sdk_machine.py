"""`@lium.machine` against a fake client: node selection, TTL, timeout and cleanup."""

import subprocess
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lium.sdk import ExecutorInfo, LiumError, PodInfo
from lium.sdk import decorators as D


def _executor(gpu_type, count, price, huid="node", machine_name=None, country="US"):
    return ExecutorInfo(
        id=f"{huid}-id", huid=huid,
        machine_name=machine_name or f"NVIDIA {gpu_type}",
        gpu_type=gpu_type, gpu_count=count,
        price_per_hour=price, price_per_gpu=price / count,
        location={"country": country}, specs={}, status="online",
        docker_in_docker=False, ip="203.0.113.4",
    )


EXECUTORS = [
    _executor("A100", 8, 3.60, "eight", "NVIDIA A100-SXM4-80GB"),   # what the API lists first
    _executor("A100", 1, 1.50, "one-dear", "NVIDIA A100-SXM4-80GB"),
    _executor("A100", 1, 1.20, "one-cheap", "NVIDIA A100-SXM4-80GB"),
    _executor("H200", 1, 2.75, "h200"),
    _executor("RTX4090", 1, 0.30, "rtx", "NVIDIA GeForce RTX 4090", country="Romania"),
]


def _pod(pod_id="pod-1"):
    return PodInfo(
        id=pod_id, name="remote-fn", status="RUNNING", huid="swift-fox-c8",
        ssh_cmd="ssh root@10.0.0.1 -p 22", ports={}, created_at="", updated_at="",
        executor=None, template={}, removal_scheduled_at=None,
        jupyter_installation_status=None, jupyter_url=None,
    )


class FakeLium:
    """Records every call the decorator makes and runs the uploaded runner locally."""

    ready = True
    run_exit_code = None  # None = really run the runner; int = pretend it exited with that code
    rent_by_spec = False  # True = a backend with POST /executors/rent-by-spec

    def __init__(self, sandbox: Path):
        self.sandbox = sandbox
        self.calls = []
        self.uploaded = {}
        self.pods = {}      # id -> PodInfo the fake "server" has running
        self.rented = 0
        self.result_paths = []   # where each call's runner writes its envelope (+ ".npz" beside it)

    def supports(self, feature):
        self.calls.append(("supports", feature))
        return self.rent_by_spec and feature == "rent_by_spec"

    def rent(self, **kw):
        # the server's pick: the cheapest node of that size and type, billed per GPU rented
        self.calls.append(("rent", kw))
        executor = D._select_executor(EXECUTORS, f"{kw['gpu_count']}x{kw['gpu_type']}")
        pod = self.up(executor_id=executor.id, name=kw["name"], template_id=kw.get("template_id"))
        # what Lium.rent returns (a RentResult, #184): the decorator reads executor, price_per_hour, pod
        return SimpleNamespace(executor=executor, price_per_hour=executor.price_per_gpu * kw["gpu_count"], pod=pod)

    def ls(self, **kw):
        self.calls.append(("ls",))
        return list(EXECUTORS)

    def ps(self):
        self.calls.append(("ps",))
        return list(self.pods.values())

    open_connections: set = set()   # pod ids Lium.has_open_connection() answers True for

    def has_open_connection(self, pod):
        return pod.id in self.open_connections

    def up(self, **kw):
        self.calls.append(("up", kw))
        self.rented += 1
        pod_id = f"pod-{self.rented}"
        pod = _pod(pod_id)
        pod.name = kw["name"]
        pod.executor = next(e for e in EXECUTORS if e.id == kw["executor_id"])
        self.pods[pod_id] = pod
        return {"id": pod_id, "name": kw["name"]}

    def schedule_termination(self, pod, *, termination_time):
        self.calls.append(("schedule_termination", pod.id, termination_time))
        return {}

    def wait_ready(self, pod, timeout=300):
        self.calls.append(("wait_ready", pod["id"], timeout))
        return self.pods[pod["id"]] if self.ready else None

    def upload(self, pod, *, local, remote):
        self.calls.append(("upload", remote))
        text = Path(local).read_text().replace("'/tmp/", f"'{self.sandbox}/")
        self.uploaded[remote] = text
        (self.sandbox / Path(remote).name).write_text(text)
        self.result_paths.append(str(self.sandbox / (Path(remote).stem + ".json")))

    def exec(self, pod, *, command, env=None):
        self.calls.append(("exec", command))
        return {"stdout": "", "stderr": "", "exit_code": 0, "success": True}

    def stream_exec(self, pod, *, command, env=None, pty=True):
        self.calls.append(("stream_exec", command, pty))
        runner = next(r for r in self.uploaded if r in command)
        if self.run_exit_code is not None:
            return self.run_exit_code
        proc = subprocess.Popen([sys.executable, "-u", str(self.sandbox / Path(runner).name)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for line in proc.stdout:
            yield {"type": "stdout", "data": line}
        for line in proc.stderr:
            yield {"type": "stderr", "data": line}
        return proc.wait(timeout=30)

    def download(self, pod, *, remote, local):
        Path(local).write_bytes((self.sandbox / Path(remote).name).read_bytes())

    @contextmanager
    def ssh_session(self, pod, timeout=30):
        self.calls.append(("ssh_session", pod.id))
        yield object()

    def down(self, pod):
        self.calls.append(("down", pod.id))
        self.pods.pop(pod.id, None)
        return {}


def _execs(fake):
    return [c[1] for c in fake.calls if c[0] in ("exec", "stream_exec")]


@pytest.fixture
def fake(monkeypatch, tmp_path):
    client = FakeLium(tmp_path)
    monkeypatch.setattr(D, "Lium", lambda: client)
    monkeypatch.setattr(D, "_WARM", {})
    FakeLium.ready = True
    FakeLium.run_exit_code = None
    FakeLium.rent_by_spec = False
    return client


def double(x):
    return x * 2


def one():
    return 1


def test_call_rents_cheapest_sets_ttl_bounds_run_and_cleans_up(fake, capsys):
    remote = D.machine(machine="A100", timeout=600)(double)

    assert remote(21) == 42

    up = next(c[1] for c in fake.calls if c[0] == "up")
    assert up["executor_id"] == "one-cheap-id"
    assert up["template_id"] is None                     # Lium.up resolves the node's default itself

    _, pod_id, when = next(c for c in fake.calls if c[0] == "schedule_termination")
    assert pod_id == "pod-1"
    ttl = datetime.fromisoformat(when) - datetime.now(timezone.utc)
    assert 600 + 14 * 60 < ttl.total_seconds() <= 600 + 15 * 60

    run = next(cmd for cmd in _execs(fake) if cmd.endswith(".py"))
    assert run.startswith("timeout -k 5 600 ") and " -u " in run
    assert ("down", "pod-1") in fake.calls
    assert not any(cmd.startswith("rm -rf") for cmd in _execs(fake))

    err = capsys.readouterr().err
    assert "[lium] double: rented 1xA100 $1.20/h (one-cheap, US), removal in 0.4h" in err
    assert "[lium] double: done in" in err
    assert "[lium] double: pod removed" in err


@pytest.mark.parametrize("field, other", [("api_key", "key-b"), ("base_url", "https://other.example/api"), ("workspace_id", "ws-2")])
def test_a_warm_pod_is_not_handed_to_another_account(monkeypatch, field, other):
    from types import SimpleNamespace

    def sdk(**changes):
        config = dict(api_key="key-a", base_url="https://lium.io/api", workspace_id="ws-1", workspace=None, **changes)
        pod = SimpleNamespace(id="pod-1", status="RUNNING", ssh_cmd="ssh root@h -p 1", name="x", executor=None)
        return SimpleNamespace(config=SimpleNamespace(**config), ps=lambda: [pod], has_open_connection=lambda p: True)

    old, new = sdk(), sdk()
    setattr(new.config, field, other)
    monkeypatch.setattr(D, "_WARM", {"k": D._Warm(old, old.ps()[0], None, 0, hourly=1.0)})

    assert D._find_warm(old, "k", lambda m: None).sdk is old                     # same account: reused
    monkeypatch.setitem(D._WARM, "k", D._Warm(old, old.ps()[0], None, 0, hourly=1.0))
    assert D._find_warm(new, "k", lambda m: None) is None                        # changed: not the old account's pod


def test_pod_that_never_becomes_ready_is_removed(fake):
    FakeLium.ready = False

    with pytest.raises(LiumError, match="failed to start within 300s"):
        D.machine(machine="A100", quiet=True)(one)()
    assert ("down", "pod-1") in fake.calls
