"""`lium ls` Disk column shows the host's FREE disk, not its total.

Stress matrix 7 Sep 2026: 27 of 88 rented nodes advertised 524 GB "Disk" while the pod's /workspace was
a 125–155 GB filesystem — the backend hands a pod (free − overhead) × its GPU share, split 2/3 into the
/root volume and 1/3 into container storage (daos/pod.py calc_volume_storage_limit). The total is the one
number a renter never gets.
"""

from __future__ import annotations

from lium.sdk import Config, Lium
from lium.cli.ls import display


def _executor(free_kib, total_kib):
    d = {
        "id": "e1", "machine_name": "NVIDIA H100 80GB HBM3", "executor_ip_address": "1.2.3.4", "price_per_gpu": 1.2,
        "location": {"country": "US"},
        "specs": {"gpu": {"count": 1, "details": [{"name": "H100", "capacity": 81559, "pcie_speed": 32000}], "driver": "580.178.04"},
                  "ram": {"total": 247409624}, "hard_disk": {"free": free_kib, "used": total_kib - free_kib, "total": total_kib}},
    }
    return Lium(Config(api_key="test-key"))._dict_to_executor_info(d)


def test_disk_column_is_free_space():
    row = display._specs_row(_executor(free_kib=385893096, total_kib=524032000))   # the live 1×H100 node

    assert row["Disk"] == "368"          # 386 GB free -> GiB
    assert row["DiskTotal"] == "500"


def test_json_keeps_disk_gb_as_free_and_adds_total():
    j = display.compact_executor(_executor(free_kib=385893096, total_kib=524032000), is_pareto=True, index=1)

    assert j["disk_gb"] == 368
    assert j["disk_total_gb"] == 500


def test_missing_hard_disk_is_dash():
    exe = _executor(free_kib=1, total_kib=2)
    exe.specs["hard_disk"] = {}

    row = display._specs_row(exe)

    assert row["Disk"] == "—" and row["DiskTotal"] == "—"
