"""Interconnect (NVLink / P2P) and the Download floor in the SDK, ``lium ls`` and ``lium describe``.

The backend reports how a node's GPUs are wired to each other (``interconnect`` / ``nvlink``, from the
validator's ``nvidia-smi topo`` run). Before that a renter could not tell an HGX board from eight PCIe
cards without peer-to-peer — hours of an 8-GPU bill. ``--min-download`` puts a floor on the Download
(Mbps) figure the node already reports.
"""

from __future__ import annotations

from types import SimpleNamespace
import json

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.ls import command as ls_command_module
from lium.cli.ls import display
from lium.cli.ls.validation import validate
from lium.cli.utils import EXIT_CONFIGURATION_ERROR
from lium.cli.describe.display import build_manifest, build_manifest_table
from lium.sdk import Config, ExecutorInfo, Lium, PodInfo

HGX = {
    "gpu_count": 8,
    "gpu_pairs": 28,
    "nvlink": True,
    "nvlink_links": 18,
    "nvlink_pairs": 28,
    "nvlink_active_links": 18,
    "pcie_class": None,
    "p2p": True,
    "p2p_pairs": 28,
    "p2p_ok_pairs": 28,
    "matrix": [["X" if i == j else "NV18" for j in range(8)] for i in range(8)],
}

PCIE = {
    "gpu_count": 8,
    "gpu_pairs": 28,
    "nvlink": False,
    "nvlink_links": None,
    "nvlink_pairs": 0,
    "nvlink_active_links": 0,
    "pcie_class": "SYS",
    "p2p": False,
    "p2p_pairs": 28,
    "p2p_ok_pairs": 0,
    "matrix": [["X" if i == j else "SYS" for j in range(8)] for i in range(8)],
}


def _executor_dict(executor_id: str, **extra) -> dict:
    """Minimal executor dict as returned by GET /executors, plus whatever the test adds."""
    base = {
        "id": executor_id,
        "machine_name": "NVIDIA H200 8x",
        "executor_ip_address": "203.0.113.4",
        "price_per_gpu": 3.25,
        "status": "available",
        "location": {"country": "United States", "country_code": "US"},
        "specs": {
            "gpu": {"count": 8, "details": [{"name": "H200", "capacity": 143771, "pcie_speed": 32}], "driver": "570.86"},
            "ram": {"total": 2097152},
            "hard_disk": {"total": 10485760},
            "network": {"upload_speed": 480.0, "download_speed": 300.0},
            "sysbox_runtime": False,
        },
        "effective_upload_speed_mbps": 480.0,
        "effective_download_speed_mbps": 300.0,
        "max_cuda_version": 12.8,
        "tier": "secure",
    }
    base.update(extra)
    return base


def _map(executor_dict: dict) -> ExecutorInfo:
    return Lium(Config(api_key="test-key"))._dict_to_executor_info(executor_dict)


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


# -- SDK mapping ---------------------------------------------------------------------------------------


def test_typed_backend_fields_are_carried_onto_executor_info():
    exe = _map(_executor_dict("hgx", interconnect=HGX, nvlink=True))

    assert exe.nvlink is True
    assert exe.interconnect["nvlink_links"] == 18
    assert exe.p2p is True
    assert exe.link == "NV18"


def test_raw_specs_are_read_when_the_backend_predates_the_typed_fields():
    """An older API returns the scrape's objects inside specs only; the CLI must still show them."""
    payload = _executor_dict("hgx")
    payload["specs"]["interconnect"] = HGX

    exe = _map(payload)

    assert exe.nvlink is True
    assert exe.link == "NV18"


def test_pcie_host_is_false_with_its_worst_class():
    exe = _map(_executor_dict("pcie", interconnect=PCIE, nvlink=False))

    assert exe.nvlink is False
    assert exe.p2p is False
    assert exe.link == "PCIe/SYS"


def test_unreported_topology_is_none_not_false():
    exe = _map(_executor_dict("old"))

    assert exe.nvlink is None
    assert exe.p2p is None
    assert exe.link is None
    assert exe.interconnect is None


def test_nvlink_without_a_link_count_still_reads_nvlink():
    exe = _map(_executor_dict("hgx", interconnect={**HGX, "nvlink_links": None}, nvlink=True))

    assert exe.link == "NVLink"


# -- Lium.ls filters -----------------------------------------------------------------------------------


