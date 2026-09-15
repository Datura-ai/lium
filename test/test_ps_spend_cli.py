"""`lium ps` that fits the terminal and answers questions; `lium spend` for the money.

Ten pods of mixed status and price and the questions are always the same:
which are running, which is the expensive one, how fast is the balance going
and for how long. Sorting, filtering and watching belong on `ps`; the money
view is its own command because the API only gives price and creation time,
so the totals are estimates worth labelling as such.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from rich.console import ConsoleDimensions

from lium.cli import utils
from lium.cli.cli import cli
from lium.cli.ps import command as ps_module
from lium.cli.ps import display
from lium.cli.ps import selection
from lium.cli.spend import command as spend_module
from lium.cli.spend import report
from lium.cli.utils import EXIT_CONFIGURATION_ERROR, EXIT_POD_NOT_FOUND, pod_snapshot_path
from lium.sdk import ExecutorInfo, PodInfo

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _executor(gpu="H100", count=1, price=2.0):
    return ExecutorInfo(
        id=f"ex-{gpu}", huid=f"node-{gpu}", machine_name="m", gpu_type=gpu, gpu_count=count,
        price_per_hour=price, price_per_gpu=price / count, location={}, specs={}, status="active",
        docker_in_docker=False, ip="1.2.3.4",
    )


def _pod(huid, name, status="RUNNING", hours_ago=1.0, executor=None, template=None):
    created = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return PodInfo(
        id=f"id-{huid}", name=name, huid=huid, status=status, ssh_cmd="ssh root@1.2.3.4 -p 22", ports={},
        created_at=created, updated_at=created, executor=executor or _executor(), template=template or {"name": "PyTorch"},
        removal_scheduled_at=None, jupyter_installation_status=None, jupyter_url=None,
    )


TRAIN = _pod("swift-fox-c8", "train", hours_ago=3.0, executor=_executor("H100", 8, 17.6))
EVAL = _pod("brave-lion-11", "eval", hours_ago=0.5, executor=_executor("A100", 1, 1.2))
PENDING = _pod("calm-owl-42", "new", status="PENDING", hours_ago=0.1, executor=_executor("B200", 8, 30.0))
PODS = [EVAL, TRAIN, PENDING]


@pytest.fixture
def fake_lium(monkeypatch, tmp_path):
    # `ps` records the rows it showed (DAH-2559); keep that file out of the real ~/.lium.
    monkeypatch.setattr(utils.config, "config_dir", tmp_path)

    class _Lium:
        pods = list(PODS)
        balance_value = 100.0
        # a server without workspaces: `ps` reads it for its workspace line (lium#183)
        workspaces = SimpleNamespace(current=lambda: None)

        def __init__(self, *a, **k):
            pass

        def ps(self):
            return list(self.pods)

        def balance(self):
            return self.balance_value

    monkeypatch.setattr(ps_module, "Lium", _Lium)
    monkeypatch.setattr(ps_module, "ensure_config", lambda: None)
    monkeypatch.setattr(spend_module, "Lium", _Lium)
    return _Lium


def _huids(output: str):
    return [p["huid"] for p in json.loads(output)]


# --- ps: sort and filter -------------------------------------------------------------------

def test_ps_filter_by_status_prefix_is_case_insensitive(fake_lium):
    result = CliRunner().invoke(cli, ["ps", "--filter", "status=run", "--json"])

    assert result.exit_code == 0, result.output
    assert _huids(result.output) == ["brave-lion-11", "swift-fox-c8"]


def test_ps_filters_combine_and_gpu_is_an_alias_for_gpu_type(fake_lium):
    result = CliRunner().invoke(cli, ["ps", "--filter", "gpu=H1", "--filter", "name=tr", "--json"])

    assert _huids(result.output) == ["swift-fox-c8"]


def test_ps_bad_filter_exits_configuration_error(fake_lium):
    result = CliRunner().invoke(cli, ["ps", "--filter", "colour=blue"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert "KEY=VALUE" in result.output and "status" in result.output


@pytest.mark.parametrize("sort_key, expected", [
    ("spent", ["swift-fox-c8", "calm-owl-42", "brave-lion-11"]),   # $52.80, $3.00, $0.60
    ("price", ["calm-owl-42", "swift-fox-c8", "brave-lion-11"]),   # highest $/h first
    ("name", ["brave-lion-11", "calm-owl-42", "swift-fox-c8"]),    # eval, new, train
    ("status", ["calm-owl-42", "brave-lion-11", "swift-fox-c8"]),  # PENDING before RUNNING
    ("created", ["calm-owl-42", "brave-lion-11", "swift-fox-c8"]), # newest first
    ("uptime", ["swift-fox-c8", "brave-lion-11", "calm-owl-42"]),  # longest running first
])
def test_ps_sort_orders(fake_lium, sort_key, expected):
    result = CliRunner().invoke(cli, ["ps", "--sort", sort_key, "--json"])

    assert _huids(result.output) == expected, result.output


def test_ps_reverse_flips_the_order(fake_lium):
    result = CliRunner().invoke(cli, ["ps", "--sort", "price", "--reverse", "--json"])

    assert _huids(result.output) == ["brave-lion-11", "swift-fox-c8", "calm-owl-42"]


def test_ps_without_sort_keeps_api_order(fake_lium):
    result = CliRunner().invoke(cli, ["ps", "--json"])

    assert _huids(result.output) == ["brave-lion-11", "swift-fox-c8", "calm-owl-42"]


def test_ps_sorted_listing_numbers_the_rows_shown_and_records_them(fake_lium):
    """`lium rm 1` after `lium ps --sort price` is the pod on row 1 of that sorted listing (DAH-2559: the
    index is whatever the last `ps` showed, in the order shown)."""
    result = CliRunner().invoke(cli, ["ps", "--sort", "price", "--json"])

    rows = json.loads(result.output)
    assert [(r["index"], r["huid"]) for r in rows] == [(1, "calm-owl-42"), (2, "swift-fox-c8"), (3, "brave-lion-11")]
    snapshot = json.loads(pod_snapshot_path().read_text())
    assert [p["huid"] for p in snapshot["pods"]] == ["calm-owl-42", "swift-fox-c8", "brave-lion-11"]


def test_ps_filtered_listing_numbers_only_the_rows_shown_and_records_them(fake_lium):
    """A filtered listing shows rows 1..k and `rm 2` means its row 2, not the second pod of the full list.
    (`ps <pod>` writing nothing is main's test_ps_for_one_pod_has_no_index_and_leaves_the_snapshot_alone.)"""
    result = CliRunner().invoke(cli, ["ps", "--filter", "status=RUNNING", "--json"])

    rows = json.loads(result.output)
    assert [(r["index"], r["huid"]) for r in rows] == [(1, "brave-lion-11"), (2, "swift-fox-c8")]
    snapshot = json.loads(pod_snapshot_path().read_text())
    assert [p["huid"] for p in snapshot["pods"]] == ["brave-lion-11", "swift-fox-c8"]


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def test_ps_json_flag_is_an_alias(fake_lium, monkeypatch):
    # spent_usd is price × wall time: the two invocations must read the same clock or a cent can tick between them
    monkeypatch.setattr(display, "datetime", _FrozenDatetime)

    assert CliRunner().invoke(cli, ["ps", "--json"]).output == CliRunner().invoke(cli, ["ps", "--format", "json"]).output


def test_ps_one_pod_still_honours_filters(fake_lium):
    result = CliRunner().invoke(cli, ["ps", "train", "--filter", "status=PENDING", "--json"])

    assert result.exit_code == 0 and json.loads(result.output) == []


def test_ps_unknown_pod_is_still_a_miss(fake_lium):
    assert CliRunner().invoke(cli, ["ps", "nope", "--json"]).exit_code == EXIT_POD_NOT_FOUND


def test_ps_filter_matching_nothing_says_so_in_table_mode(fake_lium):
    result = CliRunner().invoke(cli, ["ps", "--filter", "status=STOPPED"])

    assert result.exit_code == 0
    assert "No pods match the filters" in result.output


def test_parse_filters_rejects_missing_equals():
    with pytest.raises(ValueError):
        selection.parse_filters(["status"])


# --- ps: width and watch -------------------------------------------------------------------

def test_ps_hides_ports_on_a_narrow_terminal(fake_lium, monkeypatch):
    monkeypatch.setattr(ps_module, "_terminal_width", lambda: 90)

    result = CliRunner().invoke(cli, ["ps"])

    assert result.exit_code == 0, result.output
    assert "Ports hidden" in result.output
    assert result.output.count("Ports") == 1   # only the hint, no column header
    assert PORTS_CELL not in result.output


# `_terminal_width()` decides whether Ports is drawn; Rich then draws the table at
# its own width — 80 under CliRunner, where the # column (DAH-2559) squeezes Ports
# until its header reads "Port" and the IP folds. The wide cases draw at 160 and
# read the cell (the executor IP) as the proof the column is there.
PORTS_CELL = "1.2.3.4"


def _draw_at(monkeypatch, columns: int) -> None:
    monkeypatch.setattr(type(ps_module.console), "size", property(lambda self: ConsoleDimensions(columns, 50)))


def test_ps_wide_forces_every_column(fake_lium, monkeypatch):
    monkeypatch.setattr(ps_module, "_terminal_width", lambda: 90)
    _draw_at(monkeypatch, 160)

    result = CliRunner().invoke(cli, ["ps", "--wide"])

    assert "Ports" in result.output and PORTS_CELL in result.output
    assert "Ports hidden" not in result.output


def test_ps_keeps_every_column_when_stdout_is_not_a_terminal(fake_lium, monkeypatch):
    """Rich reports 80 columns for a pipe; cron and CI must not lose Ports (and the IP in it)."""
    monkeypatch.setattr(type(ps_module.console), "is_terminal", property(lambda self: False))
    # CliRunner's stdout is a pipe, so Rich already reports 80 columns here; the table
    # is drawn wider only so the header survives (see PORTS_CELL).
    _draw_at(monkeypatch, 160)

    output = CliRunner().invoke(cli, ["ps"]).output

    assert "Ports" in output and PORTS_CELL in output and "Ports hidden" not in output


def test_ps_json_error_envelope_with_the_json_flag(fake_lium):
    """`--json` must key the JSON error envelope, not just the happy path."""
    result = CliRunner().invoke(cli, ["ps", "nope", "--json"])

    assert result.exit_code == EXIT_POD_NOT_FOUND
    assert '"pod_not_found"' in result.output


def test_ps_shows_ports_on_a_wide_terminal(fake_lium, monkeypatch):
    monkeypatch.setattr(ps_module, "_terminal_width", lambda: 160)
    _draw_at(monkeypatch, 160)

    output = CliRunner().invoke(cli, ["ps"]).output
    assert "Ports" in output and PORTS_CELL in output and "Ports hidden" not in output


def test_ps_watch_refreshes_until_interrupted(fake_lium, monkeypatch):
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(ps_module.time, "sleep", fake_sleep)

    result = CliRunner().invoke(cli, ["ps", "--watch", "5", "--json"])

    assert result.exit_code == 0, result.output
    assert sleeps == [5.0, 5.0]
    assert result.output.count('"huid"') == 3 * 2   # two full listings before the interrupt


def test_ps_watch_interrupted_during_the_first_fetch_exits_zero(fake_lium, monkeypatch):
    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(ps_module, "_load_pods", interrupted)

    result = CliRunner().invoke(cli, ["ps", "--watch", "1"])

    assert result.exit_code == 0, result.output
    assert "Aborted" not in result.output


def test_ps_rejects_a_non_positive_watch(fake_lium):
    assert CliRunner().invoke(cli, ["ps", "--watch", "0"]).exit_code == EXIT_CONFIGURATION_ERROR


# --- spend ---------------------------------------------------------------------------------

def test_pod_spend_is_price_times_wall_time():
    row = report.pod_spend(TRAIN, now=NOW)

    assert row.config == "8×H100" and row.price_per_hour == 17.6
    assert row.uptime_hours == 3.0 and row.spent_usd == 52.8
    assert row.since == (NOW - timedelta(hours=3)).isoformat(timespec="seconds")


def test_pod_spend_labels_a_gpu_split_pod_with_its_own_count_as_ps_does():
    split = _pod("split", "split", executor=_executor("RTX3090", count=3, price=0.6))
    split.gpu_count = 1

    row = report.pod_spend(split, now=NOW)

    assert row.config == "RTX3090" and row.price_per_hour == 0.6


def test_pod_spend_without_executor_or_timestamp_is_unknown_not_zero():
    bare = PodInfo(
        id="x", name="x", huid="x", status="FAILED", ssh_cmd=None, ports={}, created_at="", updated_at="",
        executor=None, template={}, removal_scheduled_at=None, jupyter_installation_status=None, jupyter_url=None,
    )

    row = report.pod_spend(bare, now=NOW)

    assert (row.price_per_hour, row.uptime_hours, row.spent_usd, row.config) == (None, None, None, None)


def test_a_pending_pod_has_spent_nothing_and_is_not_in_the_burn():
    """The platform bills from RUNNING: a pod still PENDING is $0 spent, whatever its age, and not burning."""
    row = report.pod_spend(PENDING, now=NOW)

    assert row.billable is False
    assert row.spent_usd == 0.0
    assert row.uptime_hours == 0.1 and row.price_per_hour == 30.0


def test_build_report_totals_and_runway():
    result = report.build_report(PODS, balance=100.0, now=NOW)

    # most spent first; the PENDING pod ($0) drops to the bottom
    assert [r.huid for r in result.pods] == ["swift-fox-c8", "brave-lion-11", "calm-owl-42"]
    assert result.burn_per_hour == 18.8            # 17.6 + 1.2; the PENDING pod is not billed yet
    assert result.spent_usd == 53.4                # 52.8 + 0.6; the PENDING pod has spent $0, not 0.1 h × $30
    assert result.runway_hours == 5.3              # 100 / 18.8 -> 5.3h
    assert "estimate" in result.note or "does not report" in result.note
    assert "volume" in result.note
    assert [r.billable for r in result.pods] == [True, True, False]


def test_burn_counts_the_statuses_the_platform_bills():
    rebooting = _pod("x-1", "reboot", status="REBOOT_PENDING", executor=_executor("A100", 1, 1.2))
    failed_reboot = _pod("x-2", "reboot2", status="REBOOT_FAILED", executor=_executor("A100", 1, 1.2))
    failed = _pod("x-3", "dead", status="FAILED", executor=_executor("A100", 1, 1.2))

    result = report.build_report([rebooting, failed_reboot, failed], balance=None, now=NOW)

    assert result.burn_per_hour == 2.4


def test_build_report_runway_edge_cases():
    assert report.build_report([], balance=10.0, now=NOW).runway_hours is None
    assert report.build_report(PODS, balance=None, now=NOW).runway_hours is None
    assert report.build_report(PODS, balance=-0.5, now=NOW).runway_hours == 0.0


def test_spend_command_table_and_summary(fake_lium):
    result = CliRunner().invoke(cli, ["spend"])

    assert result.exit_code == 0, result.output
    assert "train" in result.output and "8×H100" in result.output and "$17.60" in result.output
    assert "Burn $18.80/h across 2 of 3 pods" in result.output
    assert "Balance $100.00" in result.output and "runway" in result.output


def test_spend_command_json(fake_lium, monkeypatch):
    # spent_usd is price × wall time: the two invocations must read the same clock or a cent can tick between them
    from lium.cli.spend import report as report_module

    monkeypatch.setattr(report_module, "datetime", _FrozenDatetime)
    result = CliRunner().invoke(cli, ["spend", "--format", "json"])
    assert result.output == CliRunner().invoke(cli, ["spend", "--json"]).output

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["burn_per_hour"] == 18.8 and payload["balance_usd"] == 100.0
    assert payload["pods"][0]["huid"] == "swift-fox-c8" and payload["pods"][0]["spent_usd"] >= 52.8
    assert payload["note"]


def test_spend_without_pods(fake_lium):
    fake_lium.pods = []

    result = CliRunner().invoke(cli, ["spend"])

    assert result.exit_code == 0
    assert "No active pods" in result.output and "$0.00/h" in result.output


def test_spend_survives_a_balance_failure(fake_lium):
    def boom(self):
        raise RuntimeError("pay api down")

    fake_lium.balance = boom

    result = CliRunner().invoke(cli, ["spend", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["balance_usd"] is None and payload["runway_hours"] is None
    assert payload["burn_per_hour"] == 18.8
