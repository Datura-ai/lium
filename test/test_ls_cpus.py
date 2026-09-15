"""DAH-2981: CPU count in `lium ls` and `--min-cpus` on `ls` and `up`.

The API returns ``specs.cpu.count`` for every node, but the table did not show
it and nothing could filter on it; a renter found out a 1× H100 node had 20
vCPUs only after renting it.
"""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.ls import command as ls_command_module
from lium.cli.ls import display
from lium.cli.up import validation as up_validation
from lium.cli.up.actions import ResolveExecutorAction
from lium.sdk import Config, Lium
from lium.sdk.exceptions import LiumError


def _executor_dict(executor_id: str, cpu_count) -> dict:
    d = {
        "id": executor_id,
        "machine_name": "NVIDIA H100 80GB HBM3",
        "executor_ip_address": "1.2.3.4",
        "price_per_gpu": 2.5,
        "status": "available",
        "location": {"country": "US"},
        "effective_download_speed_mbps": 1000,
        "effective_upload_speed_mbps": 1000,
        "specs": {
            "gpu": {"count": 1, "details": [{"name": "H100", "capacity": 81920}]},
            "ram": {"total": 2097152},
            "hard_disk": {"total": 10485760},
            "cpu": {"count": cpu_count, "model": "AMD EPYC 9354"},
        },
    }
    if cpu_count is None:
        del d["specs"]["cpu"]
    return d


def _map(executor_dict: dict):
    return Lium(Config(api_key="test-key"))._dict_to_executor_info(executor_dict)


def test_cpu_count_comes_from_specs():
    assert _map(_executor_dict("e1", 20)).cpu_count == 20
    assert _map(_executor_dict("e2", "24")).cpu_count == 24


def test_cpu_count_is_none_when_absent_or_unparseable():
    assert _map(_executor_dict("e3", None)).cpu_count is None
    assert _map(_executor_dict("e4", "many")).cpu_count is None


def test_ls_min_cpus_keeps_enough_and_drops_unknown(monkeypatch):
    client = Lium(Config(api_key="test-key"))
    rows = [_executor_dict("small", 20), _executor_dict("big", 64), _executor_dict("unknown", None)]
    monkeypatch.setattr(client, "_request", lambda *a, **k: SimpleNamespace(json=lambda: rows))

    assert [e.id for e in client.ls(min_cpus=24)] == ["big"]
    assert [e.id for e in client.ls()] == ["small", "big", "unknown"]


def test_table_has_cpus_column_with_the_count():
    exe = _map(_executor_dict("e1", 20))
    table, *_ = display.build_executors_table([exe], show_pareto=False)

    headers = [c.header for c in table.columns]
    assert "CPUs" in headers
    assert table.columns[headers.index("CPUs")]._cells == ["20"]


def test_compact_executor_carries_cpu_count():
    assert display.compact_executor(_map(_executor_dict("e1", 20)), is_pareto=False, index=1)["cpu_count"] == 20
    assert display.compact_executor(_map(_executor_dict("e2", None)), is_pareto=False, index=1)["cpu_count"] is None


def test_ls_min_cpus_reaches_the_sdk_and_rejects_zero(monkeypatch):
    seen: dict = {}

    class _FakeLium:
        def __init__(self, *args, **kwargs):
            pass

        def ls(self, **kwargs):
            seen.update(kwargs)
            return [_map(_executor_dict("big", 64))]

    monkeypatch.setattr(ls_command_module, "Lium", _FakeLium)
    monkeypatch.setattr(ls_command_module, "store_executor_selection", lambda executors: None)

    result = CliRunner().invoke(cli, ["ls", "--min-cpus", "24", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert seen["min_cpus"] == 24
    assert json.loads(result.output)[0]["cpu_count"] == 64

    result = CliRunner().invoke(cli, ["ls", "--min-cpus", "0"])
    assert result.exit_code == 2
    assert "--min-cpus must be a positive integer" in result.output


def test_up_passes_min_cpus_to_the_listing_and_names_it_when_nothing_matches(monkeypatch):
    seen: dict = {}

    class _FakeLium:
        def supports(self, feature):
            return False  # an older backend: the CLI lists the fleet and filters it here

        def ls(self, **kwargs):
            seen.update(kwargs)
            return []

        def unknown_gpu_type(self, gpu_short):
            return None  # H100 is a known type: the miss is the CPU floor, not the GPU name

    monkeypatch.setattr("lium.cli.ls.command.ls_store_executor", lambda **kwargs: [])
    result = ResolveExecutorAction().execute({"lium": _FakeLium(), "gpu": "H100", "min_cpus": 24})

    assert seen["min_cpus"] == 24
    assert not result.ok
    assert "min CPUs=24" in result.error


def test_up_sends_min_cpus_in_the_spec_when_the_backend_picks():
    """On a rent_by_spec backend the fleet is never listed, so the CPU floor has to travel in the
    spec the server matches — for the dry run and for the rent that follows."""
    seen: dict = {}

    class _SpecLium:
        def supports(self, feature):
            return feature == "rent_by_spec"

        def ls(self, **kwargs):
            raise AssertionError("the fleet must not be listed when the backend can pick")

        def rent(self, **kwargs):
            seen.update(kwargs)
            raise LiumError("No node matches gpu_type=H100 min_cpus=24")

    result = ResolveExecutorAction().execute({"lium": _SpecLium(), "gpu": "H100", "min_cpus": 24})

    assert seen["min_cpus"] == 24
    assert seen["dry_run"] is True
    assert not result.ok


@pytest.mark.parametrize(
    ("executor_id", "min_cpus", "expected_error"),
    [
        (None, 32, ""),
        ("node-1", 32, "Cannot use filters (--gpu, --country, --min-cpus) when specifying a node ID"),  # -c with a node ID is the GPU count (DAH-3074)
        # 0 is falsy: it must reach the positivity check, not read as "no filter given"
        (None, 0, "--min-cpus must be a positive integer"),
        ("node-1", 0, "--min-cpus must be a positive integer"),
        (None, -4, "--min-cpus must be a positive integer"),
    ],
)
def test_up_validation_treats_min_cpus_as_a_filter(executor_id, min_cpus, expected_error):
    valid, error = up_validation.validate(executor_id, None, None, None, None, None, min_cpus=min_cpus)

    assert valid is (expected_error == "")
    assert error == expected_error