def _fleet() -> list[dict]:
    return [
        _executor_dict("hgx-fast", interconnect=HGX, nvlink=True, effective_download_speed_mbps=7900.5),
        _executor_dict("hgx-slow", interconnect=HGX, nvlink=True, effective_download_speed_mbps=350.0),
        _executor_dict("pcie-fast", interconnect=PCIE, nvlink=False, effective_download_speed_mbps=8000.0),
        _executor_dict("unknown"),  # no topology; the fixture's Download is 300 Mbps
    ]


def _client_with(payload, captured: dict) -> Lium:
    client = Lium(Config(api_key="test-key"))

    def fake_request(method, endpoint, **kwargs):
        captured.update(kwargs.get("params") or {})
        return _Response(payload)

    client._request = fake_request  # type: ignore[method-assign]
    return client


def test_ls_nvlink_sends_the_server_parameter_and_filters_client_side():
    captured: dict = {}
    client = _client_with(_fleet(), captured)

    result = client.ls(nvlink=True)

    assert captured["nvlink"] == "true"
    assert [e.id for e in result] == ["hgx-fast", "hgx-slow"]


def test_ls_nvlink_excludes_nodes_with_no_verdict():
    """A renter who asked for NVLink must not be handed a node nobody has looked at."""
    client = _client_with(_fleet(), {})

    assert "unknown" not in [e.id for e in client.ls(nvlink=True)]


def test_ls_min_download_judges_the_download_figure():
    captured: dict = {}
    client = _client_with(_fleet(), captured)

    result = client.ls(min_download_mbps=1000)

    assert captured["min_download_mbps"] == 1000
    # hgx-slow reports 350 Mbps and "unknown" 300 Mbps: both under the floor
    assert [e.id for e in result] == ["hgx-fast", "pcie-fast"]


def test_ls_min_download_excludes_a_node_with_no_figure():
    """A node whose Download is unknown is not handed to a renter who asked for a floor."""
    fleet = _fleet() + [_executor_dict("silent", effective_download_speed_mbps=None)]
    client = _client_with(fleet, {})

    assert "silent" not in [e.id for e in client.ls(min_download_mbps=1)]
    assert "unknown" in [e.id for e in client.ls(min_download_mbps=250)]


def test_ls_filters_compose():
    client = _client_with(_fleet(), {})

    assert [e.id for e in client.ls(nvlink=True, min_download_mbps=1000)] == ["hgx-fast"]


def test_ls_without_the_new_arguments_sends_no_new_parameters():
    captured: dict = {}
    client = _client_with(_fleet(), captured)

    client.ls()

    assert "nvlink" not in captured
    assert "min_download_mbps" not in captured


# -- lium ls display -----------------------------------------------------------------------------------


def test_table_has_the_link_column_after_config():
    table, *_ = display.build_executors_table([_map(_executor_dict("hgx", interconnect=HGX, nvlink=True))], show_pareto=False)

    headers = [c.header for c in table.columns]
    assert "Link" in headers
    assert headers.index("Link") == headers.index("Config") + 1
    assert [h for h in headers if "Net" in h] == []  # no second speed column: Download (Mbps) is the figure


def test_link_cell_shows_the_class_and_dashes_when_unknown():
    assert "NV18" in display._link_display(_map(_executor_dict("hgx", interconnect=HGX, nvlink=True)))
    assert "PCIe/SYS" in display._link_display(_map(_executor_dict("pcie", interconnect=PCIE, nvlink=False)))
    assert display._link_display(_map(_executor_dict("old"))) == "—"


def test_compact_executor_carries_the_fields_for_agents():
    row = display.compact_executor(
        _map(_executor_dict("hgx", interconnect=HGX, nvlink=True)),
        is_pareto=False,
        index=1,
    )

    assert row["link"] == "NV18"
    assert row["nvlink"] is True
    assert row["p2p"] is True
    assert row["interconnect"]["nvlink_pairs"] == 28
    # existing fields unchanged, and no second pair of speed keys
    assert row["download_mbps"] == 300
    assert row["upload_mbps"] == 480
    assert [k for k in row if k.startswith("cdn_")] == []


def test_compact_executor_unknown_topology_is_null():
    row = display.compact_executor(_map(_executor_dict("old")), is_pareto=False, index=1)

    assert row["link"] is None
    assert row["nvlink"] is None
    assert row["interconnect"] is None


# -- lium ls command -----------------------------------------------------------------------------------


