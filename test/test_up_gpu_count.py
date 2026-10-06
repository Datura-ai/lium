"""`lium up` must not hand back a pod with fewer GPUs than were requested or billed.

Two failure modes were observed on real rentals: a pod comes up RUNNING with a
smaller GPU count than ``--count`` asked for, and a pod is billed for N GPUs
while ``nvidia-smi`` inside it sees fewer. Both used to exit 0 and connect.

The billed side of the comparison is the pod's own ``gpu_count`` from ``/pods``,
never the host's: for a GPU-split rental the nested executor still describes
the whole machine.
"""

from types import SimpleNamespace

import paramiko
import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.up import command as up_module
from lium.cli.up import validation as up_validation
from lium.cli.up.actions import (
    SSH_RETRY_INTERVAL,
    VISIBLE_GPU_COUNT_COMMAND,
    RentPodAction,
    VerifyGpuCountAction,
    billed_gpu_count,
    parse_visible_gpu_count,
    rented_gpu_count,
)
from lium.cli.utils import EXIT_CONFIGURATION_ERROR, EXIT_GENERAL_ERROR
from lium.sdk import Config, Lium

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


def test_up_succeeds_when_the_pod_has_the_requested_gpus(monkeypatch):
    result = _run_up(monkeypatch, "-c", "8", node_gpus=8, billed=8)

    assert result.exit_code == 0, result.output
    assert _FakeLium.removed == []


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


def test_up_without_count_compares_against_the_chosen_node(monkeypatch):
    """No --count: the node the user chose (4 GPUs, all free) is the request."""
    result = _run_up(monkeypatch, node_gpus=4, billed=2)

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "requested 4" in result.output
    assert "billed for 2" in result.output


def test_up_without_count_on_a_partially_free_split_host_expects_the_free_gpus(monkeypatch):
    """2 of the host's 8 GPUs are free: the rent takes and bills those 2, so the pod is right."""
    result = _run_up(monkeypatch, "--strict-gpus", node_gpus=8, free=2, billed=2)

    assert result.exit_code == 0, result.output
    assert _FakeLium.removed == []


def test_up_without_count_on_a_split_host_still_catches_a_downgrade(monkeypatch):
    """2 free, billed for 1: fewer than the rent could take is still a mismatch."""
    result = _run_up(monkeypatch, node_gpus=8, free=2, billed=1)

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "requested 2" in result.output
    assert "billed for 1" in result.output


def test_up_without_count_falls_back_to_the_host_total_when_the_api_sent_no_free_count(monkeypatch):
    result = _run_up(monkeypatch, node_gpus=8, free=None, billed=2)

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "requested 8" in result.output


def test_up_does_not_run_nvidia_smi_without_verify_gpus(monkeypatch):
    result = _run_up(monkeypatch, "-c", "8", billed=8, visible=2)

    assert result.exit_code == 0, result.output
    assert _FakeLium.exec_commands == []


def test_verify_gpus_fails_on_a_phantom_gpu(monkeypatch):
    """Billed 4, nvidia-smi sees 2: the pod is not what is being paid for."""
    result = _run_up(monkeypatch, "-c", "4", "--verify-gpus", node_gpus=4, billed=4, visible=2)

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "billed for 4" in result.output
    assert "nvidia-smi reports 2" in result.output
    assert NODE_ID in result.output
    assert _FakeLium.exec_commands == ["nvidia-smi -L"]
    assert _FakeLium.removed == []


def test_verify_gpus_passes_when_nvidia_smi_agrees(monkeypatch):
    result = _run_up(monkeypatch, "-c", "4", "--verify-gpus", node_gpus=4, billed=4, visible=4)

    assert result.exit_code == 0, result.output


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


def test_strict_gpus_keeps_a_pod_whose_image_has_no_nvidia_smi(monkeypatch):
    """`nvidia-smi: command not found` is "could not check"; the pod is billed correctly and stays."""
    result = _run_up(
        monkeypatch, "-c", "4", "--verify-gpus", "--strict-gpus", node_gpus=4, billed=4, visible=None
    )

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "exited 127" in result.output
    assert "command not found" in result.output
    assert _FakeLium.removed == []


def test_strict_gpus_keeps_a_matching_pod(monkeypatch):
    result = _run_up(monkeypatch, "-c", "8", "--strict-gpus", "--verify-gpus", billed=8, visible=8)

    assert result.exit_code == 0, result.output
    assert _FakeLium.removed == []


# --- the billed side is the pod's count, not the host's -----------------------------------------


