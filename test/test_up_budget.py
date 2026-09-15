"""DAH-2565: stop a rental once it has spent $X.

A rental could be capped by time (--ttl/--until) but not by money. Billing is
price × wall time from creation, so a budget is a deadline; this turns
`--budget USD` into the same scheduled removal `--ttl` uses, and shows spend
against the cap in `lium ps`.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.actions import ActionResult
from lium.cli.ps import display as ps_display
from lium.cli.up import command as up_command
from lium.cli.up.budget import MIN_BUDGET_MINUTES, budget_deadline, budget_hours, rental_price_per_hour
from lium.cli.utils import EXIT_CONFIGURATION_ERROR, EXIT_GENERAL_ERROR
from lium.sdk import Config, ExecutorInfo, Lium, PodInfo
from lium.sdk.utils import parse_api_timestamp, spend_cap_deadline

CREATED = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
CREATED_STR = "2026-09-05T12:00:00Z"
PRICE = 2.50


def _executor(price: float | None = PRICE) -> ExecutorInfo:
    return ExecutorInfo(
        id="exec-1", huid="brave-fox-3a", machine_name="NVIDIA H100 80GB HBM3", gpu_type="H100",
        gpu_count=1, price_per_hour=price, price_per_gpu=price, location={}, specs={},
        status="active", docker_in_docker=False, ip="",
    )


def _pod(created_at: str | None = CREATED_STR, price: float | None = PRICE, removal: str | None = None) -> PodInfo:
    return PodInfo(
        id="pod-1", name="train", status="RUNNING", huid="eager-wolf-aa",
        ssh_cmd="ssh user@pod.example -p 20000", ports={}, created_at=created_at or "",
        updated_at=CREATED_STR, executor=_executor(price) if price is not None else None,
        template={}, removal_scheduled_at=removal, jupyter_installation_status=None, jupyter_url=None,
    )


# --- arithmetic -----------------------------------------------------------------------------


def test_spend_cap_deadline_is_budget_over_price_after_start():
    assert spend_cap_deadline(CREATED, PRICE, 12.50) == CREATED + timedelta(hours=5)


@pytest.mark.parametrize("budget, price", [(0, PRICE), (-1, PRICE), (10, 0), (10, None)])
def test_spend_cap_deadline_rejects_what_is_not_a_cap(budget, price):
    with pytest.raises(ValueError):
        spend_cap_deadline(CREATED, price, budget)


def test_parse_api_timestamp_handles_z_and_naive_forms():
    assert parse_api_timestamp("2026-09-05T12:00:00Z") == CREATED
    assert parse_api_timestamp("2026-09-05T12:00:00") == CREATED
    assert parse_api_timestamp(None) is None
    assert parse_api_timestamp("yesterday") is None


def test_budget_hours_needs_a_price():
    assert budget_hours(12.50, PRICE) == 5
    assert budget_hours(12.50, 0) is None
    assert budget_hours(12.50, None) is None


def test_budget_deadline_is_anchored_on_created_at():
    assert budget_deadline(_pod(), 12.50) == CREATED + timedelta(hours=5)


def test_budget_deadline_falls_back_to_now_without_created_at():
    now = CREATED + timedelta(minutes=3)

    assert budget_deadline(_pod(created_at=None), 12.50, now=now) == now + timedelta(hours=5)


def test_budget_deadline_uses_the_executor_price_seen_before_renting_when_the_pod_has_none():
    assert budget_deadline(_pod(price=None), 12.50, fallback_price=5.0) == CREATED + timedelta(hours=2.5)
    assert budget_deadline(_pod(price=None), 12.50) is None


def test_budget_deadline_prefers_the_pods_own_price_over_the_nodes():
    """A split rental bills price_per_gpu × GPUs rented (pod.price, which ps() puts on the pod's
    executor.price_per_hour), not the node's total that was on screen before renting."""
    split_pod = _pod(price=2.50)  # one GPU of a node whose total is $20/h

    assert budget_deadline(split_pod, 12.50, fallback_price=20.0) == CREATED + timedelta(hours=5)


def test_rental_price_is_per_gpu_times_count_for_a_split_and_the_node_total_otherwise():
    node = SimpleNamespace(price_per_hour=20.0, price_per_gpu=2.50, gpu_count=8)

    assert rental_price_per_hour(node, None) == 20.0
    assert rental_price_per_hour(node, 8) == 20.0
    assert rental_price_per_hour(node, 1) == 2.50
    assert rental_price_per_hour(SimpleNamespace(price_per_hour=None, price_per_gpu=None, gpu_count=1), 1) is None


