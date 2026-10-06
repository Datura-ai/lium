"""DAH-2589: `lium describe` — one manifest an agent can act on.

An agent working on a pod needs to know which external port reaches which port
inside the container, what GPU and image it got, and what the pod costs while it
runs. Today that is spread across `ps`, the dashboard and guesswork.
"""

import json
from datetime import datetime, timedelta, timezone

from click.testing import CliRunner

from lium.sdk import ExecutorInfo, PodInfo
from lium.sdk.exceptions import LiumNotFoundError
from lium.cli.cli import cli
from lium.cli.describe import command as describe_module
from lium.cli.describe import display
from lium.cli.utils import EXIT_POD_NOT_FOUND

_DEFAULT = object()
PRICE_PER_HOUR = 16.0
UPTIME_HOURS = 2


def _hours_ago(hours: int) -> str:
    """Creation timestamp relative to now, so the test does not depend on the wall clock."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def _executor() -> ExecutorInfo:
    return ExecutorInfo(
        id="executor-uuid-1",
        huid="brave-otter-11",
        machine_name="NVIDIA H100 SXM 8x",
        gpu_type="H100",
        gpu_count=8,
        price_per_hour=PRICE_PER_HOUR,
        price_per_gpu=2.0,
        location={"country": "US"},
        specs={"gpu": {"driver": "535.104.05", "details": [{"name": "H100 80GB HBM3"}]}},
        status="active",
        docker_in_docker=False,
        ip="1.2.3.4",
        max_cuda_version=12.4,
        tier="secure",
    )


def _pod(ports=_DEFAULT, executor=_DEFAULT, template=_DEFAULT) -> PodInfo:
    return PodInfo(
        id="pod-uuid-1",
        name="my-pod",
        status="running",
        huid="eager-wolf-aa",
        ssh_cmd="ssh root@1.2.3.4 -p 34567",
        ports={"22": 34567, "8000": 34568} if ports is _DEFAULT else ports,
        created_at=_hours_ago(UPTIME_HOURS),
        updated_at=_hours_ago(0),
        executor=_executor() if executor is _DEFAULT else executor,
        template=(
            {"name": "PyTorch 2.4", "docker_image": "pytorch/pytorch", "docker_image_tag": "2.4"}
            if template is _DEFAULT else template
        ),
        removal_scheduled_at=None,
        jupyter_installation_status=None,
        jupyter_url=None,
    )


class _FakeLium:
    """Stands in for the SDK: the pods a test dictates, or the error it dictates; plus the
    detail and event log the backend keeps for a pod (DAH-2932)."""

    pods: list = []
    error: Exception | None = None
    detail: dict = {}
    events: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    def ps(self):
        if self.error:
            raise self.error
        return list(self.pods)

    def pod(self, pod_id):
        return dict(self.detail)

    def pod_events(self, pod_id):
        return list(self.events.get(pod_id, []))


def _run_describe(
    monkeypatch,
    pods: list,
    target: str = "my-pod",
    error: Exception | None = None,
    detail: dict | None = None,
    events: dict | None = None,
    json_output: bool = True,
):
    _FakeLium.pods = pods
    _FakeLium.error = error
    _FakeLium.detail = detail or {}
    _FakeLium.events = events or {}
    monkeypatch.setattr(describe_module, "Lium", _FakeLium)
    return CliRunner().invoke(cli, ["describe", target, *(["--json"] if json_output else [])])


def test_manifest_names_the_port_direction():
    """Mapping keys are container ports — the manifest says so instead of leaving it to guesswork."""
    manifest = display.build_manifest(_pod())

    assert manifest["ports"]["direction"] == "internal -> external"
    assert manifest["ports"]["mapping"] == {"22": 34567, "8000": 34568}


def test_manifest_singles_out_the_ssh_port():
    """The external port reaching container port 22 is the one an agent needs first."""
    manifest = display.build_manifest(_pod())

    assert manifest["ports"]["ssh_external"] == 34567


def test_manifest_finds_the_ssh_port_under_an_integer_key():
    """A mapping built in Python carries int keys; the SSH port must not vanish."""
    manifest = display.build_manifest(_pod(ports={22: 34567, 8000: 34568}))

    assert manifest["ports"]["ssh_external"] == 34567
    assert manifest["ports"]["service_ports"] == [{"internal": 8000, "external": 34568}]


def test_manifest_keeps_a_non_numeric_port_key_instead_of_crashing():
    """A key like "8000/tcp" is reported as-is rather than taking the command down."""
    manifest = display.build_manifest(_pod(ports={"22": 34567, "8000/tcp": 34568}))

    assert manifest["ports"]["service_ports"] == [{"internal": "8000/tcp", "external": 34568}]


def test_manifest_service_ports_exclude_ssh():
    """Everything except 22 is a port a service can be published on."""
    manifest = display.build_manifest(_pod(ports={"22": 34567, "8000": 34568, "8888": 34569}))

    assert manifest["ports"]["service_ports"] == [
        {"internal": 8000, "external": 34568},
        {"internal": 8888, "external": 34569},
    ]


def test_manifest_survives_a_pod_without_ports():
    """The backend sends ports_mapping: null before ports are allocated."""
    manifest = display.build_manifest(_pod(ports=None))

    assert manifest["ports"]["mapping"] == {}
    assert manifest["ports"]["ssh_external"] is None
    assert manifest["ports"]["service_ports"] == []


def test_manifest_reports_gpu_from_executor_specs():
    """GPU model and driver come from the executor specs, not from the machine name."""
    manifest = display.build_manifest(_pod())

    assert manifest["gpu"]["model"] == "H100 80GB HBM3"
    assert manifest["gpu"]["driver_version"] == "535.104.05"
    assert manifest["gpu"]["count"] == 8


def test_manifest_gpu_count_is_the_pods_own_for_a_split_rental():
    """1 GPU rented of an 8×H100 host: `gpu.count` is the pod's billed count, the executor still says 8 (DAH-3073)."""
    pod = _pod()
    pod.gpu_count = 1

    manifest = display.build_manifest(pod)

    assert manifest["gpu"]["count"] == 1
    assert pod.executor.gpu_count == 8