def test_billed_count_is_the_pods_own_not_the_hosts():
    """A split rental: 2 of the host's 8 GPUs. The pod is billed for 2."""
    assert billed_gpu_count(_pod(2, host_gpus=8)) == 2
    assert billed_gpu_count(_pod("2", host_gpus=8)) == 2
    assert billed_gpu_count(_pod(None, host_gpus=8)) is None
    assert billed_gpu_count(SimpleNamespace(executor=_executor(8))) is None


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


def test_unknown_billed_count_does_not_fail_the_pod():
    result = VerifyGpuCountAction().execute({
        "lium": None, "pod": _pod(None), "expected_count": 8, "executor_id": NODE_ID,
    })

    assert result.ok is True
    assert result.data["billed"] is None


def test_rented_count_is_the_free_gpus_not_the_host():
    assert rented_gpu_count(_executor(8, free=2)) == 2
    assert rented_gpu_count(_executor(8)) == 8
    assert rented_gpu_count(_executor(8, free=None)) == 8


def test_sdk_ls_reads_the_nodes_free_gpu_count(monkeypatch):
    """`/executors` sends the host total in specs and the free count as `available_gpu_count`."""
    client = Lium(Config(api_key="test"))
    rows = [
        {"id": NODE_ID, "machine_name": "NVIDIA H200", "price_per_gpu": 8.0, "available_gpu_count": 2,
         "specs": {"gpu": {"count": 8, "details": [{"name": "H200"}]}}},
        {"id": "id-other", "machine_name": "NVIDIA H200", "price_per_gpu": 8.0,
         "specs": {"gpu": {"count": 8, "details": [{"name": "H200"}]}}},
    ]
    monkeypatch.setattr(client, "_request", lambda *a, **k: SimpleNamespace(json=lambda: rows))

    nodes = client.ls(gpu_count=None)

    assert [(n.gpu_count, n.available_gpu_count) for n in nodes] == [(8, 2), (8, None)]


def test_sdk_ps_reads_the_pods_own_gpu_count(monkeypatch):
    """`/pods` sends the pod's count as a string next to the whole-host executor record."""
    client = Lium(Config(api_key="test"))
    rows = [
        {"id": "pod-1", "pod_name": "a", "status": "RUNNING", "gpu_count": "2", "price": "1.0",
         "executor": {"id": NODE_ID, "machine_name": "NVIDIA H200", "specs": {"gpu": {"count": 8, "details": [{"name": "H200"}]}}}},
        {"id": "pod-2", "pod_name": "b", "status": "RUNNING",
         "executor": {"id": NODE_ID, "machine_name": "NVIDIA H200", "specs": {"gpu": {"count": 8, "details": [{"name": "H200"}]}}}},
        {"id": "pod-3", "pod_name": "c", "status": "RUNNING", "gpu_count": "n/a"},
    ]
    monkeypatch.setattr(client, "_request", lambda *a, **k: SimpleNamespace(json=lambda: rows))

    pods = client.ps()

    assert [p.gpu_count for p in pods] == [2, None, None]
    assert pods[0].executor.gpu_count == 8   # the host, untouched


# --- nvidia-smi -L is read by exit code and GPU lines, not by counting output lines -------------


@pytest.mark.parametrize(
    "stdout, expected",
    [
        (_nvidia_smi_output(8), 8),
        ("GPU 0: NVIDIA GeForce RTX 4090 (UUID: GPU-1)\n", 1),
        ("", None),
        ("bash: nvidia-smi: command not found\n", None),
        ("No devices were found\n", None),
    ],
)
def test_parse_visible_gpu_count(stdout, expected):
    assert parse_visible_gpu_count(stdout) == expected


def test_verify_command_is_nvidia_smi_alone():
    """Piping into `wc -l` made a missing nvidia-smi read as 0 GPUs — a "mismatch" strict mode acted on."""
    assert VISIBLE_GPU_COUNT_COMMAND == "nvidia-smi -L"


def test_verify_reports_a_failed_nvidia_smi_without_a_mismatch():
    """A non-zero exit is "could not check", not "wrong count" — strict mode must not remove."""
    class _NoSmi:
        def exec(self, pod, command=None, env=None):
            return {"stdout": "", "stderr": "nvidia-smi: command not found\n", "exit_code": 127, "success": False}

    result = VerifyGpuCountAction().execute({
        "lium": _NoSmi(), "pod": _pod(4), "expected_count": 4,
        "executor_id": NODE_ID, "verify_via_ssh": True,
    })

    assert result.ok is False
    assert result.data["mismatch"] is False
    assert "exited 127" in result.error