# --- SDK ------------------------------------------------------------------------------------


def test_sdk_cap_spend_schedules_removal_at_the_deadline(monkeypatch):
    client = Lium(Config(api_key="test"))
    scheduled = {}
    monkeypatch.setattr(client, "schedule_termination", lambda pod, *, termination_time: scheduled.update(t=termination_time) or {})
    far_future_pod = _pod(created_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat())

    deadline = client.cap_spend(far_future_pod, budget_usd=PRICE * 2)

    assert deadline - parse_api_timestamp(far_future_pod.created_at) == timedelta(hours=2)
    assert scheduled["t"] == deadline.isoformat().replace("+00:00", "Z")


def test_sdk_cap_spend_keeps_an_earlier_scheduled_removal(monkeypatch):
    """A pod with a TTL removal in one hour and a budget worth five hours is capped at one hour, not
    extended to five — the same rule `lium up --budget --ttl` applies (arhangel66 on #218)."""
    client = Lium(Config(api_key="test"))
    scheduled = {}
    monkeypatch.setattr(client, "schedule_termination", lambda pod, *, termination_time: scheduled.update(t=termination_time) or {})
    now = datetime.now(timezone.utc).replace(microsecond=0)
    in_one_hour = (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    pod = _pod(created_at=(now - timedelta(minutes=10)).isoformat(), removal=in_one_hour)

    deadline = client.cap_spend(pod, budget_usd=PRICE * 5)

    assert deadline == now + timedelta(hours=1)
    assert scheduled["t"] == in_one_hour


def test_sdk_cap_spend_moves_a_later_scheduled_removal_up(monkeypatch):
    client = Lium(Config(api_key="test"))
    scheduled = {}
    monkeypatch.setattr(client, "schedule_termination", lambda pod, *, termination_time: scheduled.update(t=termination_time) or {})
    now = datetime.now(timezone.utc).replace(microsecond=0)
    pod = _pod(created_at=(now - timedelta(minutes=10)).isoformat(), removal=(now + timedelta(hours=5)).isoformat().replace("+00:00", "Z"))

    deadline = client.cap_spend(pod, budget_usd=PRICE * 2)

    assert deadline == now - timedelta(minutes=10) + timedelta(hours=2)
    assert scheduled["t"] == deadline.isoformat().replace("+00:00", "Z")


def test_sdk_cap_spend_refuses_a_removal_that_has_already_passed(monkeypatch):
    """A pod still listed after its scheduled removal time is the platform's to remove; re-posting the
    past time would be a 400 from the API, so it is a ValueError before any call."""
    client = Lium(Config(api_key="test"))
    monkeypatch.setattr(client, "schedule_termination", lambda *a, **k: pytest.fail("must not call the API"))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    pod = _pod(created_at=(now - timedelta(minutes=10)).isoformat(), removal=(now - timedelta(minutes=2)).isoformat().replace("+00:00", "Z"))

    with pytest.raises(ValueError, match="already scheduled for removal"):
        client.cap_spend(pod, budget_usd=PRICE * 5)


def test_sdk_cap_spend_refuses_an_already_spent_budget(monkeypatch):
    client = Lium(Config(api_key="test"))
    monkeypatch.setattr(client, "schedule_termination", lambda *a, **k: pytest.fail("must not schedule"))

    with pytest.raises(ValueError, match="already spent"):
        client.cap_spend(_pod(), budget_usd=0.01)   # created 2026-09-05, long spent


def test_sdk_cap_spend_needs_created_at_and_a_price():
    client = Lium(Config(api_key="test"))

    with pytest.raises(ValueError, match="created_at"):
        client.cap_spend(_pod(created_at=None), budget_usd=10)
    with pytest.raises(ValueError, match="price"):
        client.cap_spend(_pod(price=None), budget_usd=10)


# --- ps -------------------------------------------------------------------------------------


def test_ps_json_reports_the_spend_cap_when_removal_is_scheduled():
    row = ps_display.compact_pod(_pod(removal="2026-09-05T17:00:00Z"))

    assert row["spend_cap_usd"] == 12.50


def test_ps_json_spend_cap_is_none_without_a_schedule():
    assert ps_display.compact_pod(_pod())["spend_cap_usd"] is None


def test_ps_table_shows_spent_against_the_cap():
    with_cap = ps_display._format_spent(CREATED_STR, "2026-09-05T17:00:00Z", PRICE)
    without = ps_display._format_spent(CREATED_STR, None, PRICE)

    assert with_cap.endswith("/$12.50") and with_cap.startswith("$")
    assert "/" not in without


def test_ps_spent_column_is_wide_enough_for_a_three_digit_cap():
    """"$200.00/$480.00" is 15 characters; a narrower fixed column cropped it to "$200.00/$480.…"."""
    table, _ = ps_display.build_pods_table([_pod(removal="2026-09-05T17:00:00Z")])

    spent = next(column for column in table.columns if column.header == "Spent")
    assert spent.width >= len("$200.00/$480.00")


# --- lium up --budget -----------------------------------------------------------------------


def _run_up(monkeypatch, args, *, price=PRICE, pod=None, removed=None, executor=None, resolve_data=None, pod_name="train"):
    executor = executor or SimpleNamespace(
        id="exec-1", huid="brave-fox-3a", gpu_count=1, gpu_type="H100",
        price_per_hour=price, available_port_count=10, download_speed=1000,
    )
    scheduled = {}
    resolve_data = {"executor": executor, **(resolve_data or {})}

    class _Lium:
        # a server without workspaces: `up` reads it for its workspace line (lium#183)
        workspaces = SimpleNamespace(current=lambda: None)

        def get_deployment_estimate(self, *a, **k):
            return {}

        def down(self, pod):
            (removed if removed is not None else []).append(pod.id)
            return {}

    class _Resolve:
        def execute(self, ctx):
            return ActionResult(ok=True, data=resolve_data)

    class _Template:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"template": SimpleNamespace(id="tmpl-1")})

    class _Rent:
        def execute(self, ctx):
            scheduled["rented"] = True
            return ActionResult(ok=True, data={"pod_info": {}, "pod_id": "pod-1", "pod_name": pod_name})

    class _Wait:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"pod": pod or _pod()})

    class _Schedule:
        def execute(self, ctx):
            scheduled["at"] = ctx["termination_time"]
            return ActionResult(ok=True, data={"termination_time": ctx["termination_time"], "hours_until": 1})

    monkeypatch.setattr(up_command, "ensure_config", lambda: None)
    monkeypatch.setattr(up_command, "Lium", lambda **kwargs: _Lium())
    monkeypatch.setattr(up_command, "ResolveExecutorAction", _Resolve)
    monkeypatch.setattr(up_command, "ResolveTemplateAction", _Template)
    monkeypatch.setattr(up_command, "RentPodAction", _Rent)
    monkeypatch.setattr(up_command, "WaitReadyAction", _Wait)
    monkeypatch.setattr(up_command, "ScheduleTerminationAction", _Schedule)
    result = CliRunner().invoke(up_command.up_command, ["brave-fox-3a", "--yes", "--no-ssh", *args])
    return result, scheduled