def test_manifest_without_executor_still_describes_the_pod():
    """A pod whose executor the API omitted must not take the whole manifest down."""
    manifest = display.build_manifest(_pod(executor=None))

    assert manifest["gpu"] is None
    assert manifest["machine"] is None
    assert manifest["ports"]["ssh_external"] == 34567
    assert manifest["billing"]["price_per_hour"] is None


def test_manifest_bills_uptime_times_price():
    """Spend is uptime × price, so an agent can see what the pod has cost so far."""
    manifest = display.build_manifest(_pod())

    assert abs(manifest["pod"]["uptime_hours"] - UPTIME_HOURS) < 0.01
    assert abs(manifest["billing"]["spent_usd"] - UPTIME_HOURS * PRICE_PER_HOUR) < 0.1


def test_table_renders_a_pod_the_api_barely_described():
    """The human path must not crash where the JSON path degrades quietly."""
    bare_pod = _pod(ports=None, executor=None, template=None)
    bare_pod.status = None
    bare_pod.ssh_cmd = None
    bare_pod.created_at = ""

    table = display.build_manifest_table(display.build_manifest(bare_pod))

    assert table.row_count > 0


def test_describe_json_prints_the_manifest_on_stdout(monkeypatch):
    """`--json` output has to parse as-is — no spinner, no Rich decoration."""
    result = _run_describe(monkeypatch, [_pod()])

    assert result.exit_code == 0
    manifest = json.loads(result.stdout)
    assert manifest["pod"]["huid"] == "eager-wolf-aa"
    assert manifest["ports"]["ssh_external"] == 34567


def test_describe_resolves_a_pod_by_huid(monkeypatch):
    """The huid printed by `ps` is a valid handle for describe."""
    result = _run_describe(monkeypatch, [_pod()], target="eager-wolf-aa")

    assert result.exit_code == 0
    assert json.loads(result.stdout)["pod"]["id"] == "pod-uuid-1"


def test_describe_unknown_pod_exits_with_pod_not_found(monkeypatch):
    """A typo must fail with the pod-not-found code, not an empty success."""
    result = _run_describe(monkeypatch, [_pod()], target="no-such-pod-zz")

    assert result.exit_code == EXIT_POD_NOT_FOUND
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "pod_not_found"


def test_describe_does_not_blame_the_pod_id_for_an_api_failure(monkeypatch):
    """The SDK's own 404 text contains "not found" — that must not read as a bad pod id."""
    result = _run_describe(
        monkeypatch, [], error=LiumNotFoundError("Resource not found: /pods")
    )

    assert result.exit_code not in (0, EXIT_POD_NOT_FOUND)
    assert json.loads(result.stderr)["error"]["code"] != "pod_not_found"