def _run_ls(monkeypatch, fleet: list[ExecutorInfo], *args: str):
    class _FakeLium:
        workspaces = SimpleNamespace(current=lambda: None)   # a server without workspaces: no context line (main since lium#152)

        def __init__(self, *a, **k):
            pass

        def unknown_gpu_type(self, gpu_type):
            # the marketplace knows H100 and H200; anything else is a typo
            return None if gpu_type.upper() in ("H100", "H200") else ["H100", "H200"]

        def ls(self, **kwargs):
            _FakeLium.kwargs = kwargs
            result = fleet
            if kwargs.get("gpu_type"):
                result = [e for e in result if kwargs["gpu_type"].upper() in (e.gpu_type or "").upper()]
            if kwargs.get("nvlink"):
                result = [e for e in result if e.nvlink is True]
            if kwargs.get("min_download_mbps") is not None:
                result = [e for e in result if (e.effective_download_speed_mbps or 0) >= kwargs["min_download_mbps"]]
            return result

    monkeypatch.setattr(ls_command_module, "Lium", _FakeLium)
    monkeypatch.setattr(ls_command_module, "store_executor_selection", lambda executors: None)
    return CliRunner().invoke(cli, ["ls", *args]), _FakeLium


def test_ls_nvlink_and_min_ingress_reach_the_sdk_and_the_json(monkeypatch):
    fleet = [_map(d) for d in _fleet()]

    result, fake = _run_ls(monkeypatch, fleet, "--nvlink", "--min-ingress", "1000", "--format", "json")

    assert result.exit_code == 0, result.output
    assert fake.kwargs["nvlink"] is True
    assert fake.kwargs["min_download_mbps"] == 1000.0
    rows = json.loads(result.output)
    assert [row["id"] for row in rows] == ["hgx-fast"]
    assert rows[0]["link"] == "NV18"
    assert rows[0]["download_mbps"] == 7900


def test_ls_min_download_is_the_same_option_as_min_ingress(monkeypatch):
    fleet = [_map(d) for d in _fleet()]

    result, fake = _run_ls(monkeypatch, fleet, "--min-download", "1000", "--format", "json")

    assert result.exit_code == 0, result.output
    assert fake.kwargs["min_download_mbps"] == 1000.0


def test_ls_explains_an_empty_result_caused_by_the_new_filters(monkeypatch):
    fleet = [_map(_executor_dict("old"))]

    result, _ = _run_ls(monkeypatch, fleet, "--nvlink")

    assert result.exit_code == 0, result.output
    assert "NVLink between every GPU pair" in result.output
    assert "--nvlink excludes nodes with no topology report yet" in result.output
    assert "--min-download" not in result.output
    assert "nvidia-smi topo -m" in result.output
    assert "rented out" not in result.output


def test_ls_names_a_gpu_typo_before_blaming_the_nvlink_filter(monkeypatch):
    # `--gpu H2000 --nvlink`: the empty result is the typo, not an NVLink shortage
    fleet = [_map(_executor_dict("hgx", interconnect=HGX, nvlink=True))]

    result, _ = _run_ls(monkeypatch, fleet, "--gpu", "H2000", "--nvlink")

    assert result.exit_code == 0, result.output
    assert "No GPU type matches 'H2000'" in result.output
    assert "NVLink between every GPU pair" not in result.output


def test_ls_explains_an_empty_result_caused_by_min_download(monkeypatch):
    # the fixture's Download is 300 Mbps: the hint names the column the floor was judged on
    fleet = [_map(_executor_dict("old"))]

    result, _ = _run_ls(monkeypatch, fleet, "--min-download", "1000")

    assert result.exit_code == 0, result.output
    assert "Download ≥ 1000 Mbps" in result.output
    assert "--min-download judges the Download (Mbps) column" in result.output
    assert "or lower the" in result.output  # the 80-column test terminal wraps the line after "the"
    assert "--nvlink" not in result.output
    assert "nvidia-smi" not in result.output  # the topology hint belongs to --nvlink
    assert "rented out" not in result.output


def test_ls_table_shows_the_link_column(monkeypatch):
    fleet = [_map(_executor_dict("hgx", interconnect=HGX, nvlink=True))]
    # Link is an optional column (priority 4): on CliRunner's 80-column pseudo-terminal the table drops
    # it, so the width is pinned wide enough for every column (as test_ls_width.py pins it); the header
    # order is asserted on the Table object in test_table_has_the_link_column_after_config
    monkeypatch.setattr(ls_command_module.console, "_width", 200)
    monkeypatch.setattr(ls_command_module.console, "_height", 25)

    result, _ = _run_ls(monkeypatch, fleet)

    assert result.exit_code == 0, result.output
    assert "Link" in result.output
    assert "NV18" in result.output
    assert "Net↓/↑" not in result.output


