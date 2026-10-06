"""DAH-2559: a pod index is only trusted while it still means what `lium ps` showed.

The pod list is account-wide and `GET /pods` has no fixed order. Between a
`lium ps` and a `lium rm 1` another caller's pod can move into row 1; acting on
the number would then destroy work the caller never looked at. A row number is
therefore translated to the pod id the last `lium ps` *in this shell* showed on
that row, looked up by id in the live list, and refused when that pod is gone,
the listing is stale, or another shell produced it.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.sdk import PodInfo
from lium.cli.cli import cli
from lium.cli import utils
from lium.cli.ps import command as ps_module
from lium.cli.ps import display as ps_display
from lium.cli.rm import command as rm_module
from lium.cli.utils import (
    EXIT_CONFIGURATION_ERROR,
    EXIT_POD_NOT_FOUND,
    CliFailure,
    parse_targets,
    pod_snapshot_path,
    resolve_targets,
    store_pod_selection,
)

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def _pod(huid: str, name: str | None = None) -> PodInfo:
    return PodInfo(
        id=f"id-{huid}",
        name=name or huid,
        status="RUNNING",
        huid=huid,
        ssh_cmd="ssh user@pod.example -p 20000",
        ports={},
        created_at="2026-09-05T10:00:00Z",
        updated_at="2026-09-05T10:00:00Z",
        executor=None,
        template={},
        removal_scheduled_at=None,
        jupyter_installation_status=None,
        jupyter_url=None,
    )


MINE = _pod("eager-wolf-aa", "train")
THEIRS = _pod("brave-otter-11", "colleague-run")


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Point the snapshot file at a scratch directory, never at the real ~/.lium."""
    monkeypatch.setattr(utils.config, "config_dir", tmp_path)
    return tmp_path


def _snapshot(config_dir: Path, pods, when: datetime = NOW) -> None:
    store_pod_selection(pods, now=when)


def _read_snapshot() -> dict:
    return json.loads(pod_snapshot_path().read_text())


# --- resolution ----------------------------------------------------------------------------


def test_index_resolves_when_the_row_still_holds_the_same_pod(config_dir):
    _snapshot(config_dir, [MINE, THEIRS])

    matches = resolve_targets("1", [MINE, THEIRS], now=NOW + timedelta(minutes=1))

    assert [m.pod.huid for m in matches] == ["eager-wolf-aa"]
    assert matches[0].via_index is True
    assert matches[0].target == "1"


def test_index_is_refused_when_the_pod_it_stood_for_is_gone(config_dir):
    """The audit's case: my pod was removed and somebody else's now sits in row 1."""
    _snapshot(config_dir, [MINE, THEIRS])

    with pytest.raises(CliFailure) as failure:
        resolve_targets("1", [THEIRS], now=NOW + timedelta(minutes=1))

    assert failure.value.code == "stale_pod_index"
    assert failure.value.exit_code == EXIT_CONFIGURATION_ERROR
    assert "was eager-wolf-aa" in str(failure.value) and "no longer listed" in str(failure.value)
    assert "lium ps" in str(failure.value)
    # The pod now in row 1 was never selected, so the message makes no claim about it
    # (the huid only appears as the hint's example of a stable name).
    assert "is now" not in str(failure.value)


def test_index_follows_the_pod_when_the_list_is_reordered(config_dir):
    """`GET /pods` has no ORDER BY: a row update can swap rows without any pod coming or going."""
    _snapshot(config_dir, [MINE, THEIRS])

    matches = resolve_targets("1", [THEIRS, MINE], now=NOW + timedelta(minutes=1))

    assert [m.pod.huid for m in matches] == ["eager-wolf-aa"]
    assert matches[0].via_index is True


def test_hint_does_not_name_a_command(config_dir):
    """The same resolver serves exec, ssh and scp; the hint must not say `lium rm`."""
    with pytest.raises(CliFailure) as failure:
        resolve_targets("1", [MINE], now=NOW)

    assert "lium rm" not in str(failure.value)
    assert "eager-wolf-aa" in str(failure.value)


