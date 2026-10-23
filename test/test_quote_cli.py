"""`lium quote` and `Lium.quote`: a job's cost on a node before renting it."""

import json

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.quote import command as quote_module
from lium.cli.utils import SIGNUP_NUDGE
from lium.sdk import Config, Lium, LiumNotFoundError

NODE = "3f2b9c1e-8a4d-4b6e-9f10-2c3d4e5f6a7b"
QUOTE = {
    "executor_id": NODE, "gpu_count": 8, "hours": 2.0, "price_per_hour": 16.0, "total_usd": 32.05,
    "pod_balance_floor_usd": 16.0,
    "lines": [
        {"item": "compute", "unit": "hour", "rate_usd": 16.0, "quantity": 2.0, "amount_usd": 32.0, "note": "per second"},
        {"item": "volume", "unit": "GB-hour", "rate_usd": 0.000125, "quantity": 400.0, "amount_usd": 0.05, "note": ""},
        {"item": "egress", "unit": "GB", "rate_usd": None, "quantity": 0.0, "amount_usd": None, "note": "at AWS cost"},
    ],
}


class _Response:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class _Seen(list):
    client = None


@pytest.fixture
def requests_seen(monkeypatch):
    seen = _Seen()
    client = Lium(Config(api_key="test"))

    def request(method, path, **kwargs):
        seen.append((method, path, kwargs.get("params")))
        return _Response(QUOTE)

    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr(quote_module, "Lium", lambda: client)
    seen.client = client
    return seen


def _node(gpu_count=8, available_gpu_count=None):
    return type("N", (), {"id": NODE, "gpu_count": gpu_count, "available_gpu_count": available_gpu_count})()


def test_quote_by_uuid_with_gpus_asks_the_api_once_with_the_job(requests_seen, monkeypatch):
    monkeypatch.setattr(requests_seen.client, "get_executor", lambda ref: pytest.fail("no lookup expected"))

    out = CliRunner().invoke(
        cli, ["quote", NODE, "--hours", "2", "--gpus", "8", "--volume-gb", "200", "--format", "json"]
    )

    assert out.exit_code == 0, out.output
    assert requests_seen == [
        ("GET", f"/executors/{NODE}/quote", {"hours": 2.0, "volume_gb": 200.0, "backup_gb": 0.0, "gpu_count": 8})
    ]
    assert json.loads(out.output)["total_usd"] == 32.05


def test_quote_by_uuid_without_gpus_leaves_the_count_to_the_server_and_reads_no_listing(requests_seen, monkeypatch):
    # millerserhii on #337 (5c7a03f): a UUID is taken as given; the server quotes the node's free GPUs,
    # what `lium up NODE` rents (lium-platform#1184)
    monkeypatch.setattr(requests_seen.client, "get_executor", lambda ref: pytest.fail("no lookup expected"))

    out = CliRunner().invoke(cli, ["quote", NODE, "--hours", "2", "--format", "json"])

    assert out.exit_code == 0, out.output
    assert requests_seen == [("GET", f"/executors/{NODE}/quote", {"hours": 2.0, "volume_gb": 0.0, "backup_gb": 0.0})]


def test_quote_by_huid_resolves_the_listed_node_and_passes_gpus(requests_seen, monkeypatch):
    monkeypatch.setattr(requests_seen.client, "get_executor", lambda ref: _node())

    out = CliRunner().invoke(cli, ["quote", "cosmic-hawk-f2", "--hours", "1", "--gpus", "1"])

    assert out.exit_code == 0, out.output
    assert requests_seen[0][1] == f"/executors/{NODE}/quote" and requests_seen[0][2]["gpu_count"] == 1
    assert "Total $32.0500 (egress not included)" in out.output
    assert "$0.000125/GB-hour" in out.output and "$0.0001/" not in out.output


def test_quote_of_an_unlisted_huid_is_not_found_without_a_request(monkeypatch):
    client = Lium(Config(api_key="test"))
    monkeypatch.setattr(client, "get_executor", lambda ref: None)
    monkeypatch.setattr(client, "_request", lambda *a, **k: pytest.fail("no request expected"))

    with pytest.raises(LiumNotFoundError):
        client.quote("no-such-node", hours=1)