def test_verify_reports_an_empty_listing_without_a_mismatch():
    class _Empty:
        def exec(self, pod, command=None, env=None):
            return {"stdout": "", "stderr": "", "exit_code": 0, "success": True}

    result = VerifyGpuCountAction().execute({
        "lium": _Empty(), "pod": _pod(4), "expected_count": 4,
        "executor_id": NODE_ID, "verify_via_ssh": True,
    })

    assert result.ok is False
    assert result.data["mismatch"] is False
    assert "listed no GPU" in result.error


# --- sshd may not be listening yet when the pod turns RUNNING ------------------------------------


class _LateSshd:
    """Refuses the first ``refusals`` connections, then answers like a healthy 4-GPU pod."""

    def __init__(self, refusals: int, error: Exception | None = None):
        self.refusals = refusals
        self.error = error or paramiko.ssh_exception.NoValidConnectionsError(
            {("203.0.113.10", 20022): ConnectionRefusedError(111, "Connection refused")}
        )
        self.calls = 0

    def exec(self, pod, command=None, env=None):
        self.calls += 1
        if self.calls <= self.refusals:
            raise self.error
        return {"stdout": _nvidia_smi_output(4), "stderr": "", "exit_code": 0, "success": True}


def test_verify_retries_the_ssh_connection_while_sshd_comes_up():
    lium = _LateSshd(refusals=2)
    naps: list[float] = []

    result = VerifyGpuCountAction().execute({
        "lium": lium, "pod": _pod(4), "expected_count": 4, "executor_id": NODE_ID,
        "verify_via_ssh": True, "sleep": naps.append,
    })

    assert result.ok is True, result.error
    assert result.data["visible"] == 4
    assert lium.calls == 3
    assert naps == [SSH_RETRY_INTERVAL, SSH_RETRY_INTERVAL]


def test_verify_gives_up_after_the_retry_window_without_a_mismatch():
    lium = _LateSshd(refusals=100, error=OSError("connection timed out"))
    naps: list[float] = []

    result = VerifyGpuCountAction().execute({
        "lium": lium, "pod": _pod(4), "expected_count": 4, "executor_id": NODE_ID,
        "verify_via_ssh": True, "ssh_retry_seconds": 3 * SSH_RETRY_INTERVAL, "sleep": naps.append,
    })

    assert result.ok is False
    assert result.data["mismatch"] is False
    assert "no connection after 15s" in result.error
    assert lium.calls == 4
    assert len(naps) == 3


def test_verify_does_not_retry_a_configuration_error():
    lium = _LateSshd(refusals=100, error=ValueError("No SSH key configured"))
    naps: list[float] = []

    result = VerifyGpuCountAction().execute({
        "lium": lium, "pod": _pod(4), "expected_count": 4, "executor_id": NODE_ID,
        "verify_via_ssh": True, "sleep": naps.append,
    })

    assert result.ok is False
    assert result.data["mismatch"] is False
    assert lium.calls == 1
    assert naps == []


# `lium up <node> -c N` rents N GPUs of a splittable node (DAH-3074). Before, `-c` was only a filter
# for auto-selection and was rejected next to a node id, so a renter could not ask for 1 GPU of a
# 3×RTX 3090 node from the CLI although the API allows it. These check that --count is allowed with a
# node id, that the SDK sends `gpu_count` only when one was asked for, and that the rent action threads it.
class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def _stub_up(monkeypatch, client, captured):
    monkeypatch.setattr(client, "get_executor", lambda executor_id: SimpleNamespace(id="exec-1"))
    monkeypatch.setattr(client, "default_docker_template", lambda executor_id: SimpleNamespace(id="tmpl-default"))
    monkeypatch.setattr(client, "_ensure_ssh_keys_registered", lambda *a, **k: None)

    def fake_request(method, endpoint, json=None, **kwargs):
        captured["payload"] = json
        return _Resp({"id": "pod-1", "name": (json or {}).get("pod_name"), "status": "PENDING"})

    monkeypatch.setattr(client, "_request", fake_request)


def test_up_node_with_count_rents_that_many_gpus(monkeypatch):
    """`lium up <node> -c 1` on an 8-GPU node: the rent asks for 1 and the pod billed for 1 passes."""
    result = _run_up(monkeypatch, "-c", "1", by_node=True, node_gpus=8, billed=1, visible=1)

    assert result.exit_code == 0, result.output
    assert _FakeLium.up_kwargs["gpu_count"] == 1
    assert _FakeLium.removed == []


def test_up_node_with_the_host_total_still_sends_the_count(monkeypatch):
    """`-c 8` on an 8-GPU node with 1 free: the count goes to the API, which refuses what it cannot
    serve, instead of being dropped and renting the one free GPU under an 8-GPU prompt."""
    result = _run_up(monkeypatch, "-c", "8", by_node=True, node_gpus=8, free=1, billed=8)

    assert result.exit_code == 0, result.output
    assert _FakeLium.up_kwargs["gpu_count"] == 8