# --- DAH-2932: last event, a pod that is gone, the node's disk ------------------------------------

LAST_EVENT = {
    "created_at": "2026-09-06T00:20:28",
    "event_type": "pod-lifecycle",
    "sub_event_type": "pod-lifecycle.status",
    "from_status": "REBOOT_PENDING",
    "to_status": "REBOOT_FAILED",
    "reason": "reboot_failed",
    "detail": "Container creation failed due to Failed create_container (failure_step: ssh_connect)",
    "error": None,
}
GONE_EVENTS = [
    {"created_at": "2026-09-05T20:17:56", "sub_event_type": "pod-create.success", "pod_name": "vault", "error": None},
    {"created_at": "2026-09-05T23:35:48", "sub_event_type": "pod-lifecycle.status", "pod_name": "vault",
     "from_status": "RUNNING", "to_status": "DELETING", "reason": "user_initiated", "detail": None, "error": None},
    {"created_at": "2026-09-05T23:36:32", "sub_event_type": "pod-lifecycle.status", "pod_name": "vault",
     "from_status": "DELETING", "to_status": "DELETED", "reason": "user_initiated", "detail": None, "error": None},
]
GONE_ID = "0942d1f7-08b4-4746-bafa-894c20631914"
BAD_DISK = {
    "read_only_mounts": ["/var/lib/docker"], "write_probe": "failed", "kernel_io_errors": 4,
    "kernel_io_error_lines": ["critical medium error, dev nvme1n1"], "block_io_errors": {}, "nvme_states": {},
    "smart": "unavailable",
}


def _executor_with_disk(health: dict) -> ExecutorInfo:
    executor = _executor()
    executor.specs = {**executor.specs, "disk_health": health}
    return executor


def test_manifest_carries_the_last_event_from_the_detail():
    manifest = display.build_manifest(_pod(), {"last_event": LAST_EVENT})

    assert manifest["last_event"] == {
        "at": "2026-09-06T00:20:28",
        "type": "pod-lifecycle.status",
        "from_status": "REBOOT_PENDING",
        "to_status": "REBOOT_FAILED",
        "reason": "reboot_failed",
        "detail": "Container creation failed due to Failed create_container (failure_step: ssh_connect)",
    }


def test_manifest_last_event_is_null_without_a_detail_or_an_event():
    assert display.build_manifest(_pod())["last_event"] is None
    assert display.build_manifest(_pod(), {"last_event": None})["last_event"] is None


def test_format_event_reads_as_one_line_with_status_reason_detail_and_time():
    line = display.format_event(display.event_view(LAST_EVENT))

    assert line == (
        "REBOOT_FAILED (reboot_failed) — Container creation failed due to Failed create_container "
        "(failure_step: ssh_connect) · 2026-09-06T00:20:28"
    )


def test_table_shows_the_last_event_when_there_is_one():
    table = display.build_manifest_table(display.build_manifest(_pod(), {"last_event": LAST_EVENT}))

    labels = [str(cell) for cell in table.columns[0]._cells]
    assert "Last event" in labels


def test_manifest_node_disk_is_unknown_without_a_probe():
    manifest = display.build_manifest(_pod())

    assert manifest["node_disk"] is None
    assert display.format_disk_health(manifest["node_disk"]) == "unknown (not probed)"


def test_manifest_node_disk_carries_the_readings_and_the_backend_verdict():
    pod = _pod(executor=_executor_with_disk(BAD_DISK))

    manifest = display.build_manifest(pod, {"executor": {"disk_health_ok": False}})

    assert manifest["node_disk"]["ok"] is False
    assert manifest["node_disk"]["read_only_mounts"] == ["/var/lib/docker"]
    assert manifest["node_disk"]["kernel_io_errors"] == 4
    assert display.format_disk_health(manifest["node_disk"]) == (
        "PROBLEM — read-only: /var/lib/docker; write probe failed; 4 kernel I/O errors"
    )


def test_format_disk_health_says_ok_for_a_clean_probe():
    clean = {**BAD_DISK, "read_only_mounts": [], "write_probe": "ok", "kernel_io_errors": 0, "kernel_io_error_lines": []}
    pod = _pod(executor=_executor_with_disk(clean))

    view = display.build_manifest(pod, {"executor": {"disk_health_ok": True}})["node_disk"]

    assert display.format_disk_health(view) == "ok"