@pytest.mark.parametrize("value", ["0", "-5", "nan", "inf", "-inf"])
def test_ls_rejects_a_non_positive_min_download(monkeypatch, value):
    result, fake = _run_ls(monkeypatch, [], "--min-download", value)

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert "--min-download must be a positive, finite number" in result.output
    assert not hasattr(fake, "kwargs")  # refused locally: the SDK was never asked


def test_validate_accepts_a_positive_min_download():
    assert validate(None, None, None, None, min_download_mbps=1000.0) == (True, None)


# -- lium describe -------------------------------------------------------------------------------------


def _pod(executor: ExecutorInfo | None) -> PodInfo:
    return PodInfo(
        id="pod-1",
        name="tp8",
        status="RUNNING",
        huid="eager-wolf-aa",
        ssh_cmd="ssh root@pod.example -p 34567",
        ports={"22": 34567},
        created_at="2026-09-05T20:00:00Z",
        updated_at="2026-09-05T20:00:00Z",
        executor=executor,
        template={"name": "PyTorch", "docker_image": "daturaai/pytorch", "docker_image_tag": "x"},
        removal_scheduled_at=None,
        jupyter_installation_status=None,
        jupyter_url=None,
    )


def test_describe_manifest_carries_the_topology():
    manifest = build_manifest(_pod(_map(_executor_dict("hgx", interconnect=HGX, nvlink=True))))

    assert manifest["gpu"]["link"] == "NV18"
    assert manifest["gpu"]["nvlink"] is True
    assert manifest["gpu"]["p2p"] is True
    assert manifest["gpu"]["interconnect"]["matrix"][0][1] == "NV18"
    assert manifest["machine"]["download_mbps"] == 300.0
    assert manifest["machine"]["upload_mbps"] == 480.0
    assert [k for k in manifest["machine"] if k.startswith("cdn_")] == []


def test_describe_table_prints_the_nvlink_link_row():
    table = build_manifest_table(build_manifest(_pod(_map(_executor_dict("hgx", interconnect=HGX, nvlink=True)))))

    labels = [str(cell) for cell in table.columns[0]._cells]
    cells = [str(cell) for cell in table.columns[1]._cells]
    assert cells[labels.index("Link")] == "NVLink ×18, 28/28 pairs on NVLink, P2P ok"
    assert cells[labels.index("Topology")].startswith("GPU0") and "NV18" in cells[labels.index("Topology")]


def test_describe_link_row_treats_a_null_pair_count_as_zero():
    table = build_manifest_table(build_manifest(_pod(_map(_executor_dict("pcie", interconnect={**PCIE, "nvlink_pairs": None}, nvlink=False)))))

    labels = [str(cell) for cell in table.columns[0]._cells]
    cells = [str(cell) for cell in table.columns[1]._cells]
    assert "0/28 pairs on NVLink" in cells[labels.index("Link")]


def test_describe_table_prints_link_topology_and_net():
    table = build_manifest_table(build_manifest(_pod(_map(_executor_dict("pcie", interconnect=PCIE, nvlink=False)))))

    labels = [str(c._cells[i]) for c in table.columns[:1] for i in range(len(c._cells))]
    cells = [str(cell) for cell in table.columns[1]._cells]
    assert "Link" in labels and "Topology" in labels and "Net" in labels
    link_text = cells[labels.index("Link")]
    assert "PCIe (SYS)" in link_text
    assert "0/28 pairs on NVLink" in link_text
    assert "NCCL_P2P_DISABLE=1" in link_text
    topology = cells[labels.index("Topology")]
    assert topology.startswith("GPU0") and "SYS" in topology and topology.count("\n") == 7
    assert cells[labels.index("Net")] == "↓300 ↑480 Mbps (speed test)"


def test_describe_table_says_when_the_node_has_not_reported():
    table = build_manifest_table(build_manifest(_pod(_map(_executor_dict("old")))))

    labels = [str(cell) for cell in table.columns[0]._cells]
    cells = [str(cell) for cell in table.columns[1]._cells]
    assert "nvidia-smi topo -m" in cells[labels.index("Link")]
    assert "Topology" not in labels


def test_describe_single_gpu_pod_has_no_link_row():
    payload = _executor_dict("single")
    payload["specs"]["gpu"]["count"] = 1
    table = build_manifest_table(build_manifest(_pod(_map(payload))))

    labels = [str(cell) for cell in table.columns[0]._cells]
    assert "Link" not in labels
