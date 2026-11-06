"""`lium clusters` — list fabrics, rent a cluster, inspect members, remove — with the SDK mocked."""

import json

import pytest
from click.testing import CliRunner

from lium.cli import interactive
from lium.cli.cli import cli
from lium.cli.clusters import command as clusters_command
from lium.sdk import (
    Cluster,
    ClusterOffer,
    ExecutorInfo,
    LiumNotFoundError,
    LiumPermissionError,
    LiumServerError,
    PodInfo,
)

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
        """The SDK's per-member rows from `DELETE /clusters/{id}`; `rm_error` (an SDK exception) is raised instead."""
        FakeLium.removed.append(cluster.id)
        if self.rm_error is not None:
            raise self.rm_error
        rows = []
        for p in cluster.pods:
            failed = p.id == "pod-1" and getattr(self, "fail_one", False)
            message = "Pod deletion failed; call again to retry this member, or delete the pod on its own" if failed else "Pod deletion started"
            rows.append({"pod": p.id, "huid": p.huid, "name": p.name, "node_rank": p.cluster_node_index, "success": not failed,
                         "message": message, "error": message if failed else None})
        return rows


def _patch(monkeypatch, tmp_path, **overrides):
    FakeLium.up_calls, FakeLium.removed, FakeLium.scheduled, FakeLium.up_result = [], [], [], None
    FakeLium.wait_calls, FakeLium.wait_result, FakeLium.schedule_fail_on, FakeLium.rm_error = [], None, None, None
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


def test_clusters_up_schedules_the_ttl_before_waiting(monkeypatch, tmp_path):
    """A --wait timeout must not leave N nodes billing with nothing scheduled."""
    _patch(monkeypatch, tmp_path, wait_result=TimeoutError("Cluster c-1 not ready after 900s: job=PENDING"))

    result = _run("up", FABRIC, "--nodes", "2", "-n", "job", "-y", "--ttl", "2h")

    assert result.exit_code != 0
    assert [p for p, _ in FakeLium.scheduled] == ["pod-0", "pod-1"]
    assert "terminates at" in _flat(result.output)


@pytest.mark.parametrize("args, backups", [((), True), (("--no-backup",), False)])
def test_clusters_up_says_backups_are_on_unless_no_backup_turns_them_off(monkeypatch, tmp_path, args, backups):
    _patch(monkeypatch, tmp_path)

    result = _run("up", FABRIC, "--nodes", "2", "-n", "job", "-y", "--no-wait", *args)

    assert result.exit_code == 0, result.output
    assert FakeLium.up_calls[0][1]["enable_backups"] is backups
    assert ("Backups on:" in _flat(result.output)) is backups


def test_clusters_rm_reports_a_member_that_stayed(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, fail_one=True)

    result = _run("rm", "c-1", "-y", "--format", "json")

    assert result.exit_code == 3
    payload = json.loads(result.output)
    assert payload["ok"] is False and [r["success"] for r in payload["results"]] == [True, False]
    assert payload["results"][1]["error"].startswith("Pod deletion failed")
    assert "still billing" in payload["error"] and "lium clusters rm c-1" in payload["error"]


def test_clusters_up_and_rm_ask_first_in_json_mode_too(monkeypatch, tmp_path):
    """Money is spent only after -y or an answered prompt, whatever the output format (as `lium up` does)."""
    _patch(monkeypatch, tmp_path)

    result = _run("up", FABRIC, "--nodes", "2", "-n", "job", "--format", "json", "--no-wait", input="n\n")
    assert result.exit_code == 0 and FakeLium.up_calls == [], result.output
    assert "[y/n]" not in result.stdout, "the prompt goes to stderr: stdout is the JSON document"

    result = _run("rm", "c-1", "--format", "json", input="n\n")
    assert result.exit_code == 0 and FakeLium.removed == [], result.output
    assert "[y/n]" not in result.stdout


def test_clusters_up_asks_before_spending_and_names_the_price(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)

    result = _run("up", FABRIC, "--nodes", "2", "-n", "job", input="n\n")

    assert result.exit_code == 0 and "$33.00/h" in result.output and FakeLium.up_calls == []


def test_clusters_rm_asks_first(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)

    result = _run("rm", "c-1", input="n\n")

    assert result.exit_code == 0 and FakeLium.removed == []


def test_clusters_rm_404_from_the_route_is_exit_5_and_removes_nothing_per_pod(monkeypatch, tmp_path):
    """The listing showed the cluster, then `DELETE /clusters/{id}` answered 404: it was removed meanwhile, or the
    server has no such route. Regression: the old client fell through to per-pod deletes. Now: exit 5
    (`cluster_not_found`), the hint names `lium clusters ps` and `lium rm`, and no per-pod call is made."""
    _patch(monkeypatch, tmp_path, rm_error=LiumNotFoundError("Resource not found: Cluster not found"))

    result = _run("rm", "c-1", "-y", "--format", "json")

    assert result.exit_code == 5, result.output
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "cluster_not_found" and "Cluster not found" in error["message"]
    assert "Nothing was deleted" in error["message"] and "lium rm <huid>" in error["hint"]
    assert FakeLium.removed == ["c-1"]  # the one cluster call; FakeLium has no per-pod rm to fall back to


