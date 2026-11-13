"""``lium ls`` shows the disk a pod gets, as the API serves it, not the host's free disk (support ticket 1112)."""

from lium.cli.ls.display import _specs_row
from lium.sdk import Config, Lium

GIB = 1024 * 1024  # KiB
POD_DISK = {"2": {"volume_gb": 400, "storage_gb": 200}, "8": {"volume_gb": 1600, "storage_gb": 800}}


def _info(**extra):
    row = {"id": "e1", "machine_name": "NVIDIA H100 80GB HBM3", "price_per_gpu": 2.0, "gpu_count": 8, **extra}
    return Lium(Config(api_key="test-key"))._dict_to_executor_info(row)


def test_pod_disk_is_what_the_api_serves_for_the_free_gpus():
    exe = _info(available_gpu_count=2, pod_disk=POD_DISK, specs={"hard_disk": {"free": 820 * GIB, "total": 1000 * GIB}})

    row = _specs_row(exe)

    assert row["PodDisk"] == "600"
    assert row["Disk"] == "820"


def test_a_whole_free_node_shows_the_whole_node_pod_disk():
    assert _specs_row(_info(available_gpu_count=8, pod_disk=POD_DISK, specs={"hard_disk": {"free": 820 * GIB}}))["PodDisk"] == "2400"


def test_a_node_with_no_per_pod_limit_shows_all_its_free_disk():
    exe = _info(available_gpu_count=2, pod_disk=None, specs={"hard_disk": {"free": 820 * GIB}})

    assert _specs_row(exe)["PodDisk"] == "820"


def test_a_nearly_full_node_without_pod_disk_converts_its_kib_figure():
    # 4 MiB free must not read as 4096 GiB
    exe = _info(available_gpu_count=2, specs={"hard_disk": {"free": 4096}})

    assert _specs_row(exe)["PodDisk"] == "0"


def test_no_free_figure_shows_a_dash():
    assert _specs_row(_info(specs={"hard_disk": {}}))["PodDisk"] == "—"