def test_quote_refuses_non_positive_hours_before_asking(requests_seen):
    out = CliRunner().invoke(cli, ["quote", NODE, "--hours", "0"])

    assert out.exit_code == 2 and requests_seen == []


def test_quote_without_an_api_key_prices_the_node_and_says_how_to_rent(monkeypatch):
    # millerserhii on #337 (dc7153c): the quote route is public, so a visitor with no key can price a job
    seen = []

    class KeylessLium(Lium):
        def __init__(self, config=None, **kwargs):
            if config is None:
                raise ValueError("No API key found. Set LIUM_API_KEY")
            super().__init__(config=config, **kwargs)

        def _request(self, method, path, **kwargs):
            seen.append((method, path, self.config.api_key))
            return _Response(QUOTE)

    monkeypatch.setattr(quote_module, "Lium", KeylessLium)

    out = CliRunner().invoke(cli, ["quote", NODE, "--hours", "2"], env={"COLUMNS": "400"})

    assert out.exit_code == 0, out.output
    assert seen == [("GET", f"/executors/{NODE}/quote", "")]
    assert "Total $32.0500" in out.output and SIGNUP_NUDGE in out.output


@pytest.mark.parametrize("rate,shown", [(0.0000288, "$0.0000288"), (0.00005, "$0.00005"), (0.000125, "$0.000125"), (16.0, "$16.00"), (1234.5, "$1,234.50")])
def test_rate_keeps_fractions_of_a_cent(rate, shown):
    from lium.cli.quote.command import _rate

    assert _rate(rate) == shown


def test_sdk_quote_returns_a_typed_quote(requests_seen):
    from lium.sdk import JobQuote, JobQuoteLine

    quote = requests_seen.client.quote(NODE, hours=2)

    assert isinstance(quote, JobQuote) and quote.total_usd == 32.05 and quote.allocation is None
    assert isinstance(quote.lines[2], JobQuoteLine) and quote.lines[2].rate_usd is None
    assert quote.to_dict()["lines"][0] == QUOTE["lines"][0]


def test_signed_in_quote_prints_the_balance_the_rent_needs_with_open_pods(requests_seen, monkeypatch):
    # millerserhii on lium-platform#1548: with an open $8/h pod, another $8/h pod needs more than $4, not this pod's $2
    monkeypatch.setitem(QUOTE, "pod_balance_floor_usd", 2.0)
    monkeypatch.setitem(QUOTE, "rent_balance_required_usd", 4.0)

    out = CliRunner().invoke(cli, ["quote", NODE, "--hours", "2", "--gpus", "8"])

    assert out.exit_code == 0, out.output
    assert "more than $4.0000" in out.output and "$2.0000" not in out.output


def test_quote_without_the_required_balance_prints_only_this_pods_floor(requests_seen, monkeypatch):
    # an older backend omits rent_balance_required_usd and gates on this pod's floor alone: claim nothing about open pods
    monkeypatch.setitem(QUOTE, "pod_balance_floor_usd", 2.0)
    monkeypatch.setitem(QUOTE, "rent_balance_required_usd", None)

    out = CliRunner().invoke(cli, ["quote", NODE, "--hours", "2", "--gpus", "8"])

    assert out.exit_code == 0, out.output
    assert "This pod's balance floor: $2.0000" in out.output and "open pods" not in out.output


def test_exempt_quote_says_no_balance_floor_applies(requests_seen, monkeypatch):
    # the server sends 0 for an account the floor exempts: a zero balance can rent
    monkeypatch.setitem(QUOTE, "pod_balance_floor_usd", 2.0)
    monkeypatch.setitem(QUOTE, "rent_balance_required_usd", 0.0)

    out = CliRunner().invoke(cli, ["quote", NODE, "--hours", "2", "--gpus", "8"])

    assert out.exit_code == 0, out.output
    assert "No balance floor applies to this account" in out.output and "more than" not in out.output