def test_index_is_refused_without_a_prior_ps(config_dir):
    with pytest.raises(CliFailure) as failure:
        resolve_targets("1", [MINE], now=NOW)

    assert failure.value.code == "stale_pod_index"
    assert "before 'lium ps'" in str(failure.value)
    assert "in this shell" in str(failure.value)


def test_another_shells_ps_does_not_define_this_shells_numbers(config_dir, monkeypatch):
    """Agent B's `lium ps` between A's `ps` and A's `rm 1` must not rewrite A's snapshot."""
    monkeypatch.setattr(utils, "pod_index_session", lambda: "agent-a")
    _snapshot(config_dir, [MINE, THEIRS])
    monkeypatch.setattr(utils, "pod_index_session", lambda: "agent-b")
    _snapshot(config_dir, [THEIRS])

    assert sorted(p.name for p in config_dir.glob("last_ps.*.json")) == [
        "last_ps.agent-a.json",
        "last_ps.agent-b.json",
    ]
    monkeypatch.setattr(utils, "pod_index_session", lambda: "agent-a")
    assert [m.pod.huid for m in resolve_targets("1", [MINE, THEIRS], now=NOW)] == ["eager-wolf-aa"]
    monkeypatch.setattr(utils, "pod_index_session", lambda: "agent-b")
    assert [m.pod.huid for m in resolve_targets("1", [MINE, THEIRS], now=NOW)] == ["brave-otter-11"]
    monkeypatch.setattr(utils, "pod_index_session", lambda: "agent-c")
    with pytest.raises(CliFailure):
        resolve_targets("1", [MINE, THEIRS], now=NOW)


def test_snapshot_is_keyed_by_the_parent_process(config_dir):
    _snapshot(config_dir, [MINE])

    assert pod_snapshot_path() == config_dir / f"last_ps.{os.getppid()}.json"
    assert pod_snapshot_path().exists()


def test_other_shells_expired_snapshots_are_pruned(config_dir, monkeypatch):
    stale = config_dir / "last_ps.999.json"
    stale.write_text("{}")
    os.utime(stale, (NOW.timestamp() - utils.POD_INDEX_TTL_SECONDS - 1,) * 2)
    fresh = config_dir / "last_ps.998.json"
    fresh.write_text("{}")
    os.utime(fresh, (NOW.timestamp() - 5,) * 2)

    _snapshot(config_dir, [MINE], when=NOW)

    assert not stale.exists()
    assert fresh.exists()


def test_index_is_refused_when_the_listing_is_older_than_the_ttl(config_dir):
    _snapshot(config_dir, [MINE], when=NOW - timedelta(seconds=utils.POD_INDEX_TTL_SECONDS + 1))

    with pytest.raises(CliFailure) as failure:
        resolve_targets("1", [MINE], now=NOW)

    assert failure.value.code == "stale_pod_index"
    assert "older than" in str(failure.value)


def test_index_is_refused_when_the_row_is_gone(config_dir):
    _snapshot(config_dir, [MINE, THEIRS])

    with pytest.raises(CliFailure) as failure:
        resolve_targets("2", [MINE], now=NOW)

    assert failure.value.code == "stale_pod_index"
    assert "brave-otter-11" in str(failure.value)


def test_a_pod_literally_named_by_a_number_is_reachable_in_a_fresh_shell(config_dir):
    """No `lium ps` yet, but a pod is called "42": the name wins over the refused index."""
    forty_two = _pod("calm-lynx-42", "42")

    matches = resolve_targets("42", [MINE, forty_two], now=NOW)

    assert [m.pod.huid for m in matches] == ["calm-lynx-42"]
    assert matches[0].via_index is False


