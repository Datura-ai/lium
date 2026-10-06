"""`--gpu 4090` (a bare model number) must find RTX 4090 nodes, and an unknown type must say so.

Regression for the report "the previous call with --gpu 4090 returned empty due to a matching
bug": `_resolve_machine_name` compared the typed text with the extracted type verbatim, so
"4090" matched nothing, the API was queried with machine_names=4090, and the CLI reported
"All 4090 GPUs are currently rented out" — a wrong diagnosis for a spelling problem.
"""

import time
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.ls import command as ls_command
from lium.cli.up import actions as up_actions
from lium.sdk import Config, Lium, LiumServerError
from lium.sdk.utils import gpu_short_matches

MACHINES = [
    "NVIDIA GeForce RTX 4090", "NVIDIA GeForce RTX 5090", "NVIDIA RTX 6000 Ada Generation",
    "NVIDIA RTX PRO 6000 Blackwell Server Edition", "NVIDIA RTX A6000", "NVIDIA H100 80GB HBM3",
    "NVIDIA H100 PCIe", "NVIDIA A100-SXM4-80GB", "NVIDIA L40S", "NVIDIA B200",
    "NVIDIA TITAN V", "NVIDIA GeForce GTX 1660 SUPER",  # untyped by the extractor: last word
]


@pytest.mark.parametrize(
    "typed,gpu_type,expected",
    [
        ("4090", "RTX4090", True),
        ("rtx 4090", "RTX4090", True),
        ("RTX-4090", "RTX4090", True),
        ("rtx4090", "RTX4090", True),
        ("5090", "RTX5090", True),
        ("3090", "RTX4090", False),
        ("6000", "RTX6000", True),
        ("6000", "RTXPRO6000", True),
        ("6000", "A6000", True),
        ("100", "H100", True),
        ("100", "A100", True),
        ("90", "RTX4090", False),      # the whole number, not a suffix
        ("409", "RTX4090", False),
        ("h100", "H100", True),
        ("H100", "H200", False),
        ("40S", "L40S", False),        # "40S" is not a bare number; only exact names match
        ("40", "L40S", True),          # ...but the number 40 names the L40S
        ("", "H100", False),
        ("4090", "", False),
    ],
)
def test_gpu_short_matches(typed, gpu_type, expected):
    assert gpu_short_matches(typed, gpu_type) is expected


def test_gpu_short_matches_is_linear_on_a_hostile_type_name():
    """`gpu_type` falls through from an API-supplied machine name; a 40 KB one must not stall the match."""
    hostile = "9" * 40_000 + "."
    started = time.perf_counter()
    assert gpu_short_matches("4090", hostile) is False
    assert gpu_short_matches("9" * 40_000, hostile) is False   # the "." ends the name; no digit run trails it
    assert gpu_short_matches("9" * 40_000, "9" * 40_000 + "X") is True
    assert time.perf_counter() - started < 0.5


class _Client(Lium):
    def __init__(self, machines=MACHINES, executors=None):
        super().__init__(Config(api_key="test"))
        self._machines = machines
        self._executors = executors or []
        self.params: list = []

    def _request(self, method, endpoint, **kwargs):
        if endpoint == "/machines":
            return SimpleNamespace(json=lambda: [{"name": n} for n in self._machines])
        if endpoint == "/executors":
            self.params.append(kwargs.get("params"))
            return SimpleNamespace(json=lambda: self._executors)
        raise AssertionError(endpoint)


def test_bare_number_resolves_to_every_machine_with_that_number():
    client = _Client()

    assert client._resolve_machine_name("4090") == "NVIDIA GeForce RTX 4090"
    assert client._resolve_machine_name("6000") == (
        "NVIDIA RTX 6000 Ada Generation,NVIDIA RTX PRO 6000 Blackwell Server Edition,NVIDIA RTX A6000"
    )
    assert client._resolve_machine_name("100") == "NVIDIA H100 80GB HBM3,NVIDIA H100 PCIe,NVIDIA A100-SXM4-80GB"
    assert client._resolve_machine_name("3090") is None


def test_ls_sends_the_resolved_machine_names_for_a_bare_number():
    client = _Client()

    client.ls(gpu_type="4090", gpu_count=None)

    assert client.params[0]["machine_names"] == "NVIDIA GeForce RTX 4090"


def test_gpu_short_types_and_unknown_gpu_type():
    client = _Client()

    types = client.gpu_short_types()

    # "V" and "SUPER" (extractor fallbacks) are not offered as types
    assert types == ["A100", "A6000", "B200", "H100", "L40S", "RTX4090", "RTX5090", "RTX6000", "RTXPRO6000"]
    assert client.unknown_gpu_type("4090") is None
    assert client.unknown_gpu_type("rtx pro 6000") is None
    assert client.unknown_gpu_type("3090") == types
    assert client.unknown_gpu_type("H1000") == types
    # a fall-through spelling the listing resolves (`_resolve_machine_name` matches it) is known, even
    # though gpu_short_types does not offer it — a typed value that rents must never be called a typo
    assert client.unknown_gpu_type("V") is None
    assert client.unknown_gpu_type("super") is None
    # a full catalog name (what tab completion offers and `ls()` passes through) is known as itself,
    # so a sold-out `NVIDIA H100 80GB HBM3` gets the rented-out message, not "No GPU type matches"
    assert client.unknown_gpu_type("NVIDIA H100 80GB HBM3") is None
    assert client.unknown_gpu_type("NVIDIA H100 80GB HBM4") == types


def test_unknown_gpu_type_assumes_known_when_the_listing_fails(monkeypatch):
    client = _Client()
    monkeypatch.setattr(client, "gpu_types", lambda: (_ for _ in ()).throw(LiumServerError("api down")))

    assert client.unknown_gpu_type("3090") is None


def test_unknown_gpu_type_assumes_known_when_the_marketplace_is_empty():
    # an empty /machines result has no types to list — never "Types on the marketplace:" with nothing after it
    client = _Client(machines=[])

    assert client.unknown_gpu_type("3090") is None


# --- CLI messages ---------------------------------------------------------------------------------

class _FakeLium:
    def __init__(self, *a, **k):
        pass

    def ls(self, **kwargs):
        return []

    def supports(self, feature):
        # #209: ResolveExecutorAction asks for RENT_BY_SPEC before ls(); this fake is the client-side path
        return False

    def unknown_gpu_type(self, gpu_short):
        return None if gpu_short == "H100" else ["A100", "H100", "RTX4090"]


def test_ls_says_no_type_matches_instead_of_all_rented(monkeypatch):
    monkeypatch.setattr(ls_command, "Lium", _FakeLium)

    result = CliRunner().invoke(cli, ["ls", "--gpu", "3090"], catch_exceptions=False)

    out = " ".join(result.output.split())
    assert "No GPU type matches '3090'" in out and "A100, H100, RTX4090" in out
    assert "rented out" not in out


def test_ls_still_says_rented_out_for_a_known_type(monkeypatch):
    monkeypatch.setattr(ls_command, "Lium", _FakeLium)

    result = CliRunner().invoke(cli, ["ls", "--gpu", "H100"], catch_exceptions=False)

    assert "All H100 GPUs are currently rented out" in result.output


def test_up_action_names_the_known_types_for_an_unknown_gpu():
    action = up_actions.ResolveExecutorAction()

    result = action.execute({"lium": _FakeLium(), "executor_id": None, "gpu": "3090", "count": None, "country": None, "ports": None})

    assert not result.ok and "No GPU type matches '3090'" in result.error and "RTX4090" in result.error
