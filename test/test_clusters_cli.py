"""`lium clusters` — list fabrics, rent a cluster, inspect members, remove — with the SDK mocked."""

import json

import pytest
import requests
from click.testing import CliRunner

from lium.cli import interactive
from lium.cli.cli import cli
from lium.cli.clusters import command as clusters_command
from lium.sdk import Cluster, ClusterOffer, ExecutorInfo, LiumNotFoundError, LiumServerError, PodInfo

FABRIC = "hot:infiniband:0x3:0x7fff:NVIDIA H100 80GB HBM3:8"


def _node(i: int, price: float = 16.0) -> ExecutorInfo:
    return ExecutorInfo(
        id=f"exec-{i}", huid=f"node-{i}", machine_name="NVIDIA H100 80GB HBM3", gpu_type="H100", gpu_count=8,
        price_per_hour=price, price_per_gpu=price / 8, location={"country": "US"}, specs={}, status="active",
        docker_in_docker=False, ip=f"10.0.0.{i}", tier="secure",
    )


def _offer(free: int = 3) -> ClusterOffer:
    return ClusterOffer(fabric_id=FABRIC, fabric_type="infiniband", link_rate="400 Gb/sec (4X NDR)", fabric_measured=True,
                        node_count=4, nodes=[_node(i, price=16.0 + i) for i in range(free)])


def _pod(i: int, status: str = "RUNNING", cluster_id: str = "c-1", name: str = "job") -> PodInfo:
    return PodInfo(
        id=f"pod-{i}", name=name, status=status, huid=f"pod-huid-{i}",
        ssh_cmd=f"ssh root@1.2.3.{i} -p 2200{i}" if status == "RUNNING" else None, ports={"22": 22000 + i},
        created_at="", updated_at="", executor=_node(i), template={"id": "tpl-c"}, removal_scheduled_at=None,
        jupyter_installation_status=None, jupyter_url=None,
        cluster_id=cluster_id, cluster_node_index=i, cluster_overlay_ip=f"10.42.0.{i + 1}",
    )


class FakeLium:
    offers = [_offer()]
    mine = [Cluster(id="c-1", pods=[_pod(0), _pod(1)])]
    up_calls: list = []
    removed: list = []
    scheduled: list = []
    up_result = None
    wait_result = None
    wait_calls: list = []
    schedule_fail_on = None
    schedule_error: Exception = LiumServerError("Server error: 502")
    fail_one = False

    def __init__(self, *args, **kwargs):
        pass

    def clusters(self):
        return list(self.offers)

    def cluster_offer(self, fabric):
        exact = [o for o in self.offers if o.fabric_id == fabric]
        prefix = [o for o in self.offers if o.fabric_id.startswith(fabric)]
        return (exact or (prefix if len(prefix) == 1 else [None]))[0]

    def my_clusters(self):
        return list(self.mine)

    def cluster(self, cluster_id):
        for c in self.mine:
            if c.id == cluster_id or c.id.startswith(cluster_id):
                return c
        raise LiumNotFoundError(cluster_id)

    def up_cluster(self, executor_ids, **kwargs):
        FakeLium.up_calls.append((list(executor_ids), kwargs))
        if isinstance(self.up_result, Exception):
            raise self.up_result
        return self.up_result or self.mine[0]

    def schedule_termination(self, pod, *, termination_time):
        if self.schedule_fail_on == pod.id:
            raise self.schedule_error   # the SDK's messages carry no pod id: the CLI has to name the member
        FakeLium.scheduled.append((pod.id, termination_time))
        return {}

    def wait_cluster_ready(self, cluster, *, timeout):
        FakeLium.wait_calls.append((cluster.id, timeout))
        if isinstance(self.wait_result, Exception):
            raise self.wait_result
        return self.wait_result or cluster

    def rm_cluster(self, cluster):
        FakeLium.removed.append(cluster.id)
        return [{"pod": p.id, "success": p.id != "pod-1" or not getattr(self, "fail_one", False), "error": None} for p in cluster.pods]