def test_describe_json_includes_last_event_and_node_disk(monkeypatch):
    pod = _pod(executor=_executor_with_disk(BAD_DISK))

    result = _run_describe(
        monkeypatch, [pod], detail={"last_event": LAST_EVENT, "executor": {"disk_health_ok": False}}
    )

    assert result.exit_code == 0, result.output
    manifest = json.loads(result.stdout)
    assert manifest["last_event"]["reason"] == "reboot_failed"
    assert manifest["node_disk"]["ok"] is False


def test_describe_survives_a_detail_call_that_fails(monkeypatch):
    class _Failing(_FakeLium):
        def pod(self, pod_id):
            raise LiumNotFoundError("Resource not found")

    _Failing.pods = [_pod()]
    monkeypatch.setattr(describe_module, "Lium", _Failing)

    result = CliRunner().invoke(cli, ["describe", "my-pod", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["last_event"] is None


def test_describe_of_a_deleted_pod_by_id_prints_its_events_instead_of_not_found(monkeypatch):
    result = _run_describe(monkeypatch, [_pod()], target=GONE_ID, events={GONE_ID: GONE_EVENTS})

    assert result.exit_code == 0, result.output
    manifest = json.loads(result.stdout)
    assert manifest["pod"] == {
        "id": GONE_ID, "huid": None, "name": "vault", "status": "GONE", "created_at": None, "uptime_hours": None
    }
    assert manifest["last_event"]["to_status"] == "DELETED"
    assert manifest["last_event"]["reason"] == "user_initiated"
    assert [event["type"] for event in manifest["events"]] == [
        "pod-create.success", "pod-lifecycle.status", "pod-lifecycle.status"
    ]


def test_gone_manifest_headline_is_the_latest_lifecycle_event_not_the_delete_success_row():
    # a normal delete ends with `pod-delete.success`, which carries no to_status or reason
    events = GONE_EVENTS + [
        {"created_at": "2026-09-05T23:36:40", "sub_event_type": "pod-delete.success", "pod_name": "vault", "error": None}
    ]

    manifest = display.build_gone_manifest(GONE_ID, events)

    assert manifest["last_event"]["type"] == "pod-lifecycle.status"
    assert manifest["last_event"]["to_status"] == "DELETED"
    assert manifest["last_event"]["reason"] == "user_initiated"
    assert manifest["events"][-1]["type"] == "pod-delete.success"  # the log itself is complete


def test_gone_manifest_falls_back_to_the_last_event_without_a_lifecycle_row():
    events = [
        {"created_at": "2026-09-05T20:17:56", "sub_event_type": "pod-create.success", "pod_name": "vault", "error": None},
        {"created_at": "2026-09-05T20:18:10", "sub_event_type": "pod-delete.success", "pod_name": "vault", "error": None},
    ]

    manifest = display.build_gone_manifest(GONE_ID, events)

    assert manifest["last_event"]["type"] == "pod-delete.success"
    assert display.build_gone_manifest(GONE_ID, [])["last_event"] is None


def test_describe_of_a_deleted_pod_renders_a_table_for_humans(monkeypatch):
    monkeypatch.setattr(describe_module, "ensure_config", lambda: None)

    result = _run_describe(monkeypatch, [_pod()], target=GONE_ID, events={GONE_ID: GONE_EVENTS}, json_output=False)

    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())
    assert f"Pod {GONE_ID} is no longer listed" in output
    assert "DELETED (user_initiated)" in output
    assert "pod-create.success" in output


def test_describe_of_an_unknown_id_with_no_events_is_still_not_found(monkeypatch):
    result = _run_describe(monkeypatch, [_pod()], target=GONE_ID, events={})

    assert result.exit_code == EXIT_POD_NOT_FOUND
    assert json.loads(result.stderr)["error"]["code"] == "pod_not_found"


def test_describe_never_looks_up_events_for_a_name_or_huid(monkeypatch):
    class _Counting(_FakeLium):
        calls = 0

        def pod_events(self, pod_id):
            _Counting.calls += 1
            return []

    _Counting.pods = [_pod()]
    monkeypatch.setattr(describe_module, "Lium", _Counting)

    result = CliRunner().invoke(cli, ["describe", "no-such-pod-zz", "--json"])

    assert result.exit_code == EXIT_POD_NOT_FOUND
    assert _Counting.calls == 0
