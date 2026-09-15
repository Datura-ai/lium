"""`lium ls --country / --min-vram / --max-price / --tier`: narrow the catalogue before renting.

Picking a node meant paging through the whole table or filtering JSON by hand;
the fields were there but not as flags. These filters run client-side on the
same values `--format json` prints, so what an agent filters on and what a
person reads agree.
"""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.ls import command as ls_module
from lium.cli.ls import display, filters
from lium.cli.utils import EXIT_CONFIGURATION_ERROR
from lium.sdk import ExecutorInfo


def _executor(huid, gpu="H100", count=1, price_per_gpu=2.0, country="United States", code="US",
              vram_mib=81559, tier="secure", download=1000.0):
    return ExecutorInfo(
        id=f"id-{huid}", huid=huid, machine_name="m", gpu_type=gpu, gpu_count=count,
        price_per_hour=price_per_gpu * count, price_per_gpu=price_per_gpu,
        location={"country": country, "country_code": code, "city": "Somewhere"},
        specs={"gpu": {"details": [{"name": gpu, "capacity": vram_mib}]}, "ram": {"total": 2097152},
               "hard_disk": {"total": 10485760}},
        status="available", docker_in_docker=False, ip="1.2.3.4", tier=tier,
        effective_download_speed_mbps=download, effective_upload_speed_mbps=500.0,
    )


US_CHEAP = _executor("us-cheap", price_per_gpu=1.5, download=1200.0)
US_BIG = _executor("us-big", gpu="H200", vram_mib=143771, price_per_gpu=3.2, tier="spot")
NL = _executor("nl-node", country="The Netherlands", code="NL", price_per_gpu=2.4)
DE_SMALL = _executor("de-small", gpu="RTX 4090", vram_mib=24564, country="Germany", code="DE", price_per_gpu=0.4)
NODES = [US_CHEAP, US_BIG, NL, DE_SMALL]


@pytest.fixture
def fake_ls(monkeypatch):
    class _Lium:
        workspaces = SimpleNamespace(current=lambda: None)   # a server without workspaces: no context line

        def __init__(self, *a, **k):
            pass

        def ls(self, **kwargs):
            return list(NODES)

    monkeypatch.setattr(ls_module, "Lium", _Lium)
    monkeypatch.setattr(ls_module, "store_executor_selection", lambda executors: None)


def _huids(result):
    assert result.exit_code == 0, result.output
    return sorted(e["huid"] for e in json.loads(result.output))


# --- filter functions ------------------------------------------------------------------------

def test_parse_countries_splits_commas_and_lowercases():
    assert filters.parse_countries(("US,NL", " Germany ")) == ("us", "nl", "germany")


def test_country_matches_code_or_name_prefix():
    assert filters.keep(NL, filters.NodeFilters(countries=("nl",)))
    assert filters.keep(NL, filters.NodeFilters(countries=("the netherlands",)))
    assert filters.keep(NL, filters.NodeFilters(countries=("the neth",)))
    assert not filters.keep(NL, filters.NodeFilters(countries=("us", "de")))


def test_a_two_letter_input_is_a_code_not_a_name_prefix():
    denmark = _executor("dk", country="Denmark", code="DK")
    china = _executor("cn", country="China", code="CN")
    chile = _executor("cl", country="Chile", code="CL")
    switzerland = _executor("ch", country="Switzerland", code="CH")

    assert not filters.keep(denmark, filters.NodeFilters(countries=("de",)))
    assert filters.keep(DE_SMALL, filters.NodeFilters(countries=("de",)))
    assert not filters.keep(china, filters.NodeFilters(countries=("ch",)))
    assert not filters.keep(chile, filters.NodeFilters(countries=("ch",)))
    assert filters.keep(switzerland, filters.NodeFilters(countries=("ch",)))
    assert filters.keep(chile, filters.NodeFilters(countries=("chi",)))   # three letters: a name prefix again


def test_vram_is_read_from_the_first_gpu_capacity_as_the_table_shows_it():
    assert filters.vram_gb(US_BIG) == 140
    assert filters.vram_gb(US_CHEAP) == 80          # 81559 MiB, what the table prints
    assert filters.vram_gb(_executor("x", vram_mib=None)) is None


def test_min_vram_excludes_unknown_capacity():
    unknown = _executor("x", vram_mib=None)

    assert not filters.keep(unknown, filters.NodeFilters(min_vram_gb=1))
    assert filters.keep(unknown, filters.NodeFilters())