def _patch(monkeypatch, tmp_path, **overrides):
    FakeLium.up_calls, FakeLium.removed, FakeLium.scheduled, FakeLium.up_result = [], [], [], None
    FakeLium.wait_calls, FakeLium.wait_result, FakeLium.schedule_fail_on = [], None, None
    FakeLium.schedule_error = LiumServerError("Server error: 502")
    for k, v in overrides.items():
        monkeypatch.setattr(FakeLium, k, v)
    monkeypatch.setattr(clusters_command, "Lium", FakeLium)
    monkeypatch.setattr(clusters_command, "ensure_config", lambda: None)
    monkeypatch.setattr(clusters_command, "_selection_file", lambda: tmp_path / "last_cluster_selection.json")
    # CliRunner's stdin is a pipe; the tests that answer a prompt need a terminal, or `ui.confirm` refuses to ask
    # (`confirmation_required`, lium#124) before the "n" they feed it is read.
    monkeypatch.delenv(interactive.NONINTERACTIVE_ENV, raising=False)
    monkeypatch.setattr(interactive, "stdin_is_terminal", lambda: True)
    # A wide console, so Rich does not ellipsize the table cells the assertions look for.
    from lium.cli import utils

    monkeypatch.setattr(utils.console, "_width", 250)
    monkeypatch.setattr(utils.console, "_height", 60)


def _run(*args, input=None):
    return CliRunner().invoke(cli, ["clusters", *args], input=input, catch_exceptions=False)


def _flat(text: str) -> str:
    return " ".join(text.split())


# --- list -----------------------------------------------------------------------------------------

def test_clusters_lists_fabrics_and_caches_the_selection(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)

    result = _run()

    assert result.exit_code == 0, result.output
    assert "8×H100" in result.output and "3/4" in result.output and "infiniband" in result.output
    assert json.loads((tmp_path / "last_cluster_selection.json").read_text())["fabrics"] == [FABRIC]


def test_clusters_format_json_is_machine_readable(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)

    result = _run("--format", "json")

    data = json.loads(result.output)
    assert data[0]["fabric_id"] == FABRIC and data[0]["free_count"] == 3 and data[0]["gpus_per_node"] == 8
    assert data[0]["nodes"][0] == {"id": "exec-0", "huid": "node-0", "gpu_type": "H100", "gpu_count": 8,
                                   "price_per_hour": 16.0, "country": "US", "tier": "secure"}


def test_clusters_list_with_no_offers(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, offers=[])

    result = _run("list")

    assert result.exit_code == 0 and "No fabric has free nodes" in result.output


# --- up -------------------------------------------------------------------------------------------