def test_a_pod_literally_named_by_a_number_is_reachable_after_the_ttl(config_dir):
    _snapshot(config_dir, [MINE, THEIRS], when=NOW - timedelta(seconds=utils.POD_INDEX_TTL_SECONDS + 1))
    one = _pod("calm-lynx-01", "1")

    matches = resolve_targets("1", [MINE, THEIRS, one], now=NOW)

    assert [m.pod.huid for m in matches] == ["calm-lynx-01"]
    assert matches[0].via_index is False


def test_a_number_the_last_ps_never_showed_falls_back_to_name_matching(config_dir):
    """Row 7 of a two-row listing is not an index; it may still be a pod literally named "7"."""
    _snapshot(config_dir, [MINE, THEIRS])
    seven = _pod("quiet-fox-07", "7")

    matches = resolve_targets("7", [MINE, THEIRS, seven], now=NOW)

    assert [m.pod.huid for m in matches] == ["quiet-fox-07"]
    assert matches[0].via_index is False


def test_a_longer_live_list_does_not_invalidate_an_unchanged_row(config_dir):
    """A pod appended after the listing does not move row 1."""
    _snapshot(config_dir, [MINE])

    matches = resolve_targets("1", [MINE, THEIRS], now=NOW)

    assert [m.pod.huid for m in matches] == ["eager-wolf-aa"]


def test_names_and_huids_never_consult_the_snapshot(config_dir):
    assert [p.huid for p in parse_targets("train,brave-otter-11", [MINE, THEIRS])] == [
        "eager-wolf-aa",
        "brave-otter-11",
    ]


def test_name_only_mode_treats_numbers_as_names(config_dir):
    _snapshot(config_dir, [MINE])

    assert parse_targets("1", [MINE], allow_index=False) == []


def test_env_opt_out_disables_indexes_for_every_command(config_dir, monkeypatch):
    _snapshot(config_dir, [MINE])
    monkeypatch.setenv(utils.POD_INDEX_ENV, "1")

    assert parse_targets("1", [MINE]) == []


def test_unreadable_snapshot_counts_as_no_snapshot(config_dir):
    pod_snapshot_path().write_text("{not json")

    with pytest.raises(CliFailure) as failure:
        resolve_targets("1", [MINE], now=NOW)

    assert failure.value.code == "stale_pod_index"


# --- ps -------------------------------------------------------------------------------------


class _FakeLium:
    pods: list = []
    # a server without workspaces: `rm` reads it for its workspace line
    workspaces = SimpleNamespace(current=lambda: None)

    def __init__(self, *args, **kwargs):
        pass

    def ps(self):
        return list(self.pods)

    def pod(self, pod_id):
        # DAH-2932: `ps <pod>` asks GET /pods/{id} for the last lifecycle event; nothing to add here
        return {}


def _run(monkeypatch, module, pods, args, fake=None, **kwargs):
    fake = fake or _FakeLium
    fake.pods = pods
    monkeypatch.setattr(module, "Lium", fake)
    if hasattr(module, "ensure_config"):
        monkeypatch.setattr(module, "ensure_config", lambda: None)
    return CliRunner().invoke(cli, args, **kwargs)


def test_ps_json_numbers_the_rows_and_records_the_listing(config_dir, monkeypatch):
    result = _run(monkeypatch, ps_module, [MINE, THEIRS], ["ps", "--format", "json"])

    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert [(r["index"], r["huid"]) for r in rows] == [(1, "eager-wolf-aa"), (2, "brave-otter-11")]

    snapshot = _read_snapshot()
    assert [p["id"] for p in snapshot["pods"]] == ["id-eager-wolf-aa", "id-brave-otter-11"]


def test_ps_table_leads_with_the_row_number():
    table, _ = ps_display.build_pods_table([MINE, THEIRS])

    assert table.columns[0].header == "#"
    assert [str(cell) for cell in table.columns[0]._cells] == [
        ps_display.console.get_styled("1", "dim"),
        ps_display.console.get_styled("2", "dim"),
    ]
    assert table.columns[1].header == "Pod"