def test_up_node_without_count_sends_no_count(monkeypatch):
    result = _run_up(monkeypatch, by_node=True, node_gpus=8, billed=8)

    assert result.exit_code == 0, result.output
    assert _FakeLium.up_kwargs["gpu_count"] is None


def test_up_prompt_names_the_split_count_and_its_price(monkeypatch):
    """Without -y, `-c 1` of a 2-GPU node at $8/GPU is confirmed as 1 of 2 at $8.00/h."""
    asked: list[str] = []
    monkeypatch.setattr(up_module.ui, "confirm", lambda message, default=False: asked.append(message) or False)

    result = _run_up(monkeypatch, "-c", "1", by_node=True, node_gpus=2, yes=False)

    assert result.exit_code == 0, result.output
    assert asked == ["Acquire pod on brave-orbit-b9 (1×H200 of 2) at $8.00/h?"]
    assert _FakeLium.up_kwargs == {}   # declined: nothing was rented


def test_up_prompt_without_count_names_the_free_gpus(monkeypatch):
    """No -c on a 4-GPU node with 2 free: the rent takes the 2 free GPUs, so the prompt says 2 of 4."""
    asked: list[str] = []
    monkeypatch.setattr(up_module.ui, "confirm", lambda message, default=False: asked.append(message) or False)

    result = _run_up(monkeypatch, by_node=True, node_gpus=4, free=2, yes=False)

    assert result.exit_code == 0, result.output
    assert asked == ["Acquire pod on brave-orbit-b9 (2×H200 of 4) at $16.00/h?"]


def test_up_refuses_before_the_prompt_when_no_gpu_is_free(monkeypatch):
    """A fully rented 4-GPU node without -c: refused before the prompt could offer 0×H200 at $0.00/h."""
    asked: list[str] = []
    monkeypatch.setattr(up_module.ui, "confirm", lambda message, default=False: asked.append(message) or True)

    result = _run_up(monkeypatch, by_node=True, node_gpus=4, free=0, yes=False)

    assert result.exit_code == EXIT_GENERAL_ERROR, result.output
    assert "No GPU of brave-orbit-b9 is free" in result.output
    assert asked == [] and _FakeLium.up_kwargs == {}


def test_up_refuses_a_count_above_the_nodes_gpus(monkeypatch):
    """-c 5 on a 2-GPU node is refused here, not by the API after a prompt that said 5×H200 of 2."""
    result = _run_up(monkeypatch, "-c", "5", by_node=True, node_gpus=2, yes=False)

    assert result.exit_code == EXIT_CONFIGURATION_ERROR, result.output
    assert "-c 5: brave-orbit-b9 has 2 GPU(s)" in result.output
    assert _FakeLium.up_kwargs == {}


def test_validate_allows_count_with_node_id():
    ok, error = up_validation.validate("brave-fox-3a", None, 1, None, None, None)
    assert ok is True and error == ""


@pytest.mark.parametrize("gpu, country", [("H100", None), (None, "DE")])
def test_validate_still_rejects_real_filters_with_node_id(gpu, country):
    ok, error = up_validation.validate("brave-fox-3a", gpu, None, country, None, None)
    assert ok is False and "--gpu, --country" in error


def test_validate_rejects_non_positive_count():
    ok, error = up_validation.validate("brave-fox-3a", None, 0, None, None, None)
    assert ok is False and "--count" in error


def test_sdk_up_sends_gpu_count_when_given(monkeypatch):
    client = Lium(Config(api_key="test"))
    captured: dict = {}
    _stub_up(monkeypatch, client, captured)

    client.up(executor_id="exec-1", name="one-of-three", ssh_keys=["ssh-ed25519 AAA"], gpu_count=1)

    assert captured["payload"]["gpu_count"] == 1


def test_sdk_up_omits_gpu_count_for_a_whole_node(monkeypatch):
    client = Lium(Config(api_key="test"))
    captured: dict = {}
    _stub_up(monkeypatch, client, captured)

    client.up(executor_id="exec-1", name="whole", ssh_keys=["ssh-ed25519 AAA"])

    assert "gpu_count" not in captured["payload"]


def test_rent_pod_action_threads_gpu_count():
    captured: dict = {}

    class FakeLium:
        def up(self, **kwargs):
            captured.update(kwargs)
            return {"id": "pod-1", "name": kwargs["name"]}

    result = RentPodAction().execute(
        {"lium": FakeLium(), "executor": SimpleNamespace(id="exec-1", huid="brave-fox-3a"), "template": None,
         "name": "one-of-three", "gpu_count": 1}
    )

    assert result.ok
    assert captured["gpu_count"] == 1
