"""DAH-3053: `lium ls` asks the API for a listing, not the fleet's full scrape, and the CLI
starts without the provider stack.

Measured 7 Sep 2026 on a Mac: `lium ls` 3.0–3.9 s wall for 0.2 s of CPU. The SDK sent
`size=1000`, which the backend treated as a paged request and rebuilt the listing per call
(DAH-3052), then downloaded 1.25 MB of specs for a 13-column table; importing the CLI pulled
in the provider portal client (pydantic models, JWT) that `ls`/`ps`/`up` never call.
"""

import subprocess
import sys

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.ls.display import compact_executor
from lium.sdk import Config, Lium

# One row of `GET /executors?view=summary` (lium-platform DAH-3052 `LISTING_VIEWS["summary"]`).
SUMMARY_ROW = {
    "id": "ad83de56-8a04-4ab1-ac5a-4ab007b4e2b3",
    "machine_name": "NVIDIA H200",
    "price_per_gpu": 2.5,
    "gpu_count": 8,
    "available_gpu_count": 8,
    "min_gpu_count_for_rental": None,
    "tier": "secure",
    "reliability_score": 98.5,
    "max_cuda_version": 12.8,
    "is_bookmarked": False,
    "effective_upload_speed_mbps": 1200.5,
    "effective_download_speed_mbps": 2400.1,
    "location": {"country": "The Netherlands", "country_code": "NL", "city": "Amsterdam", "lat": 52.352, "lon": 4.9392},
    "specs": {
        "gpu": {
            "count": 8,
            "driver": "570.86.15",
            "details": [{"name": "NVIDIA H200", "capacity": 143771, "pcie_speed": 32, "memory_speed": 2619, "graphics_speed": 1980}],
        },
        "cpu": {"count": 192, "model": "AMD EPYC 9654"},
        "ram": {"total": 1_585_000_000},
        "hard_disk": {"total": 7_000_000_000, "free": 6_500_000_000},
        "network": {"upload_speed": 1200.5, "download_speed": 2400.1},
        "available_port_count": 30,
        "sysbox_runtime": True,
    },
}


def _capture_params(client, rows):
    captured = {}

    class Response:
        def json(self):
            return rows

    def fake_request(method, endpoint, **kwargs):
        captured.update(method=method, endpoint=endpoint, params=kwargs.get("params"))
        return Response()

    client._request = fake_request
    return captured


def test_ls_asks_for_the_summary_view_and_no_page_size():
    client = Lium(Config(api_key="test"))
    captured = _capture_params(client, [SUMMARY_ROW])

    client.ls()

    assert captured["endpoint"] == "/executors"
    assert captured["params"] == {"view": "summary"}


def test_ls_can_still_ask_for_the_full_scrape():
    client = Lium(Config(api_key="test"))
    captured = _capture_params(client, [SUMMARY_ROW])

    client.ls(view="full", gpu_count=8)

    assert captured["params"] == {"view": "full", "gpu_count_gte": 8, "gpu_count_lte": 8}


def test_a_summary_row_carries_every_field_the_table_and_json_render():
    # a contract guard, green on main too: it fails when a field the table or the JSON reads leaves the summary view
    client = Lium(Config(api_key="test"))
    _capture_params(client, [SUMMARY_ROW])

    [executor] = client.ls()
    row = compact_executor(executor, is_pareto=True, index=1)

    assert (executor.gpu_type, executor.gpu_count, executor.price_per_gpu, executor.price_per_hour) == ("H200", 8, 2.5, 20.0)
    assert (executor.gpu_model, executor.driver_version, executor.docker_in_docker) == ("NVIDIA H200", "570.86.15", True)
    assert (executor.download_speed, executor.upload_speed, executor.tier, executor.max_cuda_version) == (2400.1, 1200.5, "secure", 12.8)
    # the `lium ls --format json` object, name for name as README.md documents it
    assert set(row) == {
        "index", "id", "huid", "config", "gpu_type", "gpu_count", "price_per_gpu_hour", "price_per_hour", "country",
        "vram_gb", "ram_gb", "disk_gb", "upload_mbps", "download_mbps", "available_ports", "docker_in_docker",
        "is_pareto", "max_cuda_version", "tier",
    }
    assert None not in row.values()
    assert (row["country"], row["vram_gb"], row["ram_gb"], row["disk_gb"], row["available_ports"]) == ("The Netherlands", 140, 1512, 6676, 30)


LAZY_MODULES = ("paramiko", "lium.sdk._hostkeys", "lium.cli.provider", "lium.provider.client", "jwt")


def test_importing_the_cli_loads_neither_paramiko_nor_the_provider_stack():
    # a fresh interpreter: importing the CLI loads none of these; `lium provider --help` then loads the group
    probe = (
        "import sys;"
        "from click.testing import CliRunner;"
        "from lium.cli.cli import cli;"
        f"lazy = {LAZY_MODULES!r};"
        "print(sorted(m for m in sys.modules if m in lazy));"
        "result = CliRunner().invoke(cli, ['provider', '--help']);"
        "print(result.exit_code, 'lium.cli.provider' in sys.modules)"
    )

    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    after_import, after_provider_help = result.stdout.strip().splitlines()
    assert after_import == "[]", f"importing the CLI pulled in: {after_import}"
    assert after_provider_help == "0 True"


def test_paramiko_and_the_host_key_policies_still_resolve_on_the_client_module(monkeypatch):
    # DAH-2904's tests read these four names off lium.sdk.client and patch attributes of its `paramiko`
    import paramiko

    from lium.sdk import _hostkeys
    from lium.sdk import client as sdk_client

    assert sdk_client.paramiko.SSHClient is paramiko.SSHClient
    assert sdk_client.host_key_fingerprint is _hostkeys.host_key_fingerprint
    assert sdk_client._PinOnFirstUsePolicy is _hostkeys._PinOnFirstUsePolicy
    assert sdk_client._InsecureAcceptPolicy is _hostkeys._InsecureAcceptPolicy
    assert issubclass(sdk_client._PinOnFirstUsePolicy, sdk_client.paramiko.MissingHostKeyPolicy)

    sentinel = object()
    monkeypatch.setattr(sdk_client.paramiko, "SSHClient", sentinel)
    assert paramiko.SSHClient is sentinel  # a patch through the client module lands on paramiko itself

    with pytest.raises(AttributeError):
        sdk_client.no_such_name


def test_provider_group_is_listed_by_help_and_answers_its_own_help():
    # green on main too: it guards that LazyGroup lists and dispatches the group exactly as click.Group did
    runner = CliRunner()

    listing = runner.invoke(cli, ["--help"])
    provider_help = runner.invoke(cli, ["provider", "--help"])

    assert listing.exit_code == 0 and "provider" in listing.output
    assert provider_help.exit_code == 0 and "Usage:" in provider_help.output and "node" in provider_help.output
