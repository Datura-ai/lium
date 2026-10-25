"""`lium up` must not hand back a pod with fewer GPUs than were requested or billed.

Two failure modes were observed on real rentals: a pod comes up RUNNING with a
smaller GPU count than ``--count`` asked for, and a pod is billed for N GPUs
while ``nvidia-smi`` inside it sees fewer. Both used to exit 0 and connect.

The billed side of the comparison is the pod's own ``gpu_count`` from ``/pods``,
never the host's: for a GPU-split rental the nested executor still describes
the whole machine.
"""

from types import SimpleNamespace

from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.up import command as up_module
from lium.cli.up.actions import (
    VerifyGpuCountAction,
)
from lium.cli.utils import EXIT_GENERAL_ERROR

NODE_ID = "id-brave-orbit-b9"
WHOLE_HOST = object()   # `free=` default: every GPU of the node is rentable


def _executor(gpu_count: int, free: int | None | object = WHOLE_HOST) -> SimpleNamespace:
    """A node with ``gpu_count`` GPUs; ``free`` GPUs rentable (default all; None: the API sent no count)."""
    return SimpleNamespace(
        id=NODE_ID,
        huid="brave-orbit-b9",
        gpu_type="H200",
        gpu_count=gpu_count,
        available_gpu_count=gpu_count if free == WHOLE_HOST else free,
        price_per_hour=8.0 * gpu_count,
        price_per_gpu=8.0,
        location={"country": "United States", "country_code": "US"},
        specs={"gpu": {"count": gpu_count}},
        download_speed=1000,
        upload_speed=1000,
        available_port_count=5,
        docker_in_docker=False,
        max_cuda_version=12.8,
        tier="secure",
    )


def _pod(billed_gpus: int | None, host_gpus: int = 8) -> SimpleNamespace:
    """A RUNNING pod billed for ``billed_gpus`` (None: the API sent no count) on a ``host_gpus`` node."""
    return SimpleNamespace(
        id="pod-uuid-1234",
        huid="eager-wolf-aa",
        name="brave-orbit-b9",
        status="RUNNING",
        ssh_cmd="ssh root@203.0.113.10 -p 20022",
        ports={"22": 20022},
        executor=_executor(host_gpus),
        gpu_count=billed_gpus,
    )


def _nvidia_smi_output(count: int) -> str:
    return "".join(f"GPU {i}: NVIDIA H200 (UUID: GPU-{i:08d}-0000-0000-0000-000000000000)\n" for i in range(count))


class _FakeLium:
    """Stands in for the SDK: a node, the pod it produced, and what nvidia-smi says."""

    node_gpus = 8
    node_free: int | None | object = WHOLE_HOST
    billed_gpus: int | None = 8
    visible_gpus: int | None = 8   # None: nvidia-smi is not installed in the image
    exec_error: Exception | None = None
    removed: list[str] = []
    exec_commands: list[str] = []
    # a server without workspaces: `up` reads it for its workspace line
    workspaces = SimpleNamespace(current=lambda: None)
    up_kwargs: dict = {}   # what the CLI asked the SDK to rent

    def __init__(self, *args, **kwargs):
        pass

    def supports(self, feature):
        return False  # an older backend: no rent_by_spec, the client picks the node (DAH-3047)

    def get_executor(self, executor_id):
        return _executor(self.node_gpus, free=self.node_free)

    def ls(self, **kwargs):
        return [_executor(self.node_gpus, free=self.node_free)]

    def default_docker_template(self, executor_id):
        return SimpleNamespace(id="tpl-1", name="pytorch")

    def get_deployment_estimate(self, executor_id, template_id):
        return {}

    def up(self, **kwargs):
        _FakeLium.up_kwargs = dict(kwargs)
        return {"id": "pod-uuid-1234", "name": "brave-orbit-b9"}

    def ps(self):
        return [_pod(self.billed_gpus, host_gpus=self.node_gpus)]

    def wait_ready(self, pod, *, timeout=None, poll_interval=None, on_poll=None):
        # the CLI waits through Lium.wait_ready (DAH-2558); the pod here is ready on the first look
        return self.ps()[0]

    def exec(self, pod, command=None, env=None):
        _FakeLium.exec_commands.append(command)
        if self.exec_error:
            raise self.exec_error
        if self.visible_gpus is None:
            return {"success": False, "exit_code": 127, "stdout": "", "stderr": "bash: nvidia-smi: command not found\n"}
        return {"success": True, "exit_code": 0, "stdout": _nvidia_smi_output(self.visible_gpus), "stderr": ""}

    def rm(self, pod):
        _FakeLium.removed.append(pod.huid)


