"""DAH-3073: `lium ps` shows the GPUs the pod is billed for, not the whole host.

``PodInfo.executor`` describes the host (DAH-2877 keeps it that way); for a
GPU-split rental only ``PodInfo.gpu_count`` says what the renter has.
"""

from lium.sdk import ExecutorInfo, PodInfo
from lium.cli.ps import display as ps_display


def _host_3x3090() -> ExecutorInfo:
    return ExecutorInfo(
        id="e0a7c1e2-6c2e-4d3d-9d8b-0f1a2b3c4d5e",
        huid="brave-otter-11",
        machine_name="NVIDIA GeForce RTX 3090",
        gpu_type="RTX3090",
        gpu_count=3,
        price_per_hour=0.18,
        price_per_gpu=0.18,
        location={"country": "Germany", "country_code": "DE"},
        specs={"gpu": {"count": 3, "details": [{"name": "NVIDIA GeForce RTX 3090", "capacity": 24576}] * 3}},
        status="running",
        docker_in_docker=False,
        ip="203.0.113.10",
    )


def _pod(gpu_count):
    return PodInfo(
        id="d7b3e3b2-0f7c-4f7e-9c3c-0b3f1a2e9a01",
        name="sx-ctl",
        status="RUNNING",
        huid="eager-wolf-aa",
        ssh_cmd="ssh root@pod.invalid -p 2222",
        ports={"22": 2222},
        created_at="2026-09-07T01:51:40Z",
        updated_at="2026-09-07T01:52:00Z",
        executor=_host_3x3090(),
        template={"name": "Pytorch"},
        removal_scheduled_at=None,
        jupyter_installation_status=None,
        jupyter_url=None,
        gpu_count=gpu_count,
    )


def test_ps_json_shows_the_pods_own_gpu_count_for_a_split_rental():
    row = ps_display.compact_pod(_pod(gpu_count=1))

    assert row["gpu_count"] == 1
    assert row["config"] == "RTX3090"


def test_ps_json_falls_back_to_the_host_count_when_the_pod_sent_none():
    row = ps_display.compact_pod(_pod(gpu_count=None))

    assert row["gpu_count"] == 3
    assert row["config"] == "3×RTX3090"


def test_ps_table_config_column_uses_the_pods_own_gpu_count():
    table, _ = ps_display.build_pods_table([_pod(gpu_count=1), _pod(gpu_count=None)], show_index=False)

    config = next(column for column in table.columns if column.header == "Config")
    assert [str(cell) for cell in config._cells] == ["RTX3090", "3×RTX3090"]
