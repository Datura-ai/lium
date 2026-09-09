"""DAH-3298: a node index is only trusted while it still means what `lium ls` showed.

`lium up 3` translates the row number through the listing the last `lium ls`
recorded in `~/.lium/last_selection.json`. Every `ls` in the home directory —
another agent's included — rewrites that one file, so a number taken at face
value can rent a node the caller never looked at. The listing now carries the
shell that wrote it and a UTC time; a number from another shell, from a listing
older than 10 minutes, or with no listing at all is refused with a hint
(the rule lium#138 / DAH-2559 set for pod indexes).
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from lium.sdk import ExecutorInfo
from lium.cli import utils
from lium.cli.cli import cli
from lium.cli.up import command as up_module
from lium.cli.utils import (
    POD_INDEX_TTL_SECONDS,
    resolve_executor_indices,
    store_executor_selection,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _node(huid: str, price: float = 1.0) -> ExecutorInfo:
    return ExecutorInfo(
        id=f"id-{huid}",
        huid=huid,
        machine_name="NVIDIA H100 80GB HBM3",
        gpu_type="H100",
        gpu_count=1,
        price_per_hour=price,
        price_per_gpu=price,
        location={"country": "US", "country_code": "US"},
        specs={},
        status="available",
        docker_in_docker=False,
        ip="203.0.113.10",
        available_port_count=5,
    )


CHEAP = _node("brave-wolf-26", 0.30)
DEAR = _node("noble-eagle-be", 9.84)


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Point the listing file at a scratch directory, never at the real ~/.lium."""
    monkeypatch.setattr(utils.config, "config_dir", tmp_path)
    return tmp_path


def _listing_file(config_dir):
    return config_dir / "last_selection.json"


# --- resolution ----------------------------------------------------------------------------


def test_index_resolves_against_this_shells_fresh_listing(config_dir):
    store_executor_selection([CHEAP, DEAR], now=NOW)

    ids, error = resolve_executor_indices(["2"], now=NOW + timedelta(minutes=1))

    assert error is None
    assert ids == ["id-noble-eagle-be"]
    written = json.loads(_listing_file(config_dir).read_text())
    assert written["session"] == utils.pod_index_session()
    assert written["timestamp"] == NOW.isoformat()


def test_index_is_refused_without_a_prior_ls(config_dir):
    ids, error = resolve_executor_indices(["1"], now=NOW)

    assert ids == []
    assert "before 'lium ls'" in error and "in this shell" in error
    assert "Run 'lium ls'" in error and "huid" in error


def test_index_is_refused_when_the_listing_is_older_than_the_ttl(config_dir):
    store_executor_selection([CHEAP, DEAR], now=NOW)

    ids, error = resolve_executor_indices(
        ["1"], now=NOW + timedelta(seconds=POD_INDEX_TTL_SECONDS + 1)
    )

    assert ids == []
    assert "older than 10 minutes" in error and "Run 'lium ls'" in error


def test_index_is_refused_when_another_shell_wrote_the_listing(config_dir, monkeypatch):
    """The hazard: another agent's `lium ls` in the same home directory renumbers the rows."""
    monkeypatch.setattr(utils, "pod_index_session", lambda: "other-agent")
    store_executor_selection([DEAR, CHEAP], now=NOW)
    monkeypatch.setattr(utils, "pod_index_session", lambda: "this-shell")

    ids, error = resolve_executor_indices(["1"], now=NOW + timedelta(seconds=5))

    assert ids == []
    assert "another shell" in error and "Run 'lium ls'" in error


def test_listing_written_before_this_change_is_refused(config_dir):
    """Negative control: the old file shape (naive local time, no session) no longer resolves."""
    _listing_file(config_dir).write_text(
        json.dumps({"timestamp": "2026-09-09T12:00:00", "executors": [{"id": "id-old", "huid": "old"}]})
    )

    ids, error = resolve_executor_indices(["1"], now=NOW + timedelta(seconds=5))

    assert ids == []
    assert error is not None and "Run 'lium ls'" in error


def test_indexes_can_be_switched_off(config_dir, monkeypatch):
    store_executor_selection([CHEAP], now=NOW)
    monkeypatch.setenv(utils.POD_INDEX_ENV, "1")

    ids, error = resolve_executor_indices(["1"], now=NOW)

    assert ids == []
    assert utils.POD_INDEX_ENV in error


def test_out_of_range_and_non_numeric_still_reported(config_dir):
    store_executor_selection([CHEAP], now=NOW)

    ids, error = resolve_executor_indices(["3", "x"], now=NOW)

    assert ids == []
    assert "out of range (1..1)" in error and "not a valid index" in error


# --- through the CLI -----------------------------------------------------------------------


class _FakeLium:
    """Records every API call `up` makes; a refused index must make none."""

    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        def record(*args, **kwargs):
            _FakeLium.calls.append(name)
            return None

        return record


def test_up_refuses_a_stale_index_before_touching_the_api(config_dir, monkeypatch):
    """`lium up 1` after a 10-minute-old `ls`: nothing is looked up or rented, exit non-zero."""
    store_executor_selection([CHEAP, DEAR], now=NOW - timedelta(seconds=POD_INDEX_TTL_SECONDS + 60))
    _FakeLium.calls = []
    monkeypatch.setattr(up_module, "Lium", _FakeLium)
    monkeypatch.setattr(up_module, "ensure_config", lambda: None)
    # the workspace line (GET /users/me, lium#183) is printed before the node is resolved; it is
    # not the lookup this test guards, so it is stubbed out rather than counted as a call
    monkeypatch.setattr(up_module, "show_workspace", lambda *args, **kwargs: None)

    result = CliRunner().invoke(cli, ["up", "1", "-y", "--no-ssh"])

    assert result.exit_code != 0
    assert "older than 10 minutes" in result.output
    assert "Run 'lium ls'" in result.output
    assert _FakeLium.calls == []