def test_ps_table_for_a_single_pod_lookup_has_no_row_number():
    table, _ = ps_display.build_pods_table([MINE], show_index=False)

    assert table.columns[0].header == "Pod"


def test_ps_for_one_pod_has_no_index_and_leaves_the_snapshot_alone(config_dir, monkeypatch):
    _snapshot(config_dir, [MINE, THEIRS])

    result = _run(monkeypatch, ps_module, [THEIRS, MINE], ["ps", "eager-wolf-aa", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["index"] is None
    snapshot = _read_snapshot()
    assert [p["huid"] for p in snapshot["pods"]] == ["eager-wolf-aa", "brave-otter-11"]


def test_compact_pod_default_index_is_none():
    assert ps_display.compact_pod(MINE)["index"] is None


# --- rm -------------------------------------------------------------------------------------


class _RecordingLium(_FakeLium):
    removed: list = []

    def rm(self, pod):
        self.removed.append(pod.huid)
        return {}


def _run_rm(monkeypatch, pods, args, **kwargs):
    _RecordingLium.removed = []
    result = _run(monkeypatch, rm_module, pods, ["rm", *args], fake=_RecordingLium, **kwargs)
    return result, _RecordingLium.removed


def test_rm_by_index_refuses_when_the_pod_is_gone(config_dir, monkeypatch):
    """Somebody else's pod now sits in row 1: nothing is removed, the pod I saw is named."""
    _snapshot(config_dir, [MINE, THEIRS], when=datetime.now(timezone.utc))

    result, removed = _run_rm(monkeypatch, [THEIRS], ["1", "-y"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR, result.output
    assert removed == []
    assert "eager-wolf-aa" in result.output


def test_rm_by_index_removes_the_pod_seen_even_when_rows_swapped(config_dir, monkeypatch):
    _snapshot(config_dir, [MINE, THEIRS], when=datetime.now(timezone.utc))

    result, removed = _run_rm(monkeypatch, [THEIRS, MINE], ["1", "-y"])

    assert result.exit_code == 0, result.output
    assert removed == ["eager-wolf-aa"]


def test_rm_by_index_names_the_pod_it_removes(config_dir, monkeypatch):
    _snapshot(config_dir, [MINE, THEIRS], when=datetime.now(timezone.utc))

    result, removed = _run_rm(monkeypatch, [MINE, THEIRS], ["1", "-y"])

    assert result.exit_code == 0, result.output
    assert removed == ["eager-wolf-aa"]
    assert "1 → eager-wolf-aa (name: train)" in result.output


def test_rm_by_index_asks_a_human_before_removing(config_dir, monkeypatch):
    _snapshot(config_dir, [MINE, THEIRS], when=datetime.now(timezone.utc))
    # CliRunner swaps sys.stdin for its own pipe; tell the module a human can answer (DAH-2883's is_interactive).
    monkeypatch.setattr(rm_module.ui, "is_interactive", lambda: True)  # rm asks through ui.confirm
    monkeypatch.setattr(rm_module.ui, "confirm", lambda *a, **k: False)

    result, removed = _run_rm(monkeypatch, [MINE, THEIRS], ["1"])

    assert result.exit_code == 0, result.output
    assert removed == []
    assert "eager-wolf-aa" in result.output


def test_rm_name_only_does_not_read_numbers_as_indexes(config_dir, monkeypatch):
    _snapshot(config_dir, [MINE], when=datetime.now(timezone.utc))

    result, removed = _run_rm(monkeypatch, [MINE], ["1", "--name-only", "-y"])

    assert result.exit_code == EXIT_POD_NOT_FOUND, result.output
    assert removed == []


def test_rm_by_huid_is_unaffected(config_dir, monkeypatch):
    result, removed = _run_rm(monkeypatch, [MINE, THEIRS], ["eager-wolf-aa", "-y"])

    assert result.exit_code == 0, result.output
    assert removed == ["eager-wolf-aa"]
