"""Unit tests for ``Lium.ls()`` widen_for_splitting behaviour (DAH-2254)."""

from __future__ import annotations

import inspect
import json
import pathlib

from lium.sdk import Config, Lium
from lium.cli.ls.display import _cfg, compact_executor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXTURE_PATH = pathlib.Path(__file__).parent / "fixtures" / "splittable_executors.json"


def _load_fixture_rows() -> list[dict]:
    """Load the shared fixture and augment each row with the minimal API fields
    that ``_dict_to_executor_info`` needs (specs, location, executor_ip_address)."""
    rows = json.loads(_FIXTURE_PATH.read_text())
    augmented = []
    for row in rows:
        d = dict(row)
        # Add mandatory API envelope fields that the fixture omits for brevity
        d.setdefault("executor_ip_address", "1.2.3.4")
        d.setdefault("location", {"country": "US"})
        d.setdefault("specs", {
            "gpu": {
                "count": d.get("gpu_count", 1),
                "details": [{"name": d.get("gpu_type", "H200"), "capacity": 81920, "pcie_speed": 16}],
                "driver": "535.104.05",
            },
            "ram": {"total": 2097152},
            "hard_disk": {"total": 10485760},
            "sysbox_runtime": False,
        })
        d.setdefault("effective_upload_speed_mbps", 500.0)
        d.setdefault("effective_download_speed_mbps", 1000.0)
        augmented.append(d)
    return augmented


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _client_with_fixture(monkeypatch) -> tuple[Lium, list]:
    """Return a Lium client whose _request always returns the augmented fixture rows."""
    client = Lium(Config(api_key="test-key"))
    rows = _load_fixture_rows()

    def fake_request(method, endpoint, **kwargs):
        return _Response(rows)

    monkeypatch.setattr(client, "_request", fake_request)
    return client, rows


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_ls_widen_true_includes_splittable(monkeypatch):
    """widen_for_splitting=True, gpu_count=2: exec-split-min1 included; others excluded."""
    client, _ = _client_with_fixture(monkeypatch)
    result = client.ls(gpu_count=2, widen_for_splitting=True)
    ids = [e.id for e in result]

    assert "exec-split-min1" in ids, f"exec-split-min1 missing from {ids}"
    assert "exec-split-min4" not in ids, "exec-split-min4 wrongly included (min=4 > 2)"
    assert "exec-full" not in ids, "exec-full wrongly included (non-splittable, available=8 != 2)"
    assert "exec-native1" not in ids, "exec-native1 wrongly included (available=1 != 2)"


def test_ls_widen_false_excludes_splittable(monkeypatch):
    """widen_for_splitting=False (default), gpu_count=2: no rows match available_gpu_count==2."""
    client, _ = _client_with_fixture(monkeypatch)
    result = client.ls(gpu_count=2, widen_for_splitting=False)
    ids = [e.id for e in result]

    # None of the fixture rows have available_gpu_count == 2
    assert ids == [], f"Expected empty list with strict filter, got {ids}"


def test_ls_widen_default_is_false():
    """The default value of widen_for_splitting must be False (Gate D)."""
    param = inspect.signature(Lium.ls).parameters["widen_for_splitting"]
    assert param.default is False, (
        f"widen_for_splitting default must be False, got {param.default!r}"
    )


def test_compact_executor_includes_min_gpu_count_for_rental(monkeypatch):
    """compact_executor dict must include min_gpu_count_for_rental (CLI-AC #3)."""
    client, _ = _client_with_fixture(monkeypatch)
    result = client.ls(widen_for_splitting=False)
    # Find exec-split-min1 and exec-full
    by_id = {e.id: e for e in result}

    # exec-split-min1 has min_gpu_count_for_rental=1
    exe_split = by_id.get("exec-split-min1")
    assert exe_split is not None, "exec-split-min1 missing from unfiltered ls()"
    d_split = compact_executor(exe_split, is_pareto=False, index=1)
    assert "min_gpu_count_for_rental" in d_split, "key missing from compact_executor"
    assert d_split["min_gpu_count_for_rental"] == 1

    # exec-full has min_gpu_count_for_rental=None
    exe_full = by_id.get("exec-full")
    assert exe_full is not None, "exec-full missing from unfiltered ls()"
    d_full = compact_executor(exe_full, is_pareto=False, index=2)
    assert "min_gpu_count_for_rental" in d_full
    assert d_full["min_gpu_count_for_rental"] is None


def test_cfg_marker_for_splittable(monkeypatch):
    """_cfg() appends '↯ from N' for splittable rows; non-splittable rows have no '↯'."""
    client, _ = _client_with_fixture(monkeypatch)
    result = client.ls(widen_for_splitting=False)
    by_id = {e.id: e for e in result}

    exe_split = by_id["exec-split-min1"]
    assert "↯ from 1" in _cfg(exe_split), f"_cfg output: {_cfg(exe_split)!r}"

    exe_full = by_id["exec-full"]
    assert "↯" not in _cfg(exe_full), f"_cfg output for non-splittable: {_cfg(exe_full)!r}"