def _run_up(monkeypatch, *args, node_gpus=8, free=WHOLE_HOST, billed=8, visible=8, exec_error=None, by_node=False, yes=True):
    """Run `up`. A `-c` selects by --gpu filter (the count as a filter) unless `by_node`, where it is
    the number of the named node's GPUs to rent (DAH-3074). `yes=False` leaves the confirm prompt in."""
    _FakeLium.node_gpus = node_gpus
    _FakeLium.node_free = free
    _FakeLium.billed_gpus = billed
    _FakeLium.visible_gpus = visible
    _FakeLium.exec_error = exec_error
    _FakeLium.removed = []
    _FakeLium.exec_commands = []
    _FakeLium.up_kwargs = {}
    monkeypatch.setattr(up_module, "Lium", _FakeLium)
    monkeypatch.setattr(up_module, "ensure_config", lambda: None)
    monkeypatch.setattr("lium.cli.ls.command.ls_store_executor", lambda **kwargs: [])
    target = ["some-node-id"] if by_node or "-c" not in args else ["--gpu", "H200"]
    return CliRunner().invoke(cli, ["up", *target, *(["-y"] if yes else []), "--no-ssh", *args])


def test_up_fails_on_a_silent_gpu_downgrade(monkeypatch):
    """Requested 8, billed 1: the pod is named, both numbers are shown, exit is non-zero."""
    result = _run_up(monkeypatch, "-c", "8", node_gpus=8, billed=1)

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "requested 8" in result.output
    assert "billed for 1" in result.output
    assert NODE_ID in result.output
    assert "eager-wolf-aa" in result.output
    # Without --strict-gpus the pod is left for the caller to inspect or remove.
    assert _FakeLium.removed == []


def test_up_without_count_on_a_split_host_still_catches_a_downgrade(monkeypatch):
    """2 free, billed for 1: fewer than the rent could take is still a mismatch."""
    result = _run_up(monkeypatch, node_gpus=8, free=2, billed=1)

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "requested 2" in result.output
    assert "billed for 1" in result.output


def test_strict_gpus_removes_a_downgraded_pod(monkeypatch):
    result = _run_up(monkeypatch, "-c", "8", "--strict-gpus", node_gpus=8, billed=1)

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert _FakeLium.removed == ["eager-wolf-aa"]
    assert "removed" in result.output


def test_strict_gpus_removes_a_pod_with_phantom_gpus(monkeypatch):
    result = _run_up(
        monkeypatch, "-c", "4", "--verify-gpus", "--strict-gpus", node_gpus=4, billed=4, visible=2
    )

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert _FakeLium.removed == ["eager-wolf-aa"]


def test_strict_gpus_keeps_a_pod_it_could_not_check(monkeypatch):
    """An SSH failure is not evidence of a bad pod; strict mode must not remove it."""
    result = _run_up(
        monkeypatch,
        "-c", "4", "--verify-gpus", "--strict-gpus",
        node_gpus=4, billed=4, exec_error=RuntimeError("no SSH key configured"),
    )

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "Could not verify GPU count" in result.output
    assert _FakeLium.removed == []


# --- the billed side is the pod's count, not the host's -----------------------------------------


def test_split_rental_matches_when_the_pod_has_what_was_asked():
    result = VerifyGpuCountAction().execute({
        "lium": None, "pod": _pod(2, host_gpus=8), "expected_count": 2, "executor_id": NODE_ID,
    })

    assert result.ok is True, result.error
    assert result.data["billed"] == 2


def test_split_rental_fewer_than_asked_is_a_mismatch():
    result = VerifyGpuCountAction().execute({
        "lium": None, "pod": _pod(2, host_gpus=8), "expected_count": 8, "executor_id": NODE_ID,
    })

    assert result.ok is False
    assert result.data["mismatch"] is True
    assert "requested 8, pod is billed for 2" in result.error


# --- nvidia-smi -L is read by exit code and GPU lines, not by counting output lines -------------


# --- sshd may not be listening yet when the pod turns RUNNING ------------------------------------


# `lium up <node> -c N` rents N GPUs of a splittable node (DAH-3074). Before, `-c` was only a filter
# for auto-selection and was rejected next to a node id, so a renter could not ask for 1 GPU of a
# 3×RTX 3090 node from the CLI although the API allows it. These check that --count is allowed with a
# node id, that the SDK sends `gpu_count` only when one was asked for, and that the rent action threads it.




def test_up_partial_count_on_a_whole_host_only_node_is_refused_before_renting(monkeypatch):
    """Listing min_gpu_count_for_rental null = whole host only: -c 2 of 8 fails with the full count."""
    plain = _executor

    def whole_host_only(*args, **kwargs):
        executor = plain(*args, **kwargs)
        executor.whole_host_only = True
        return executor

    monkeypatch.setitem(globals(), "_executor", whole_host_only)
    result = _run_up(monkeypatch, "-c", "2", node_gpus=8, by_node=True)

    assert result.exit_code != 0
    assert "rents whole only. Request all 8 GPUs." in result.output
    assert _FakeLium.up_kwargs == {}