def test_clusters_rm_403_is_permission_denied_exit_6(monkeypatch, tmp_path):
    """A member of another user, or an API key without the manage scope: the server refuses the whole cluster.
    Regression guarded: the command's `except LiumNotFoundError` must not widen to `LiumError`, or a 403 would come
    out as exit 5 `cluster_not_found` saying the cluster is gone while it is billing."""
    _patch(monkeypatch, tmp_path, rm_error=LiumPermissionError("Permission denied: You do not have permission to access this pod"))

    result = _run("rm", "c-1", "-y", "--format", "json")

    assert result.exit_code == 6, result.output
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "permission_denied" and "You do not have permission" in error["message"]


NOTICE = "Backups need a paid deposit: this cluster rents without a backup."


@pytest.mark.parametrize("fmt", ["table", "json"])
@pytest.mark.parametrize("failure", [None, "wait", "member", "ttl"])
def test_clusters_up_keeps_the_backup_notice_whatever_fails_after_the_rent(monkeypatch, tmp_path, fmt, failure):
    """DAH-5023: the cluster rented and bills whatever fails next, so its backup notice is never lost."""
    from lium.sdk import PodStartError

    overrides = {"up_result": Cluster(id="c-1", pods=[_pod(0), _pod(1)], backup_notice=NOTICE)}
    if failure == "wait":
        overrides["wait_result"] = TimeoutError("Cluster c-1 not ready after 900s: job=PENDING")
    elif failure == "member":
        overrides["wait_result"] = PodStartError("member failed", pod_id="pod-1", status="FAILED")
    elif failure == "ttl":
        overrides["schedule_fail_on"] = "pod-1"
    _patch(monkeypatch, tmp_path, **overrides)

    result = _run("up", FABRIC, "--nodes", "2", "-n", "job", "-y", "--ttl", "2h", "--format", fmt)

    assert (result.exit_code == 0) is (failure is None), result.output
    if fmt == "table":
        assert NOTICE in _flat(result.output)
    elif failure in (None, "ttl"):  # a document on stdout
        assert json.loads(result.stdout)["backup_notice"] == NOTICE
    else:  # the error envelope on stderr
        assert json.loads(result.stderr.strip().splitlines()[-1])["data"]["backup_notice"] == NOTICE


@pytest.mark.parametrize("fmt", ["table", "json"])
def test_clusters_up_confirmed_but_not_listed_keeps_the_backup_notice(monkeypatch, tmp_path, fmt):
    """DAH-5023: a confirmed rent the listing does not show yet still bills, so its backup notice is shown."""
    from lium.sdk.exceptions import ClusterNotListedError

    error = ClusterNotListedError("confirmed but not listed", pod_ids=["pod-0", "pod-1"], backup_notice=NOTICE)
    _patch(monkeypatch, tmp_path, up_result=error)

    result = _run("up", FABRIC, "--nodes", "2", "-n", "job", "-y", "--format", fmt)

    assert result.exit_code != 0, result.output
    if fmt == "table":
        assert NOTICE in _flat(result.output)
    else:
        envelope = json.loads(result.stderr.strip().splitlines()[-1])
        assert envelope["error"]["code"] == "cluster_rented_not_listed"
        assert envelope["data"]["backup_notice"] == NOTICE


def _replacement(status: str, created_at: str = "2026-10-30T12:00:00Z") -> PodInfo:
    pod = _pod(1, status=status)
    pod.id, pod.created_at, pod.executor = "pod-1b", created_at, _node(5)
    return pod


@pytest.mark.parametrize(
    "old_status, new_status, carrier",
    [
        ("RUNNING", "PENDING", "pod-1"),
        ("RUNNING", "FAILED", "pod-1"),
        ("RUNNING", "RUNNING", "pod-1b"),
        ("DELETING", "RUNNING", "pod-1b"),
        ("RUNNING", "DELETING", "pod-1"),
    ],
    ids=["pending", "failed", "running-old-not-yet-deleting", "running-old-deleting", "replacement-deleting"],
)
def test_a_node_being_replaced_is_one_node_in_the_launch_output(old_status, new_status, carrier):
    # DAH-5442: during a replacement index 1 holds two pods; launchers must still see two nodes, ranks 0 and 1
    old = _pod(1, status=old_status)
    old.created_at = "2026-10-30T10:00:00Z"
    cluster = Cluster(id="c-1", pods=[_pod(0), old, _replacement(new_status)])

    assert [pod.id for pod in cluster.nodes] == ["pod-0", carrier]
    assert cluster.size == 2
    assert cluster.hostfile() == "10.42.0.1 slots=8\n10.42.0.2 slots=8\n"
    assert cluster.torchrun_args(cluster.pods[2]) == (
        "--nnodes 2 --node_rank 1 --master_addr 10.42.0.1 --master_port 29500"
    )