def test_clusters_up_by_index_rents_the_cheapest_nodes(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    _run()  # caches fabric #1

    result = _run("up", "1", "--nodes", "2", "-n", "job", "-y", "--ttl", "2h")

    assert result.exit_code == 0, result.output
    ids, kwargs = FakeLium.up_calls[0]
    assert ids == ["exec-0", "exec-1"] and kwargs["name"] == "job" and kwargs["wait"] is False
    assert FakeLium.wait_calls == [("c-1", 900)]
    assert "Cluster c-1 (2 nodes" in result.output and "MASTER_ADDR=10.42.0.1" in result.output
    assert [p for p, _ in FakeLium.scheduled] == ["pod-0", "pod-1"]


def test_clusters_up_no_wait_does_not_wait(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)

    result = _run("up", FABRIC, "--nodes", "2", "-n", "job", "-y", "--no-wait")

    assert result.exit_code == 0, result.output
    assert FakeLium.wait_calls == []


def test_clusters_up_and_rm_ask_first_in_json_mode_too(monkeypatch, tmp_path):
    """Money is spent only after -y or an answered prompt, whatever the output format (as `lium up` does)."""
    _patch(monkeypatch, tmp_path)

    result = _run("up", FABRIC, "--nodes", "2", "-n", "job", "--format", "json", "--no-wait", input="n\n")
    assert result.exit_code == 0 and FakeLium.up_calls == [], result.output
    assert "[y/n]" not in result.stdout, "the prompt goes to stderr: stdout is the JSON document"

    result = _run("rm", "c-1", "--format", "json", input="n\n")
    assert result.exit_code == 0 and FakeLium.removed == [], result.output
    assert "[y/n]" not in result.stdout


def test_clusters_up_json_prints_the_cluster_record(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)

    result = _run("up", FABRIC, "--nodes", "2", "-n", "job", "--format", "json", "--no-wait", "-y")

    data = json.loads(result.output)
    assert data["id"] == "c-1" and data["master_addr"] == "10.42.0.1" and data["size"] == 2
    assert data["hostfile"] == "10.42.0.1 slots=8\n10.42.0.2 slots=8\n"
    assert data["pods"][1]["node_rank"] == 1 and data["pods"][1]["overlay_ip"] == "10.42.0.2"
    assert FakeLium.up_calls[0][1]["wait"] is False


def test_clusters_up_asks_before_spending_and_names_the_price(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)

    result = _run("up", FABRIC, "--nodes", "2", "-n", "job", input="n\n")

    assert result.exit_code == 0 and "$33.00/h" in result.output and FakeLium.up_calls == []


def test_clusters_up_refuses_more_nodes_than_are_free(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)

    result = _run("up", FABRIC, "--nodes", "4", "-n", "job", "-y")

    assert result.exit_code != 0 and "Only 3 of 4 nodes" in result.output and FakeLium.up_calls == []


def test_clusters_up_rejects_a_single_node_and_a_bad_ttl(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)

    assert "--nodes must be at least 2" in _run("up", FABRIC, "--nodes", "1", "-n", "job", "-y").output
    assert "Invalid TTL" in _run("up", FABRIC, "--nodes", "2", "-n", "job", "-y", "--ttl", "soon").output
    assert FakeLium.up_calls == []


def test_clusters_up_without_a_cached_listing_says_so(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)

    result = _run("up", "1", "--nodes", "2", "-n", "job", "-y")

    assert result.exit_code != 0 and "Run 'lium clusters' first" in result.output


def test_clusters_up_timeout_says_the_members_are_billing(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, wait_result=TimeoutError("Cluster c-1 not ready after 900s: job=PENDING"))

    result = _run("up", FABRIC, "--nodes", "2", "-n", "job", "-y")

    assert result.exit_code != 0 and "rented and billing" in _flat(result.output)


def test_clusters_up_wait_failure_does_not_say_retry(monkeypatch, tmp_path):
    """The members are rented when --wait gives up; the hint says what to do with them, never 'Retry'."""
    _patch(monkeypatch, tmp_path, wait_result=TimeoutError("Cluster c-1 not ready after 900s: job=PENDING"))

    result = _run("up", FABRIC, "--nodes", "2", "-n", "job", "-y")

    flat = _flat(result.output)
    assert result.exit_code == 3 and "rented and billing" in flat
    assert "Do not re-run 'lium clusters up'" in flat and "lium clusters rm c-1" in flat and "Retry" not in flat


def test_clusters_up_schedules_the_ttl_before_waiting(monkeypatch, tmp_path):
    """A --wait timeout must not leave N nodes billing with nothing scheduled."""
    _patch(monkeypatch, tmp_path, wait_result=TimeoutError("Cluster c-1 not ready after 900s: job=PENDING"))

    result = _run("up", FABRIC, "--nodes", "2", "-n", "job", "-y", "--ttl", "2h")

    assert result.exit_code != 0
    assert [p for p, _ in FakeLium.scheduled] == ["pod-0", "pod-1"]
    assert "terminates at" in _flat(result.output)


@pytest.mark.parametrize("error", [LiumServerError("Server error: 502"), requests.ConnectionError("connection reset")])
def test_clusters_up_ttl_failure_names_the_cluster_and_the_unscheduled_member(monkeypatch, tmp_path, error):
    """Every member is tried, whatever the failure; the error names the billing cluster and the member (rank + huid —
    every member shares the pod name) that has no TTL."""
    _patch(monkeypatch, tmp_path, schedule_fail_on="pod-0", schedule_error=error)

    result = _run("up", FABRIC, "--nodes", "2", "-n", "job", "-y", "--ttl", "2h", "--no-wait")

    assert result.exit_code == 3
    assert [p for p, _ in FakeLium.scheduled] == ["pod-1"]   # pod-1 was still tried after pod-0 failed
    flat = _flat(result.output)
    assert "c-1" in flat and "NOT scheduled" in flat and "rank 0 (pod-huid-0)" in flat and "lium clusters rm" in flat
    assert "pod-huid-1" not in flat
    # the nodes are rented: the hint under the error must not be the generic "Retry" (a retry rents a second cluster)
    assert "Do not re-run 'lium clusters up'" in flat and "Retry" not in flat

    FakeLium.scheduled = []
    result = _run("up", FABRIC, "--nodes", "2", "-n", "job", "-y", "--ttl", "2h", "--no-wait", "--format", "json")

    assert result.exit_code == 3
    payload = json.loads(result.output)
    assert payload["ok"] is False and payload["id"] == "c-1"
    assert payload["unscheduled"] == [{"node_rank": 0, "huid": "pod-huid-0", "id": "pod-0"}]
    assert "NOT scheduled" in payload["error"] and "pod-huid-0" in payload["error"]


# --- ps / show ------------------------------------------------------------------------------------

def test_clusters_ps_lists_my_clusters(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)

    result = _run("ps")

    assert result.exit_code == 0 and "c-1" in result.output and "RUNNING" in result.output and "10.42.0.1" in result.output


def test_clusters_ps_json(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)

    data = json.loads(_run("ps", "--format", "json").output)

    assert data[0]["id"] == "c-1" and data[0]["status"] == "RUNNING" and len(data[0]["pods"]) == 2


@pytest.mark.parametrize("args", [(), ("list",), ("up", FABRIC, "--nodes", "2", "-n", "job", "-y", "--no-wait"),
                                  ("ps",), ("show", "c-1"), ("rm", "c-1", "-y")])
def test_clusters_json_flag_is_an_alias_for_format_json(monkeypatch, tmp_path, args):
    """docs/exit-codes.md: `--json` is accepted everywhere `--format json` is."""
    _patch(monkeypatch, tmp_path)
    by_format = _run(*args, "--format", "json")
    _patch(monkeypatch, tmp_path)
    by_flag = _run(*args, "--json")

    assert by_flag.exit_code == by_format.exit_code == 0
    assert json.loads(by_flag.output) == json.loads(by_format.output)


def test_clusters_show_by_id_prefix_or_name(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)

    by_prefix = _run("show", "c-")
    by_name = _run("show", "job")

    assert by_prefix.exit_code == 0 and "10.42.0.2" in by_prefix.output and "ssh root@1.2.3.1 -p 22001" in by_prefix.output
    assert by_name.exit_code == 0 and "Cluster c-1" in by_name.output


def test_clusters_show_hostfile_and_torchrun(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)

    assert _run("show", "c-1", "--hostfile").output == "10.42.0.1 slots=8\n10.42.0.2 slots=8\n"
    assert _run("show", "c-1", "--torchrun", "1").output.strip() == "--nnodes 2 --node_rank 1 --master_addr 10.42.0.1 --master_port 29500"
    assert "no node with rank 7" in _run("show", "c-1", "--torchrun", "7").output


def test_clusters_show_unknown(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)

    result = _run("show", "nope")

    assert result.exit_code != 0 and "No cluster 'nope'" in result.output


# --- rm -------------------------------------------------------------------------------------------

def test_clusters_rm_removes_every_member(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)

    result = _run("rm", "job", "-y")

    assert result.exit_code == 0, result.output
    assert FakeLium.removed == ["c-1"] and "pod-0" in result.output and "removed" in result.output


def test_clusters_rm_asks_first(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)

    result = _run("rm", "c-1", input="n\n")

    assert result.exit_code == 0 and FakeLium.removed == []


def test_clusters_rm_reports_a_member_that_stayed(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, fail_one=True)

    result = _run("rm", "c-1", "-y", "--format", "json")

    assert result.exit_code == 3
    payload = json.loads(result.output)
    assert payload["ok"] is False and [r["success"] for r in payload["results"]] == [True, False]
    assert "still billing" in payload["error"]


def test_clusters_output_keeps_api_text_that_looks_like_rich_markup(monkeypatch, tmp_path):
    """Text from the API or the user that Rich would read as markup reaches the terminal verbatim.

    Regression: the summary lines, the table cells and the prompts were f-strings printed with markup on — an IPv6
    overlay address `[fd00:42::1]` is a Rich tag, so `show` printed `MASTER_ADDR=` and an empty Overlay IP cell, `ps`
    dropped the `[v2]` of a cluster named `job[v2]`, `rm` printed `failed: node unreachable` for the API's
    `node [exec-1] unreachable`, and a `[/…]` in any of them raised MarkupError.
    """
    master, worker = _pod(0, name="job[v2]"), _pod(1, name="job[v2]")
    master.cluster_overlay_ip = "[fd00:42::1]"
    _patch(monkeypatch, tmp_path, mine=[Cluster(id="c-1", pods=[master, worker])])
    monkeypatch.setattr(
        FakeLium, "rm_cluster",
        lambda self, cluster: [{"pod": "pod-0", "success": True, "error": None},
                               {"pod": "pod-1", "success": False, "error": "node [exec-1] unreachable"}],
    )

    shown = _run("show", "c-1")
    listed = _run("ps")
    removed = _run("rm", "c-1", "-y")

    assert shown.exit_code == 0 and "MASTER_ADDR=[fd00:42::1]" in shown.output
    assert shown.output.count("[fd00:42::1]") == 2  # the summary line and the master's Overlay IP cell
    assert listed.exit_code == 0 and "job[v2]" in listed.output and "[fd00:42::1]" in listed.output
    assert removed.exit_code == 3 and "failed: node [exec-1] unreachable" in _flat(removed.output)