def _fresh_pod(minutes_ago: int = 3) -> tuple[PodInfo, datetime]:
    """A pod created a few minutes ago, so a budget deadline lies ahead of now."""
    created = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=minutes_ago)
    return _pod(created_at=created.strftime("%Y-%m-%dT%H:%M:%SZ")), created


def test_up_budget_schedules_removal_when_the_budget_runs_out(monkeypatch):
    pod, created = _fresh_pod()
    result, scheduled = _run_up(monkeypatch, ["--budget", "12.50"], pod=pod)

    assert result.exit_code == 0, result.output
    assert scheduled["at"] == created + timedelta(hours=5)
    assert "Budget $12.50 at $2.50/h ≈ 5.0h" in result.output
    assert "Spend cap $12.50" in result.output


def test_up_budget_messages_show_a_bracketed_pod_name_literally(monkeypatch):
    """`[v2]` in a pod name is Rich markup unless escaped: the label would swallow it (or raise on `[/x]`)."""
    pod, _ = _fresh_pod()
    result, _ = _run_up(monkeypatch, ["--budget", "12.50"], pod=pod, pod_name="train[v2]")
    assert result.exit_code == 0, result.output
    assert "(name: train[v2], id: pod-1)" in result.output


def test_up_budget_and_ttl_keep_the_earlier_deadline(monkeypatch):
    pod, created = _fresh_pod()
    result, scheduled = _run_up(monkeypatch, ["--budget", "12.50", "--ttl", "10h"], pod=pod)
    assert result.exit_code == 0, result.output
    # --ttl 10h from now is later than creation + 5 h: the budget wins.
    assert scheduled["at"] == created + timedelta(hours=5)


