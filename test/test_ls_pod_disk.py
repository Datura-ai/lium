"""``lium ls`` shows the disk a pod gets, not the host's free disk (support ticket 1112)."""

from lium.cli.ls.display import _specs_row
from lium.sdk import Config, Lium

GIB = 1024 * 1024  # KiB


def _info(**extra):
    row = {"id": "e1", "machine_name": "NVIDIA H100 80GB HBM3", "price_per_gpu": 2.0, "gpu_count": 8, **extra}
    return Lium(Config(api_key="test-key"))._dict_to_executor_info(row)


def test_pod_disk_is_the_free_gpus_share_of_the_free_disk_less_the_reserve():
    exe = _info(available_gpu_count=2, specs={"hard_disk": {"free": 820 * GIB, "total": 1000 * GIB}, "storage_limit_supported": True})

    row = _specs_row(exe)

    assert row["PodDisk"] == "200"  # (820 - 20) * 2 / 8
    assert row["Disk"] == "820"


def test_a_whole_free_node_gets_the_free_disk_less_the_reserve():
    assert _specs_row(_info(available_gpu_count=8, specs={"hard_disk": {"free": 820 * GIB}, "storage_limit_supported": True}))["PodDisk"] == "800"


def test_no_free_figure_shows_a_dash():
    assert _specs_row(_info(specs={"hard_disk": {}}))["PodDisk"] == "—"


def test_a_node_with_no_per_pod_limit_shows_all_its_free_disk():
    exe = _info(available_gpu_count=2, specs={"hard_disk": {"free": 820 * GIB}, "storage_limit_supported": False})

    assert _specs_row(exe)["PodDisk"] == "820"