def test_describe_lists_every_active_filter():
    text = filters.describe(filters.NodeFilters(countries=("us", "germany"), min_vram_gb=80, max_price_per_gpu_hour=2, tier="spot"))

    assert text == "country US/germany, VRAM ≥ 80 GB, ≤ $2.00/GPU·h, tier spot"


# --- command -------------------------------------------------------------------------------

def test_ls_country_filter_accepts_codes_and_names(fake_ls):
    assert _huids(CliRunner().invoke(cli, ["ls", "--country", "NL", "--json"])) == ["nl-node"]
    assert _huids(CliRunner().invoke(cli, ["ls", "--country", "us,de", "--json"])) == ["de-small", "us-big", "us-cheap"]
    assert _huids(CliRunner().invoke(cli, ["ls", "--country", "germany", "--country", "NL", "--json"])) == ["de-small", "nl-node"]


def test_ls_min_vram(fake_ls):
    assert _huids(CliRunner().invoke(cli, ["ls", "--min-vram", "80", "--json"])) == ["nl-node", "us-big", "us-cheap"]
    assert _huids(CliRunner().invoke(cli, ["ls", "--min-vram", "100", "--json"])) == ["us-big"]


def test_ls_max_price_is_per_gpu_hour(fake_ls):
    assert _huids(CliRunner().invoke(cli, ["ls", "--max-price", "2", "--json"])) == ["de-small", "us-cheap"]


def test_a_node_without_a_price_never_passes_max_price():
    """The SDK stores a missing price_per_gpu as 0; that is unknown, not free."""
    unpriced = _executor("free", price_per_gpu=0)

    assert not filters.keep(unpriced, filters.NodeFilters(max_price_per_gpu_hour=100))
    assert filters.keep(unpriced, filters.NodeFilters())


def test_ls_tier(fake_ls):
    assert _huids(CliRunner().invoke(cli, ["ls", "--tier", "spot", "--json"])) == ["us-big"]


def test_ls_filters_combine(fake_ls):
    result = CliRunner().invoke(cli, ["ls", "--country", "US", "--min-vram", "80", "--max-price", "2", "--format", "json"])

    assert _huids(result) == ["us-cheap"]


def test_ls_json_flag_is_an_alias_and_carries_the_filterable_fields(fake_ls):
    result = CliRunner().invoke(cli, ["ls", "--json"])

    rows = json.loads(result.output)
    row = next(r for r in rows if r["huid"] == "nl-node")
    assert row["country"] == "The Netherlands" and row["country_code"] == "NL" and row["city"] == "Somewhere"
    assert row["vram_gb"] == 80 and row["price_per_gpu_hour"] == 2.4 and row["tier"] == "secure"
    assert result.output == CliRunner().invoke(cli, ["ls", "--format", "json"]).output


def test_ls_no_match_names_the_filters_and_exits_zero(fake_ls):
    result = CliRunner().invoke(cli, ["ls", "--gpu", "H100", "--min-vram", "500"])

    assert result.exit_code == 0, result.output
    assert "No nodes match VRAM ≥ 500 GB" in result.output
    assert "lium ls --gpu H100" in result.output


def test_ls_no_match_in_json_is_an_empty_list(fake_ls):
    result = CliRunner().invoke(cli, ["ls", "--country", "ZZ", "--json"])

    assert result.exit_code == 0 and json.loads(result.output) == []


@pytest.mark.parametrize("args", [["--min-vram", "0"], ["--max-price", "-1"]])
def test_ls_rejects_non_positive_thresholds(fake_ls, args):
    assert CliRunner().invoke(cli, ["ls", *args]).exit_code == EXIT_CONFIGURATION_ERROR


def test_ls_table_explains_the_star(fake_ls):
    result = CliRunner().invoke(cli, ["ls"])

    assert result.exit_code == 0, result.output
    # short fragments: Rich wraps the dim lines at the console width
    assert "default order: cheapest $/GPU·h first" in result.output
    assert "★ = no other node beats it" in result.output
    assert "a 10% faster download wins outright" in result.output


def test_compact_executor_has_country_code_and_city():
    row = display.compact_executor(NL, True, 1)

    assert row["country_code"] == "NL" and row["city"] == "Somewhere" and row["machine_name"] == "m"