def test_up_budget_spent_during_startup_removes_the_pod_instead_of_scheduling_the_past(monkeypatch):
    """A deadline already behind us is a 400 from the API and an uncapped pod: the
    cap is honoured by removing the pod now, and the exit code says so."""
    removed: list = []
    # created 2026-09-05 (the fixture default): $12.50 at $2.50/h ran out long ago
    result, scheduled = _run_up(monkeypatch, ["--budget", "12.50"], removed=removed)

    assert result.exit_code == EXIT_GENERAL_ERROR, result.output
    assert "at" not in scheduled, "a past termination time must never be sent"
    assert removed == ["pod-1"]
    assert "budget" in result.output.lower() and "removed" in result.output.lower()

    result, scheduled = _run_up(monkeypatch, ["--budget", "250000", "--ttl", "1h"])
    assert result.exit_code == 0, result.output
    assert scheduled["at"] - datetime.now(timezone.utc) < timedelta(hours=1, minutes=1)


def test_up_budget_that_buys_under_five_minutes_is_refused_before_renting(monkeypatch):
    tiny = PRICE * (MIN_BUDGET_MINUTES - 1) / 60
    result, scheduled = _run_up(monkeypatch, ["--budget", f"{tiny:.4f}"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR, result.output
    assert "rented" not in scheduled
    assert f"minimum is {MIN_BUDGET_MINUTES} min" in result.output


def test_up_budget_without_a_count_prices_the_free_gpus_not_the_whole_host(monkeypatch):
    """A rent with no --count gets the node's free GPUs and bills for those (DAH-2877): on a split host
    with 2 of 8 GPUs free, $1 buys 6 min at 2 × $5/h, not 1.5 min at the host's $40/h."""
    split_host = SimpleNamespace(
        id="exec-1", huid="brave-fox-3a", gpu_count=8, available_gpu_count=2, gpu_type="H100",
        price_per_hour=40.0, price_per_gpu=5.0, available_port_count=10, download_speed=1000,
    )
    pod, _created = _fresh_pod(minutes_ago=1)
    result, scheduled = _run_up(monkeypatch, ["--budget", "1.00"], pod=pod, executor=split_host)

    assert result.exit_code == 0, result.output
    assert scheduled.get("rented") is True
    assert "Budget $1.00 at $10.00/h" in result.output


def test_up_budget_on_the_spec_path_uses_the_servers_price(monkeypatch):
    """`--gpu H100 --budget`: the server picked and priced the rent (#209) — a 1-GPU split of an
    8×H100 host at $2/GPU/h bills $2/h, so $1 buys 30 min; pricing the host's free GPUs instead
    would say $16/h and refuse the budget."""
    host = SimpleNamespace(
        id="exec-1", huid="brave-fox-3a", gpu_count=8, available_gpu_count=8, gpu_type="H100",
        price_per_hour=16.0, price_per_gpu=2.0, available_port_count=10, download_speed=1000, location={},
    )
    pod, _created = _fresh_pod(minutes_ago=1)
    result, scheduled = _run_up(
        monkeypatch, ["--budget", "1.00"], pod=pod, executor=host,
        resolve_data={"price_per_hour": 2.0, "gpu_count": 1, "spec": {"gpu_type": "H100", "gpu_count": 1}},
    )

    assert result.exit_code == 0, result.output
    assert scheduled.get("rented") is True
    assert "Budget $1.00 at $2.00/h" in result.output


def test_up_budget_without_a_node_price_is_refused_before_renting(monkeypatch):
    result, scheduled = _run_up(monkeypatch, ["--budget", "10"], price=0)

    assert result.exit_code == EXIT_CONFIGURATION_ERROR, result.output
    assert "rented" not in scheduled


def test_up_budget_must_be_positive():
    result = CliRunner().invoke(up_command.up_command, ["brave-fox-3a", "--budget", "0"])

    assert result.exit_code == 2
    assert "--budget" in result.output


def test_up_without_budget_schedules_nothing(monkeypatch):
    result, scheduled = _run_up(monkeypatch, [])

    assert result.exit_code == 0, result.output
    assert "at" not in scheduled
