"""`lium mcp`: the MCP server an agent host runs to rent and use Lium GPUs (lium/cli/mcp)."""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.mcp import tools as tools_module
from lium.cli.mcp.tools import OUTPUT_TAIL_CHARS, LiumTools
from lium.sdk import ExecutorInfo, Lium, PodInfo
from lium.sdk.exceptions import (
    LiumError,
    LiumInsufficientBalanceError,
    LiumNotFoundError,
    LiumRentOutcomeUnknownError,
    PodStartError,
)

TOOL_NAMES = {"list_gpus", "rent_pod", "list_pods", "run_command", "remove_pod", "get_balance"}


def _node(node_id: str, price_per_gpu: float) -> ExecutorInfo:
    return ExecutorInfo(
        id=node_id, huid=f"huid-{node_id}", machine_name="NVIDIA H100 80GB HBM3", gpu_type="H100", gpu_count=8,
        price_per_hour=price_per_gpu * 8, price_per_gpu=price_per_gpu, location={"country": "US"}, specs={},
        status="available", docker_in_docker=False, ip="10.0.0.1", available_gpu_count=8,
    )


def _pod(pod_id: str, name: str) -> PodInfo:
    return PodInfo(
        id=pod_id, name=name, status="RUNNING", huid=f"huid-{pod_id}", ssh_cmd="ssh root@10.0.0.1 -p 2200", ports={},
        created_at="2026-10-13T00:00:00Z", updated_at="2026-10-13T00:00:00Z", executor=_node("n1", 2.0), template={},
        removal_scheduled_at=None, jupyter_installation_status=None, jupyter_url=None,
    )


class FakeLium:
    def __init__(self):
        self.pods = [_pod("p1", "job")]
        self.removed = []
        self.exec_calls = []

    def ls(self, gpu_type=None, gpu_count=None):
        return [_node("expensive", 3.0), _node("cheap", 1.5), _node("mid", 2.0)]

    def get_executor(self, executor):
        return next((n for n in self.ls() if executor in (n.id, n.huid)), None)

    def balance_or_none(self):
        return 60.0

    def ps(self):
        return self.pods

    next_pod = 2
    up_calls = 0

    def up(self, **kwargs):
        self.up_kwargs = kwargs
        self.up_calls += 1
        pod_id = f"p{self.next_pod}"
        self.next_pod += 1
        self.pods = [*self.pods, _pod(pod_id, kwargs["name"])]
        return {"id": pod_id, "name": kwargs["name"], "status": "PENDING"}

    ready_result = "ready"

    def wait_ready(self, created, timeout, on_poll=None):
        self.wait_timeout = timeout
        if on_poll:
            on_poll(None, "PENDING", 0.0)
        if isinstance(self.ready_result, Exception):
            raise self.ready_result
        return _pod(created["id"], created["name"]) if self.ready_result == "ready" else None

    exec_error = None

    def exec(self, pod, command, timeout, max_output_bytes):
        self.exec_calls.append((pod.id, command, timeout))
        self.max_output_bytes = max_output_bytes
        if self.exec_error:
            raise self.exec_error
        return {"exit_code": 0, "stdout": "x" * (OUTPUT_TAIL_CHARS + 10), "stderr": ""}

    def rm(self, pod):
        self.removed.append(pod.id)
        self.pods = [p for p in self.pods if p.id != pod.id]

    def balance(self):
        raise LiumInsufficientBalanceError("Balance too low", code="insufficient_balance", hint="lium fund")


_SERVERS_STAY_LIVE = []  # non-empty: every server made stays running beside the others


@pytest.fixture(autouse=True)
def rental_state_dir(tmp_path, monkeypatch):
    """Unknown rentals are kept on disk; never in the real home directory."""
    monkeypatch.setattr(tools_module, "DEFAULT_RENTAL_STATE_DIR", tmp_path / "pending-rentals")
    monkeypatch.setattr(LiumTools, "BACKGROUND_CLEANUP", False)  # tests that want the background thread turn it on
    # Tests restart the server in this process. A new store stands for the next process: the earlier server has
    # exited, and the OS has dropped its journal locks (tests of two live servers set _SERVERS_STAY_LIVE).
    stores, init = [], tools_module._RentalStore.__init__

    def exit_earlier_servers(self, directory):
        if not _SERVERS_STAY_LIVE:
            for store in stores:
                for fd in store._held.values():
                    os.close(fd)
                store._held.clear()
        stores.append(self)
        init(self, directory)

    monkeypatch.setattr(tools_module._RentalStore, "__init__", exit_earlier_servers)
    return tmp_path / "pending-rentals"


@pytest.fixture
def fake():
    return FakeLium()


@pytest.fixture
def tools(fake):
    tools = LiumTools(client_factory=lambda: fake)
    tools.BACKGROUND_CLEANUP = False  # tests drive cleanup through rent_pod's own reconcile; one test turns it on
    return tools


def test_list_gpus_returns_the_cheapest_nodes_first_up_to_the_limit(tools):
    result = tools.list_gpus(limit=2)

    assert result["ok"] is True
    assert [n["executor_id"] for n in result["nodes"]] == ["cheap", "mid"]
    assert result["nodes"][0]["price_per_gpu_hour_usd"] == 1.5


def test_rent_pod_waits_for_the_pod_and_returns_it(tools, fake):
    result = tools.rent_pod("cheap", "agent-job")

    assert result["ok"] is True
    assert result["pod"]["name"] == "agent-job"
    assert fake.up_kwargs["executor_id"] == "cheap" and fake.up_kwargs["wait"] is False
    assert fake.wait_timeout == 600


@pytest.mark.parametrize("outcome", [None, TimeoutError("ssh probe timed out")])
def test_a_slow_rental_returns_the_billing_pod_id_and_says_not_to_rent_again(tools, fake, outcome):
    fake.ready_result = outcome

    result = tools.rent_pod("cheap", "agent-job", timeout_seconds=5)

    assert result["ok"] is True and result["status"] == "pending"
    assert result["pod_id"] == "p2"
    assert "Do NOT call rent_pod again" in result["next"]


def test_list_gpus_shows_tier_reliability_and_the_rent_price_for_the_free_gpus(fake, tools):
    node = _node("cheap", 2.5)
    node.tier, node.reliability_score, node.available_gpu_count = "spot", 81.2, 4
    fake.ls = lambda gpu_type=None, gpu_count=None: [node]

    listed = tools.list_gpus()["nodes"][0]

    assert (listed["tier"], listed["reliability_score"], listed["rent_price_per_hour_usd"]) == ("spot", 81.2, 10.0)


def test_rent_pod_takes_the_id_or_rent_url_docs_search_gpus_returns(tools, fake):
    assert tools.rent_pod("https://lium.io/browse-pods/cheap", "job")["ok"] is True
    assert fake.up_kwargs["executor_id"] == "cheap"


def test_a_dry_run_quotes_cost_and_balance_and_rents_nothing(tools, fake):
    from lium.cli.mcp.tools import tool_functions

    rent_pod = next(f for f in tool_functions(tools) if f.__name__ == "rent_pod")

    quote = asyncio.run(rent_pod("https://lium.io/browse-pods/cheap", "job", dry_run=True))

    assert quote["ok"] is True and fake.up_calls == 0
    assert quote["node"]["executor_id"] == "cheap" and quote["gpus_rented"] == 8
    assert (quote["cost_per_hour_usd"], quote["balance_usd"], quote["hours_balance_covers"]) == (12.0, 60.0, 5.0)
    assert asyncio.run(rent_pod("gone", "job", dry_run=True))["error"]["code"] == "node_not_found"


def test_a_pod_that_fails_to_start_is_an_error_that_names_it(tools, fake):
    fake.ready_result = PodStartError("pod p2 FAILED", pod_id="p2", status="FAILED")

    result = tools.rent_pod("cheap", "agent-job")

    assert result["ok"] is False and result["pod_id"] == "p2"
    assert result["error"]["code"] == "pod_start_failed" and "remove_pod" in result["error"]["hint"]


def test_run_command_finds_the_pod_by_name_and_keeps_only_the_output_tail(tools, fake):
    result = tools.run_command("job", "nvidia-smi")

    assert fake.exec_calls == [("p1", "nvidia-smi", 300)]
    assert fake.max_output_bytes == tools_module.MAX_OUTPUT_BYTES
    assert result["exit_code"] == 0
    assert result["stdout"].startswith("…[cut to the last")
    assert len(result["stdout"]) < OUTPUT_TAIL_CHARS + 100


def test_a_command_that_floods_output_stops_at_the_limit_instead_of_buffering_it(tools, fake):
    from contextlib import nullcontext
    from types import SimpleNamespace

    read = [0]

    def recv(size):  # `yes`: output never ends
        read[0] += size
        return b"y\n" * (size // 2)

    channel = SimpleNamespace(
        recv=recv, recv_stderr=lambda size: b"", recv_ready=lambda: True, recv_stderr_ready=lambda: False,
        exit_status_ready=lambda: False, close=lambda: None, recv_exit_status=lambda: 0,
    )
    ssh = SimpleNamespace(
        exec_command=lambda command: (SimpleNamespace(close=lambda: None), SimpleNamespace(channel=channel), None),
    )
    client = SimpleNamespace(_remote_command=lambda pod, command, **kw: nullcontext(ssh.exec_command(command)))
    fake.exec = lambda pod, command, timeout, max_output_bytes: Lium.exec(
        client, pod, command=command, timeout=timeout, max_output_bytes=max_output_bytes
    )

    result = tools.run_command("job", "yes")

    assert result["ok"] is False and result["error"]["code"] == "output_too_large"
    assert "may still be running" in result["error"]["message"]  # closing the session does not end the process
    assert "ps aux" in result["error"]["hint"] and "out.log" in result["error"]["hint"]
    assert read[0] < tools_module.MAX_OUTPUT_BYTES + 1024 * 1024  # stopped near the limit, not after gigabytes


def test_a_command_timeout_does_not_echo_the_command(tools, fake):
    command = "HF_TOKEN=secret python train.py"
    fake.exec_error = TimeoutError(f"Command did not finish within 300s on pod job: {command}")

    result = tools.run_command("p1", command)

    assert result["ok"] is False and result["error"]["code"] == "command_timeout"
    assert "secret" not in json.dumps(result)


def test_other_command_errors_have_the_command_cut_out(tools, fake):
    command = "HF_TOKEN=secret python train.py"
    fake.exec_error = RuntimeError(f"ssh failed running {command}")

    result = tools.run_command("p1", command)

    assert "secret" not in json.dumps(result) and "<command>" in result["error"]["message"]


def test_remove_pod_of_an_unknown_pod_is_an_error_result_not_an_exception(tools, fake):
    result = tools.remove_pod("nope")

    assert result["ok"] is False
    assert result["error"]["code"] == "LookupError"
    assert fake.removed == []


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("method", ["remove_pod", "run_command"])
@pytest.mark.parametrize(
    "query,names,expected",
    [
        ("p2", ("p2", "other"), "p2"),
        ("huid-p2", ("one", "two"), "p2"),
        ("two", ("one", "two"), "p2"),
        ("job", ("job", "job"), None),
        ("huid-p2", ("huid-p2", "other"), None),
        ("missing", ("one", "two"), None),
    ],
)
def test_pod_target_resolution(tools, fake, reverse, method, query, names, expected):
    fake.pods = [_pod("p1", names[0]), _pod("p2", names[1])]
    if reverse:
        fake.pods.reverse()
    args = (query, "true") if method == "run_command" else (query,)

    result = getattr(tools, method)(*args)

    targets = fake.removed if method == "remove_pod" else [call[0] for call in fake.exec_calls]
    assert targets == ([expected] if expected else [])
    assert result["ok"] is (expected is not None)


def test_an_exact_id_wins_over_another_pod_named_with_it(tools, fake):
    fake.pods = [_pod("p1", "p2"), _pod("p2", "job")]

    assert tools.remove_pod("p2")["removed"] == "p2"


def test_an_sdk_error_keeps_its_code_and_hint(tools):
    result = tools.get_balance()

    assert result == {
        "ok": False,
        "error": {"code": "insufficient_balance", "message": "Balance too low", "hint": "lium fund"},
    }


def test_the_server_registers_every_tool_and_answers_a_call(tools):
    pytest.importorskip("mcp")
    from lium.cli.mcp.command import build_server

    server = build_server(tools)
    listed = asyncio.run(server.list_tools())
    assert {tool.name for tool in listed} == TOOL_NAMES

    result = asyncio.run(server.call_tool("list_pods", {}))
    text = json.dumps(result, default=lambda o: getattr(o, "text", str(o)))
    assert '"job"' in text or "job" in text


@pytest.mark.parametrize("slow_method,tool,arguments", [
    ("up", "rent_pod", {"executor_id": "cheap", "name": "job"}),
    ("exec", "run_command", {"pod": "p1", "command": "sleep 600"}),
])
def test_a_slow_tool_call_does_not_block_the_others(tools, fake, monkeypatch, slow_method, tool, arguments):
    pytest.importorskip("mcp")
    from lium.cli.mcp.command import build_server

    server = build_server(tools)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = getattr(fake, slow_method)

    def slow(*args, **kwargs):
        started.set()
        release.wait(2)  # bounds the failure when the dispatcher blocks
        finished.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(fake, slow_method, slow)

    async def check():
        task = asyncio.create_task(server.call_tool(tool, arguments))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            await server.call_tool("list_pods", {})
            assert not finished.is_set()
        finally:
            release.set()
            await asyncio.wait_for(task, 5)

    asyncio.run(check())


def test_every_tool_description_says_when_to_use_it(tools):
    from lium.cli.mcp.tools import tool_functions

    # MCP directories (Glama) grade descriptions that open with when to call the tool
    for function in tool_functions(tools):
        assert function.__doc__.startswith("Use this when "), function.__name__


def test_a_rental_cancelled_mid_call_removes_its_pod_before_a_retry_rents(tools, fake, monkeypatch):
    from lium.cli.mcp.tools import tool_functions

    tools.BACKGROUND_CLEANUP = True
    rent_pod = next(f for f in tool_functions(tools) if f.__name__ == "rent_pod")
    started, release = threading.Event(), threading.Event()
    original_up, events = fake.up, []

    def slow_up(**kwargs):
        events.append("up")
        started.set()
        release.wait(2)
        return original_up(**kwargs)

    def rm(pod):
        events.append(f"rm {pod.id}")
        fake.removed.append(pod.id)
        fake.pods = [p for p in fake.pods if p.id != pod.id]

    monkeypatch.setattr(fake, "up", slow_up)
    monkeypatch.setattr(fake, "rm", rm)

    async def check():
        task = asyncio.create_task(rent_pod("cheap", "job"))
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        release.set()
        assert (await rent_pod("cheap", "job"))["ok"] is True

    asyncio.run(check())
    assert events == ["up", "rm p2", "up"]


def test_a_rental_cancelled_after_its_worker_finished_still_removes_the_unseen_pod(tools, fake):
    from lium.cli.mcp.tools import _Rental

    rental = _Rental()
    tools.rent_pod("cheap", "job", rental=rental)  # finished; the host cancelled before the result reached it

    tools.cancel_rental(rental)
    retry = tools.rent_pod("other-node", "again")

    assert retry["ok"] is True
    assert fake.removed == ["p2"] and fake.up_calls == 2


def _failing_until_fixed(original, error, fixed):
    def call(*args, **kwargs):
        if not fixed.is_set():
            raise error
        return original(*args, **kwargs)

    return call


@pytest.mark.parametrize("failure", ["pending_delete_refused", "delete_timeout", "listing_failed"])
def test_an_unconfirmed_cleanup_blocks_renting_until_the_pod_is_confirmed_removed(tools, fake, monkeypatch, failure):
    from lium.cli.mcp.tools import _Rental

    tools.CLEANUP_WAIT_SECONDS = 0.05
    rental, fixed = _Rental(), threading.Event()
    tools.rent_pod("node-a", "first", rental=rental)
    if failure == "pending_delete_refused":
        monkeypatch.setattr(fake, "rm", _failing_until_fixed(fake.rm, LiumError("Cannot delete a PENDING pod"), fixed))
    elif failure == "delete_timeout":
        monkeypatch.setattr(fake, "rm", _failing_until_fixed(fake.rm, TimeoutError("delete acknowledgement lost"), fixed))
    else:
        monkeypatch.setattr(fake, "ps", _failing_until_fixed(fake.ps, ConnectionError("ps failed"), fixed))

    tools.cancel_rental(rental)
    retry = tools.rent_pod("node-b", "retry")

    assert retry["ok"] is False and retry["error"]["code"] == "cleanup_pending"
    assert retry["unresolved"][0]["pod_ids"] == ["p2"]
    assert fake.up_calls == 1
    if failure != "listing_failed":
        assert tools.list_pods()["unresolved_rentals"][0]["pod_ids"] == ["p2"]

    monkeypatch.setattr(time, "monotonic", lambda real=time.monotonic: real() + 86_400)  # a day later: still held
    assert tools.rent_pod("node-b", "retry")["error"]["code"] == "cleanup_pending" and fake.up_calls == 1
    fixed.set()
    retry = tools.rent_pod("node-b", "retry")

    assert retry["ok"] is True
    assert fake.removed == ["p2"] and fake.up_calls == 2
    assert "unresolved_rentals" not in tools.list_pods()


def test_removing_the_pod_by_hand_settles_an_unresolved_rental(tools, fake, monkeypatch):
    from lium.cli.mcp.tools import _Rental

    tools.CLEANUP_WAIT_SECONDS = 0.05
    rental = _Rental()
    tools.rent_pod("node-a", "first", rental=rental)
    original_rm = fake.rm
    monkeypatch.setattr(fake, "rm", lambda pod: (_ for _ in ()).throw(LiumError("Cannot delete a PENDING pod")))
    tools.cancel_rental(rental)
    assert tools.rent_pod("node-b", "retry")["error"]["code"] == "cleanup_pending"

    monkeypatch.setattr(fake, "rm", original_rm)
    assert tools.remove_pod("p2")["ok"] is True

    assert tools.rent_pod("node-b", "retry")["ok"] is True


@pytest.mark.parametrize("answer", ["removed", "not_found"])
def test_a_pod_the_listing_does_not_show_is_still_removed_by_its_id(tools, fake, monkeypatch, answer):
    from lium.cli.mcp.tools import _Rental

    rental, asked = _Rental(), []
    tools.rent_pod("node-a", "first", rental=rental)
    fake.pods = [p for p in fake.pods if p.id != "p2"]  # the API named p2, the listing lags

    def rm(pod):
        asked.append(pod.id)
        if answer == "not_found":
            raise LiumNotFoundError("Resource not found: pod")

    monkeypatch.setattr(fake, "rm", rm)
    tools.cancel_rental(rental)

    assert tools.rent_pod("node-b", "retry")["ok"] is True
    assert asked == ["p2"]


@pytest.mark.parametrize("created", [True, False])
@pytest.mark.parametrize("seconds_later", [0, 121, 86_400])
def test_a_rent_call_with_no_answer_blocks_renting_however_long_ago_it_was(tools, fake, monkeypatch, created, seconds_later):
    tools.CLEANUP_WAIT_SECONDS = 0.05
    original_up = fake.up

    def lost_answer(**kwargs):
        if created:
            original_up(**kwargs)
        else:
            fake.up_calls += 1
        raise LiumRentOutcomeUnknownError("no answer, repeat refused", idempotency_key="key-1", pod_name=kwargs["name"])

    monkeypatch.setattr(fake, "up", lost_answer)
    first = tools.rent_pod("node-a", "first")
    assert first["error"]["code"] == "rent_outcome_unknown"
    monkeypatch.setattr(fake, "up", original_up)
    monkeypatch.setattr(time, "monotonic", lambda real=time.monotonic: real() + seconds_later)

    retry = tools.rent_pod("node-b", "retry")

    assert retry["error"]["code"] == "cleanup_pending"
    assert fake.up_calls == 1 and fake.removed == []
    unresolved = tools.list_pods()["unresolved_rentals"][0]
    assert unresolved["outcome"] == "unknown" and unresolved["idempotency_key"] == "key-1"
    assert unresolved["same_name_pods_to_check"] == (["p2"] if created else [])


def test_a_pod_someone_else_rented_with_the_same_name_is_never_removed(tools, fake, monkeypatch):
    tools.CLEANUP_WAIT_SECONDS = 0.05

    def lost_answer(**kwargs):
        raise LiumRentOutcomeUnknownError("no answer, repeat refused", idempotency_key="key-1", pod_name=kwargs["name"])

    monkeypatch.setattr(fake, "up", lost_answer)
    tools.rent_pod("node-a", "job-7")
    fake.pods = [*fake.pods, _pod("theirs", "job-7")]  # another client rents a pod with this name on another node

    tools.rent_pod("node-b", "retry")
    tools.list_pods()

    assert fake.removed == []
    assert [p.id for p in fake.pods] == ["p1", "theirs"]


@pytest.mark.parametrize("failure", [ValueError("No SSH keys found"), LiumError("API error 400: node is not available")])
def test_a_rent_the_sdk_says_was_never_made_does_not_block_the_next_one(tools, fake, monkeypatch, failure):
    original_up = fake.up
    monkeypatch.setattr(fake, "up", lambda **kwargs: (_ for _ in ()).throw(failure))
    assert tools.rent_pod("node-a", "first")["ok"] is False
    monkeypatch.setattr(fake, "up", original_up)

    assert tools.rent_pod("node-b", "retry")["ok"] is True


class _Answer:
    def __init__(self, status_code, data, headers=None):
        self.status_code, self.ok, self._data, self.text = status_code, status_code < 300, data, str(data)
        self.headers = headers or {}

    def json(self):
        return self._data


class _ServerStopped(BaseException):
    """The MCP host kills the server while a request is in flight: nothing after the send runs."""


def _real_sdk(monkeypatch, outcomes, pods=lambda posts: [], api_key="test", features=("rent_idempotency",)):
    """A real Lium client whose rent POSTs are answered from ``outcomes`` (a response or an exception each)."""
    from lium.sdk import Config, Lium
    from lium.sdk import client as client_module
    from lium.sdk import utils as sdk_utils

    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(sdk_utils.time, "sleep", lambda seconds: None)
    sdk = Lium(Config(api_key=api_key))
    sdk._features = set(features)
    monkeypatch.setattr(type(sdk.config), "ssh_public_keys", property(lambda self: ["ssh-ed25519 test"]))
    monkeypatch.setattr(sdk, "get_executor", lambda executor_id: _node("node-a", 2.0))
    monkeypatch.setattr(sdk, "_ensure_ssh_keys_registered", lambda *a, **k: None)
    posts, removed, accounts = [], [], []
    monkeypatch.setattr(sdk, "ps", lambda: pods(posts))
    monkeypatch.setattr(sdk, "rm", lambda pod: removed.append(pod.id))

    def request(method, url, headers=None, timeout=None, **kwargs):
        posts.append(headers["Idempotency-Key"])
        accounts.append(headers["X-API-KEY"])
        outcome = outcomes.pop(0) if outcomes else _no_answer()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(client_module.requests, "request", request)
    sdk.posts, sdk.removed, sdk.accounts = posts, removed, accounts
    return sdk


def _host_uses_the_pod(tools, sdk, monkeypatch, rental_state_dir, pod_id):
    """A returned result is not proof the host read it: the journal stays until the host names the pod."""
    assert list(rental_state_dir.glob("*.json"))
    monkeypatch.setattr(sdk, "ps", lambda: [_pod(pod_id, "job")])
    monkeypatch.setattr(sdk, "exec", lambda pod, command, timeout, max_output_bytes: {"exit_code": 0, "stdout": "", "stderr": ""})
    assert tools.run_command(pod_id, "nvidia-smi")["ok"] is True
    assert list(rental_state_dir.glob("*.json")) == []


def _real_tools(sdk, **kwargs):
    tools = LiumTools(client_factory=lambda: sdk, **kwargs)
    tools.BACKGROUND_CLEANUP, tools.CLEANUP_WAIT_SECONDS = False, 0.05
    return tools


def _no_answer():
    import requests

    return requests.ReadTimeout("read timed out")


def _stored(status_code, data):
    """The API's stored answer to the first request with this key."""
    return _Answer(status_code, data, headers={"Idempotent-Replayed": "true"})


LOST_RENTS = pytest.mark.parametrize("first, second", [
    (_no_answer(), _Answer(409, {"detail": "idempotency_in_progress"})),
    (_no_answer(), _Answer(400, {"detail": "A rental is already pending on this node"})),
    (_Answer(429, {"detail": "slow down"}), _no_answer()),
    (_Answer(429, {"detail": "slow down"}), _Answer(503, {"detail": "unavailable"})),
], ids=["timeout-409", "timeout-400", "429-timeout", "429-503"])


@LOST_RENTS
def test_an_unanswered_rent_is_replayed_with_its_key_and_reported_by_list_pods(monkeypatch, first, second):
    outcomes = [first, second]
    sdk = _real_sdk(monkeypatch, outcomes)
    tools = _real_tools(sdk)

    lost = tools.rent_pod("node-a", "job", template_id="tpl-1")
    retry = tools.rent_pod("node-a", "job-again", template_id="tpl-1")  # every replay meanwhile goes unanswered
    outcomes.append(_Answer(201, {"id": "pod-1"}))  # the API answers the key with the pod the lost request made
    listed = tools.list_pods()

    assert lost["error"]["code"] == "rent_outcome_unknown"
    assert retry["error"]["code"] == "cleanup_pending"
    assert len(set(sdk.posts)) == 1  # every request, replays included, under one key: one pod at most
    assert listed["recovered_rentals"] == [{"name": "job", "pod_ids": ["pod-1"], "idempotency_key": sdk.posts[0]}]
    assert "unresolved_rentals" not in listed and sdk.removed == []
    assert "recovered_rentals" not in tools.list_pods()  # handed over once


@LOST_RENTS
def test_a_retry_that_recovers_the_lost_pod_returns_it_and_rents_nothing_new(monkeypatch, first, second):
    outcomes = [first, second]
    sdk = _real_sdk(monkeypatch, outcomes)
    tools = _real_tools(sdk)
    tools.rent_pod("node-a", "job", template_id="tpl-1")

    outcomes.extend([_Answer(201, {"id": "pod-1"}), _Answer(201, {"id": "pod-2"})])
    retry = tools.rent_pod("node-b", "job", template_id="tpl-1", timeout_seconds=0)

    assert len(set(sdk.posts)) == 1 and len(outcomes) == 1  # pod-2 was never asked for
    assert retry["status"] == "recovered" and retry["recovered_rentals"][0]["pod_ids"] == ["pod-1"]


def test_a_rate_limited_rent_whose_repeat_is_refused_rented_nothing(monkeypatch):
    outcomes = [_Answer(429, {"detail": "slow down"}), _Answer(400, {"detail": "node is not available"}),
                _Answer(201, {"id": "pod-2"})]
    sdk = _real_sdk(monkeypatch, outcomes)
    tools = _real_tools(sdk)

    refused = tools.rent_pod("node-a", "job", template_id="tpl-1", timeout_seconds=0)
    monkeypatch.setattr(sdk, "wait_ready", lambda created, timeout, on_poll=None: None)
    retry = tools.rent_pod("node-a", "job-again", template_id="tpl-1", timeout_seconds=0)

    assert refused["ok"] is False and refused["error"]["code"] != "rent_outcome_unknown"
    assert retry["ok"] is True and retry["pod_id"] == "pod-2"


def test_a_same_name_pod_found_after_a_lost_rent_is_shown_never_taken_or_removed(monkeypatch):
    # another client rents a pod with this name on this node while our request is unanswered
    sdk = _real_sdk(monkeypatch, [_no_answer(), _no_answer()], pods=lambda posts: [_pod("theirs", "job")] if posts else [])
    tools = _real_tools(sdk)

    lost = tools.rent_pod("node-a", "job", template_id="tpl-1")
    listed = tools.list_pods()

    assert lost["error"]["code"] == "rent_outcome_unknown" and "pod_id" not in lost
    assert listed["unresolved_rentals"][0]["same_name_pods_to_check"] == ["theirs"]
    assert sdk.removed == []


def test_the_request_is_journaled_before_it_is_sent(monkeypatch, rental_state_dir):
    outcomes = [_Answer(201, {"id": "pod-1"})]
    sdk = _real_sdk(monkeypatch, outcomes)
    monkeypatch.setattr(sdk, "wait_ready", lambda created, timeout, on_poll=None: None)
    snapshots, send = [], sdk._request

    def checked_send(method, path, **kwargs):
        if path.endswith("/rent"):
            snapshots.append(bool(list(rental_state_dir.glob("*.json"))))
        return send(method, path, **kwargs)

    monkeypatch.setattr(sdk, "_request", checked_send)
    tools = _real_tools(sdk)
    rented = tools.rent_pod("node-a", "job", template_id="tpl-1", timeout_seconds=0)

    assert snapshots and all(snapshots)
    assert rented["pod_id"] == "pod-1"
    _host_uses_the_pod(tools, sdk, monkeypatch, rental_state_dir, "pod-1")


def test_two_saves_of_one_rental_at_once_leave_the_newer_record_whole(monkeypatch, rental_state_dir):
    sdk = _real_sdk(monkeypatch, [_no_answer(), _no_answer()])
    tools = _real_tools(sdk)
    tools.rent_pod("node-a", "job", template_id="tpl-1")
    rental, dump, overlapped = tools._unresolved[0], json.dump, []

    def worker_saves_meanwhile(state, f):
        if not overlapped:
            # a cancellation is writing its snapshot; the worker learns the pod id and saves the same rental
            rental.pod_ids.add("pod-1")
            overlapped.append(threading.Thread(target=tools._store.save, args=(rental,)))
            overlapped[0].start()
            overlapped[0].join(0.3)
        dump(state, f)

    monkeypatch.setattr(tools_module.json, "dump", worker_saves_meanwhile)
    tools._save(rental)
    overlapped[0].join(5)

    assert [p.suffix for p in rental_state_dir.iterdir() if p.suffix != ".lock"] == [".json"]
    (journal,) = rental_state_dir.glob("*.json")
    assert tools_module._Rental.from_state(json.loads(journal.read_text())).pod_ids == {"pod-1"}  # readable after a restart


def test_a_rent_whose_journal_cannot_be_written_is_never_sent(monkeypatch):
    sdk = _real_sdk(monkeypatch, [])
    tools = _real_tools(sdk)

    def disk_full(self, rental):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tools_module._RentalStore, "save", disk_full)
    failed = tools.rent_pod("node-a", "job", template_id="tpl-1")

    assert failed["ok"] is False and "No space left" in failed["error"]["message"]
    assert sdk.posts == [] and tools._unresolved == []


def test_a_server_stopped_mid_request_is_replayed_with_the_same_key_after_a_restart(monkeypatch, rental_state_dir):
    outcomes = [_ServerStopped()]
    sdk = _real_sdk(monkeypatch, outcomes)
    with pytest.raises(_ServerStopped):
        _real_tools(sdk).rent_pod("node-a", "job", template_id="tpl-1")

    outcomes.append(_Answer(201, {"id": "pod-1"}))
    after = _real_tools(sdk)  # the restarted server
    listed = after.list_pods()

    assert len(sdk.posts) == 2 and len(set(sdk.posts)) == 1
    assert listed["recovered_rentals"][0]["pod_ids"] == ["pod-1"]
    _host_uses_the_pod(after, sdk, monkeypatch, rental_state_dir, "pod-1")


@pytest.mark.parametrize("cancelled", [False, True])
def test_a_restarted_server_replays_the_lost_rent_with_the_same_key(monkeypatch, rental_state_dir, cancelled):
    outcomes = [_no_answer(), _no_answer()]
    sdk = _real_sdk(monkeypatch, outcomes)
    rental = tools_module._Rental()
    before = _real_tools(sdk)
    assert before.rent_pod("node-a", "job", template_id="tpl-1", rental=rental)["error"]["code"] == "rent_outcome_unknown"
    if cancelled:
        before.cancel_rental(rental)
    assert [p.name for p in rental_state_dir.glob("*.json")] == [f"{sdk.posts[0]}.json"]
    assert oct(rental_state_dir.stat().st_mode & 0o777) == "0o700"

    outcomes.append(_Answer(201, {"id": "pod-1"}))
    after = _real_tools(sdk)  # the user restarted the MCP server
    listed = after.list_pods()

    assert len(set(sdk.posts)) == 1
    if cancelled:
        assert sdk.removed == ["pod-1"] and "recovered_rentals" not in listed
        # nothing of the rental is left (the account's admission lock file stays: unlinking a lock file races)
        assert [p.name for p in rental_state_dir.iterdir() if not p.name.startswith("admit-")] == []
    else:
        assert sdk.removed == [] and listed["recovered_rentals"][0]["pod_ids"] == ["pod-1"]
        _host_uses_the_pod(after, sdk, monkeypatch, rental_state_dir, "pod-1")


def test_a_restored_cancelled_rental_is_removed_without_waiting_for_a_tool_call(monkeypatch, rental_state_dir):
    outcomes = [_no_answer(), _no_answer()]
    sdk = _real_sdk(monkeypatch, outcomes)
    rental = tools_module._Rental()
    before = _real_tools(sdk)
    before.rent_pod("node-a", "job", template_id="tpl-1", rental=rental)
    before.cancel_rental(rental)

    removed = threading.Event()
    monkeypatch.setattr(sdk, "rm", lambda pod: removed.set())
    monkeypatch.setattr(LiumTools, "BACKGROUND_CLEANUP", True)
    outcomes.append(_Answer(201, {"id": "pod-1"}))
    LiumTools(client_factory=lambda: sdk)  # the restarted server; the host calls no tool

    assert removed.wait(5)


@pytest.mark.parametrize("refusal", [
    _Answer(403, {"detail": "insufficient balance"}),
    _Answer(400, {"detail": "node is not available"}),
    # a backend release older than pending rows replays the stored pending answer for a rent that did rent
    _stored(409, {"error": {"code": "idempotency_outcome_unknown", "message": "outcome unknown"}}),
    _stored(409, {"error": {"code": "idempotency_in_progress", "message": "in progress"}}),
], ids=["fresh-403", "fresh-400", "replayed-outcome-unknown", "replayed-in-progress"])
def test_a_replay_answer_that_is_not_a_stored_refusal_settles_nothing(monkeypatch, rental_state_dir, refusal):
    outcomes = [_no_answer(), _no_answer(), refusal]
    sdk = _real_sdk(monkeypatch, outcomes)
    tools = _real_tools(sdk)

    tools.rent_pod("node-a", "job", template_id="tpl-1")
    listed = tools.list_pods()

    assert listed["unresolved_rentals"]
    assert list(rental_state_dir.glob("*.json"))


@pytest.mark.parametrize("stored", [
    _stored(402, {"detail": "insufficient balance"}),
    _stored(409, {"detail": "Provider has paused new rentals for this node."}),
], ids=["402", "409"])
def test_the_apis_stored_refusal_of_the_first_request_settles_it_as_never_rented(monkeypatch, rental_state_dir, stored):
    outcomes = [_no_answer(), _no_answer(), stored]
    sdk = _real_sdk(monkeypatch, outcomes)
    tools = _real_tools(sdk)

    tools.rent_pod("node-a", "job", template_id="tpl-1")
    listed = tools.list_pods()

    assert "unresolved_rentals" not in listed and "recovered_rentals" not in listed
    assert list(rental_state_dir.glob("*.json")) == []


@pytest.mark.parametrize("change", ["other_account", "key_expired", "no_idempotency_feature"])
def test_a_rental_is_not_replayed_where_its_key_would_be_a_new_rent(monkeypatch, rental_state_dir, change):
    sdk = _real_sdk(monkeypatch, [_no_answer(), _no_answer()])
    submitted = time.time()
    _real_tools(sdk).rent_pod("node-a", "job", template_id="tpl-1")

    other = _real_sdk(monkeypatch, [_Answer(201, {"id": "pod-2"})],
                      api_key="different-account" if change == "other_account" else "test",
                      features=() if change == "no_idempotency_feature" else ("rent_idempotency",))
    if change == "key_expired":
        monkeypatch.setattr(time, "time", lambda: submitted + 86_401)
    listed = _real_tools(other).list_pods()

    assert other.posts == []
    if change == "other_account":  # not even adopted: the journal is left, unlocked, for its own account's server
        assert "unresolved_rentals" not in listed
    else:
        assert listed["unresolved_rentals"][0]["last_error"]
    assert list(rental_state_dir.glob("*.json"))


@pytest.mark.parametrize("replayable", [True, False], ids=["replay-answers-unknown", "too-old-to-replay"])
def test_deleting_the_journal_never_lifts_the_rent_block(monkeypatch, rental_state_dir, replayable):
    """Only the API's answer settles an unanswered rent; while nothing can, no second rent is sent beside it."""
    unknown = _stored(409, {"error": {"code": "idempotency_outcome_unknown", "message": "outcome unknown"}})
    sdk = _real_sdk(monkeypatch, [_no_answer(), _no_answer()] + [unknown] * 8)
    submitted = time.time()
    tools = _real_tools(sdk)
    tools.rent_pod("node-a", "job", template_id="tpl-1")
    if not replayable:
        monkeypatch.setattr(time, "time", lambda: submitted + tools_module.REPLAY_WINDOW_SECONDS + 1)

    (unresolved,) = tools.list_pods()["unresolved_rentals"]
    if not replayable:
        hint = unresolved["last_error"]
        assert "deleting its journal does not lift" in hint and "Do not rent through any client" in hint
        # never points to another way to rent, nor to a restart, while the first rental is unknown
        assert "restart" not in hint and "https://" not in hint and "rent any" not in hint
    (journal,) = rental_state_dir.glob("*.json")
    journal.unlink()
    posts = len(sdk.posts)

    assert tools.rent_pod("node-b", "job-2", template_id="tpl-1")["ok"] is False
    assert journal.exists() and all(p == sdk.posts[0] for p in sdk.posts[posts:])  # written back; no new rent sent
    assert tools.list_pods()["unresolved_rentals"]


def _windows_locking(monkeypatch):
    """Run the store as on Windows: no fcntl, msvcrt.locking instead (emulated here with an flock of the same
    non-blocking semantics, since CI runs on Linux)."""
    import fcntl
    import types

    def locking(fd, mode, nbytes):
        assert mode == msvcrt.LK_NBLCK and nbytes == 1
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises OSError when held, as msvcrt.locking does

    msvcrt = types.SimpleNamespace(LK_NBLCK=2, locking=locking)
    monkeypatch.setitem(sys.modules, "fcntl", None)  # `import fcntl` raises ImportError
    monkeypatch.setitem(sys.modules, "msvcrt", msvcrt)


@pytest.mark.parametrize("backend", ["posix", "windows"])
def test_a_running_server_never_adopts_another_running_servers_rental(monkeypatch, rental_state_dir, backend):
    """One `lium mcp` per Claude Code session, Desktop and Cursor share the journal directory: a second server
    neither replays nor adopts the first one's unanswered rental, but rents nothing beside it until it settles."""
    monkeypatch.setattr(sys.modules[__name__], "_SERVERS_STAY_LIVE", [True])
    if backend == "windows":
        _windows_locking(monkeypatch)
    sdk = _real_sdk(monkeypatch, [_no_answer(), _no_answer()])
    first = _real_tools(sdk)
    first.rent_pod("node-a", "job", template_id="tpl-1")
    journal = rental_state_dir / f"{sdk.posts[0]}.json"

    other = _real_sdk(monkeypatch, [_Answer(201, {"id": "pod-2"})])
    second = _real_tools(other)
    listed = second.list_pods()

    assert second._unresolved == [] and "unresolved_rentals" not in listed and "recovered_rentals" not in listed
    assert other.posts == []
    second.CLEANUP_WAIT_SECONDS = 0.05
    refused = second.rent_pod("node-b", "other-job", template_id="tpl-1", timeout_seconds=0)
    assert refused["error"]["code"] == "cleanup_pending" and "another running" in refused["unresolved"][0]["last_error"]
    assert other.posts == []  # the first may still rent a pod: no second rent is sent
    assert journal.exists() and first._unresolved and second._unresolved == []  # still the first server's to settle

    # once the first server settles it (here: its journal goes), the second rents again
    first._store.forget(first._unresolved[0])
    assert second.rent_pod("node-b", "other-job", template_id="tpl-1", timeout_seconds=0)["pod_id"] == "pod-2"


@pytest.mark.parametrize("case", ["simultaneous_first_calls", "pod_not_yet_delivered", "cancelled_pod_not_removed",
                                  "pod_handed_to_the_transport"])
def test_another_live_sessions_rental_blocks_a_rent_here(monkeypatch, rental_state_dir, case):
    """Two live sessions of one account: whatever the first session's rental is doing, the second sends no rent
    until the first session's host has named the pod (a result handed to the transport may still be lost)."""
    from lium.cli.mcp.tools import _Rental

    monkeypatch.setattr(sys.modules[__name__], "_SERVERS_STAY_LIVE", [True])
    if case == "simultaneous_first_calls":
        sdk = _real_sdk(monkeypatch, [_no_answer(), _no_answer()])
    else:
        sdk = _real_sdk(monkeypatch, [_Answer(201, {"id": "pod-1"})])
    first, rental = _real_tools(sdk), _Rental()
    first.rent_pod("node-a", "job", template_id="tpl-1", rental=rental, timeout_seconds=0)
    (journal,) = rental_state_dir.glob("*.json")
    if case == "cancelled_pod_not_removed":
        monkeypatch.setattr(sdk, "rm", lambda pod: (_ for _ in ()).throw(LiumError("Cannot delete a PENDING pod")))
        first.cancel_rental(rental)
    if case == "pod_handed_to_the_transport":
        first.deliver(rental)  # handed to the transport only: the response may still be lost before the host has it
    holder = None
    if case == "simultaneous_first_calls":
        # the first session is mid-admission: it holds the account's lock and has not written its journal yet
        written = journal.read_bytes()
        journal.unlink()
        holder = first._store.lock_admission(first._scope(), 0)
        assert holder is not None and holder >= 0

        def the_first_session_journals_then_sends():
            time.sleep(0.2)
            journal.write_bytes(written)
            os.close(holder)

        threading.Thread(target=the_first_session_journals_then_sends).start()

    other = _real_sdk(monkeypatch, [_Answer(201, {"id": "pod-2"})])
    second = _real_tools(other)
    second.CLEANUP_WAIT_SECONDS = 0.5
    refused = second.rent_pod("node-b", "other-job", template_id="tpl-1", timeout_seconds=0)

    assert refused["ok"] is False and other.posts == []
    assert journal.exists() and second._unresolved == []  # still the first session's to settle
    if case == "pod_handed_to_the_transport":
        assert "known_pod_ids" in refused["error"]["hint"] and "do not remove_pod" in refused["error"]["hint"]
        # the second host knows pod-1 and asks for another pod beside it: rented, the first session's journal kept
        rented = second.rent_pod("node-b", "other-job", template_id="tpl-1", timeout_seconds=0,
                                 known_pod_ids=["pod-1"])
        assert rented["pod_id"] == "pod-2" and journal.exists()


def test_a_rent_another_server_sends_after_this_ones_reconcile_still_blocks_it(monkeypatch, rental_state_dir):
    """Admission reads the journals at that moment, not as of the reconcile just before it."""
    monkeypatch.setattr(sys.modules[__name__], "_SERVERS_STAY_LIVE", [True])
    sdk = _real_sdk(monkeypatch, [_no_answer(), _no_answer()])
    _real_tools(sdk).rent_pod("node-a", "job", template_id="tpl-1")
    journal = rental_state_dir / f"{sdk.posts[0]}.json"
    written = journal.read_bytes()
    journal.unlink()  # not there yet when the second server reconciles

    other = _real_sdk(monkeypatch, [_Answer(201, {"id": "pod-2"})])
    second = _real_tools(other)
    second.CLEANUP_WAIT_SECONDS = 0.05
    reconcile = second._reconcile

    def reconcile_then_the_other_server_sends(*args, **kwargs):
        unresolved = reconcile(*args, **kwargs)
        journal.write_bytes(written)  # the other server journals its rent right after
        return unresolved

    monkeypatch.setattr(second, "_reconcile", reconcile_then_the_other_server_sends)
    refused = second.rent_pod("node-b", "other-job", template_id="tpl-1", timeout_seconds=0)

    assert refused["error"]["code"] == "cleanup_pending" and other.posts == []


def test_a_server_started_while_its_predecessor_finished_a_rent_adopts_the_journal_once_it_exits(
    monkeypatch, rental_state_dir
):
    """The relaunched host starts the new server while the old one still holds its last rent's journal; the new
    one must not have looked once and given up, or it rents a second pod while the first may be billing."""
    monkeypatch.setattr(sys.modules[__name__], "_SERVERS_STAY_LIVE", [True])
    sdk = _real_sdk(monkeypatch, [_no_answer(), _no_answer()])
    first = _real_tools(sdk)
    first.rent_pod("node-a", "job", template_id="tpl-1")

    second = _real_tools(sdk)
    second._reconcile()  # its startup pass: the journal is still held
    assert second._unresolved == []

    for fd in first._store._held.values():  # the old server exits; the OS drops its locks
        os.close(fd)
    first._store._held.clear()
    refused = second.rent_pod("node-b", "job", template_id="tpl-1")

    assert len(second._unresolved) == 1 and refused["ok"] is False, refused
    assert len(set(sdk.posts)) == 1  # no second pod


def test_a_server_for_another_account_leaves_an_orphaned_journal_to_its_own_account(monkeypatch, rental_state_dir):
    """Account A's server exits mid-rent; account B's server starts and stays up. When A's server comes back it
    adopts its journal and refuses a fresh rent until that one is settled."""
    monkeypatch.setattr(sys.modules[__name__], "_SERVERS_STAY_LIVE", [True])
    sdk = _real_sdk(monkeypatch, [_no_answer(), _no_answer()])
    first = _real_tools(sdk)
    first.rent_pod("node-a", "job", template_id="tpl-1")
    for fd in first._store._held.values():  # A's server exits; the OS drops its locks
        os.close(fd)
    first._store._held.clear()

    other = _real_sdk(monkeypatch, [], api_key="different-account")
    foreign = _real_tools(other)
    assert "unresolved_rentals" not in foreign.list_pods() and foreign._store._held == {}

    restarted = _real_tools(sdk)  # A again, while B still runs
    refused = restarted.rent_pod("node-b", "job", template_id="tpl-1")

    assert len(restarted._unresolved) == 1 and refused["ok"] is False, refused
    assert len(set(sdk.posts)) == 1  # every POST, the replay included, carries the first key: no second pod


def test_the_background_replays_a_wanted_rental_only_soon_after_it_was_sent(monkeypatch, rental_state_dir):
    """Hours later a replay that rents makes a pod nobody hears of; it waits for a call whose result reports it."""
    outcomes = [_no_answer(), _no_answer()]
    sdk = _real_sdk(monkeypatch, outcomes)
    submitted = time.time()
    _real_tools(sdk).rent_pod("node-a", "job", template_id="tpl-1")
    outcomes.append(_Answer(201, {"id": "pod-1"}))
    monkeypatch.setattr(time, "time", lambda: submitted + tools_module.BACKGROUND_REPLAY_SECONDS + 1)

    after = _real_tools(sdk)  # restarted the next morning
    after._reconcile()  # what the background thread runs

    assert len(sdk.posts) == 2 and after._recovered == []
    assert "not replayed in the background" in after._unresolved[0].last_error
    listed = after.list_pods()
    assert len(sdk.posts) == 3 and listed["recovered_rentals"][0]["pod_ids"] == ["pod-1"]


def test_a_cancel_that_lands_while_a_replay_keeps_the_pod_still_removes_it(monkeypatch, rental_state_dir):
    outcomes = [_no_answer(), _no_answer()]
    sdk = _real_sdk(monkeypatch, outcomes)
    tools = _real_tools(sdk)
    rental = tools_module._Rental()
    tools.rent_pod("node-a", "job", template_id="tpl-1", rental=rental)

    outcomes.append(_Answer(201, {"id": "pod-1"}))
    settle = tools._settle_cleanup

    def cancel_after_resolution(r, live, reports=False):
        disposition = settle(r, live, reports)
        tools.cancel_rental(r)
        return disposition

    monkeypatch.setattr(tools, "_settle_cleanup", cancel_after_resolution)
    first = tools.list_pods()
    monkeypatch.setattr(tools, "_settle_cleanup", settle)
    tools.list_pods()

    assert "recovered_rentals" not in first
    assert sdk.removed == ["pod-1"] and tools._unresolved == [] and tools._recovered == []
    assert list(rental_state_dir.glob("*.json")) == []


def test_a_server_stopped_while_waiting_for_the_pod_hands_it_over_after_a_restart(monkeypatch, rental_state_dir):
    outcomes = [_Answer(201, {"id": "pod-1"})]
    sdk = _real_sdk(monkeypatch, outcomes)

    def stop_during_wait(*args, **kwargs):
        raise _ServerStopped()

    monkeypatch.setattr(sdk, "wait_ready", stop_during_wait)
    with pytest.raises(_ServerStopped):
        _real_tools(sdk).rent_pod("node-a", "job", template_id="tpl-1")
    assert list(rental_state_dir.glob("*.json"))

    outcomes.append(_Answer(201, {"id": "pod-2"}))
    after = _real_tools(sdk)  # the restarted server
    result = after.rent_pod("node-a", "retry", template_id="tpl-1")

    assert result["status"] == "recovered" and result["recovered_rentals"][0]["pod_ids"] == ["pod-1"]
    assert len(sdk.posts) == 1 and len(outcomes) == 1  # the journal named the pod: no replay, no second rent
    _host_uses_the_pod(after, sdk, monkeypatch, rental_state_dir, "pod-1")


@pytest.mark.parametrize("receiving_call", ["rent_pod", "list_pods"])
def test_recovered_pods_a_cancelled_call_was_handing_over_wait_for_the_next_call(monkeypatch, rental_state_dir,
                                                                                receiving_call):
    outcomes = [_no_answer(), _no_answer()]
    sdk = _real_sdk(monkeypatch, outcomes)
    tools = _real_tools(sdk)
    tools.rent_pod("node-a", "job", template_id="tpl-1")
    outcomes.append(_Answer(201, {"id": "pod-1"}))

    receiver = tools_module._Rental()
    if receiving_call == "rent_pod":
        handed = tools.rent_pod("node-b", "retry", template_id="tpl-1", rental=receiver)
    else:
        handed = tools.list_pods(receiver)
    assert handed["recovered_rentals"][0]["pod_ids"] == ["pod-1"]
    # a retry that overlaps the call still holding pod-1 gets pod-1, not a new rent
    outcomes.append(_Answer(201, {"id": "pod-2"}))
    overlapping = tools.rent_pod("node-b", "retry", template_id="tpl-1")
    assert overlapping["status"] == "recovered" and overlapping["recovered_rentals"][0]["pod_ids"] == ["pod-1"]
    assert len(set(sdk.posts)) == 1 and len(outcomes) == 1
    tools.cancel_rental(receiver)  # the host cancelled the call: it never saw that result
    assert list(rental_state_dir.glob("*.json"))

    listed = tools.list_pods()

    assert listed["recovered_rentals"][0]["pod_ids"] == ["pod-1"]
    assert sdk.removed == [] and len(set(sdk.posts)) == 1
    _host_uses_the_pod(tools, sdk, monkeypatch, rental_state_dir, "pod-1")


@pytest.mark.parametrize("receiving_call", ["rent_pod", "list_pods"])
def test_a_call_cancelled_before_it_reserves_recovered_pods_takes_none(monkeypatch, rental_state_dir, receiving_call):
    outcomes = [_no_answer(), _no_answer()]
    sdk = _real_sdk(monkeypatch, outcomes)
    tools = _real_tools(sdk)
    tools.rent_pod("node-a", "job", template_id="tpl-1")
    outcomes.append(_Answer(201, {"id": "pod-1"}))

    receiver = tools_module._Rental()
    reserve = tools._hand_off_recovered

    def cancel_before_reservation(target):
        if target is receiver:
            tools.cancel_rental(receiver)  # the host cancels while the worker is still in a blocking SDK call
        return reserve(target)

    monkeypatch.setattr(tools, "_hand_off_recovered", cancel_before_reservation)
    if receiving_call == "rent_pod":
        tools.rent_pod("node-b", "retry", template_id="tpl-1", rental=receiver)
    else:
        tools.list_pods(receiver)
    monkeypatch.setattr(tools, "_hand_off_recovered", reserve)

    listed = tools.list_pods()
    assert listed["recovered_rentals"][0]["pod_ids"] == ["pod-1"]
    assert sdk.removed == [] and receiver.handed == []
    assert len(set(sdk.posts)) == 1


@LOST_RENTS
def test_a_pod_recovered_before_the_unknown_result_is_delivered_stays_journaled(monkeypatch, rental_state_dir,
                                                                              first, second):
    outcomes = [first, second, _Answer(201, {"id": "pod-1"})]
    sdk = _real_sdk(monkeypatch, outcomes)
    tools = _real_tools(sdk)
    monkeypatch.setattr(tools, "_start_reconciler", lambda: tools._reconcile())  # recovery wins the race

    lost = tools.rent_pod("node-a", "job", template_id="tpl-1")
    assert lost["error"]["code"] == "rent_outcome_unknown"
    assert list(rental_state_dir.glob("*.json"))

    recovered = _real_tools(sdk).rent_pod("node-a", "retry", template_id="tpl-1")  # the restarted server
    assert recovered["status"] == "recovered"
    assert recovered["recovered_rentals"][0]["pod_ids"] == ["pod-1"]
    assert len(set(sdk.posts)) == 1


def test_a_server_stopped_after_returning_the_pod_but_before_writing_it_hands_it_over_again(monkeypatch,
                                                                                         rental_state_dir):
    outcomes = [_Answer(201, {"id": "pod-1"})]
    sdk = _real_sdk(monkeypatch, outcomes, pods=lambda posts: [_pod("pod-1", "job")] if posts else [])
    monkeypatch.setattr(sdk, "wait_ready", lambda *a, **k: None)
    tools = _real_tools(sdk)
    assert tools.rent_pod("node-a", "job", template_id="tpl-1", timeout_seconds=0)["pod_id"] == "pod-1"
    # the callback returned; the server dies before the response is written to stdio

    outcomes.append(_Answer(201, {"id": "pod-2"}))
    after = _real_tools(sdk)
    retry = after.rent_pod("node-a", "job", template_id="tpl-1", timeout_seconds=0)

    assert retry["status"] == "recovered"
    assert retry["recovered_rentals"][0] == {"name": "job", "pod_ids": ["pod-1"], "idempotency_key": sdk.posts[0],
                                             "may_have_been_reported": True}
    assert len(sdk.posts) == 1 and len(outcomes) == 1


def test_a_retry_on_a_live_server_gets_the_unacknowledged_pod_back_until_the_host_names_it(monkeypatch,
                                                                                        rental_state_dir):
    outcomes = [_Answer(201, {"id": "pod-1"}), _Answer(201, {"id": "pod-2"})]
    sdk = _real_sdk(monkeypatch, outcomes)
    monkeypatch.setattr(sdk, "wait_ready", lambda *a, **k: None)
    tools = _real_tools(sdk)
    assert tools.rent_pod("node-a", "job", template_id="tpl-1", timeout_seconds=0)["pod_id"] == "pod-1"
    # the host timed out or dropped that response; the server lives on and the host retries

    for _ in range(2):
        retry = tools.rent_pod("node-a", "job", template_id="tpl-1", timeout_seconds=0)
        assert retry["status"] == "recovered"
        assert retry["recovered_rentals"] == [{"name": "job", "pod_ids": ["pod-1"], "idempotency_key": sdk.posts[0],
                                               "may_have_been_reported": True}]
    assert len(sdk.posts) == 1 and len(outcomes) == 1  # one POST, one key, one pod
    assert list(rental_state_dir.glob("*.json"))

    # the host names the pod it holds: that is the acknowledgment, and a second pod is what it asked for
    second = tools.rent_pod("node-a", "job-2", template_id="tpl-1", timeout_seconds=0, known_pod_ids=["pod-1"])
    assert second["pod_id"] == "pod-2" and len(set(sdk.posts)) == 2
    assert [p.name for p in rental_state_dir.glob("*.json")] == [f"{sdk.posts[1]}.json"]


def test_a_listing_older_than_the_delivery_does_not_erase_the_journal(monkeypatch, rental_state_dir):
    outcomes = [_Answer(201, {"id": "pod-1"})]
    sdk = _real_sdk(monkeypatch, outcomes)
    monkeypatch.setattr(sdk, "wait_ready", lambda *a, **k: None)
    tools = _real_tools(sdk)
    listing = {"rent_meanwhile": True, "pods": []}

    def ps():
        if listing["rent_meanwhile"]:
            # this listing was taken while the account had no pods; the rent is delivered before it returns
            listing["rent_meanwhile"] = False
            assert tools.rent_pod("node-a", "job", template_id="tpl-1", timeout_seconds=0)["pod_id"] == "pod-1"
            return []
        return listing["pods"]

    monkeypatch.setattr(sdk, "ps", ps)
    tools.list_pods()
    assert list(rental_state_dir.glob("*.json"))  # the stale snapshot says nothing about pod-1

    tools.list_pods()  # a newer listing the pod is not in yet: still nothing
    assert list(rental_state_dir.glob("*.json"))

    listing["pods"] = [_pod("pod-1", "job")]
    tools.list_pods()
    listing["pods"] = []
    tools.list_pods()  # listed after the delivery, then gone: nothing left to tell the host about
    assert list(rental_state_dir.glob("*.json")) == []


def test_an_empty_listing_taken_before_one_that_showed_the_pod_does_not_erase_the_journal(monkeypatch,
                                                                                         rental_state_dir):
    outcomes = [_Answer(201, {"id": "pod-1"}), _Answer(201, {"id": "pod-2"})]
    sdk = _real_sdk(monkeypatch, outcomes)
    monkeypatch.setattr(sdk, "wait_ready", lambda *a, **k: None)
    tools = _real_tools(sdk)
    assert tools.rent_pod("node-a", "job", template_id="tpl-1", timeout_seconds=0)["pod_id"] == "pod-1"
    listing, shown = {"overlap": True}, []

    def ps():
        if listing["overlap"]:
            # listing A is taken before the new pod shows; listing B starts later, shows it and returns first
            listing["overlap"] = False
            shown.append(tools.list_pods())
            return []
        return [_pod("pod-1", "job")]

    monkeypatch.setattr(sdk, "ps", ps)
    assert tools.list_pods()["pods"] == []  # A returns last, with the older empty snapshot
    assert [p["pod_id"] for p in shown[0]["pods"]] == ["pod-1"]

    assert list(rental_state_dir.glob("*.json"))
    retry = tools.rent_pod("node-a", "job", template_id="tpl-1", timeout_seconds=0)
    assert retry["status"] == "recovered" and retry["recovered_rentals"][0]["pod_ids"] == ["pod-1"]
    assert len(sdk.posts) == 1 and len(outcomes) == 1


@pytest.mark.parametrize("first_call_is", ["about_to_send", "waiting_for_the_pod_to_start"])
def test_a_retry_while_the_first_rent_pod_is_still_running_rents_nothing(monkeypatch, rental_state_dir,
                                                                         first_call_is):
    outcomes = [_Answer(201, {"id": "pod-1"}), _Answer(201, {"id": "pod-2"})]
    sdk = _real_sdk(monkeypatch, outcomes)
    tools = _real_tools(sdk)
    reached, release, results = threading.Event(), threading.Event(), []

    def block(*args, **kwargs):
        reached.set()
        release.wait(5)
        return [] if first_call_is == "about_to_send" else None

    # the first call has passed admission: it holds either nothing yet (its snapshot listing) or a billing pod
    monkeypatch.setattr(sdk, "ps" if first_call_is == "about_to_send" else "wait_ready", block)
    first = threading.Thread(
        target=lambda: results.append(tools.rent_pod("node-a", "job", template_id="tpl-1", timeout_seconds=0)))
    first.start()
    assert reached.wait(5)

    retry = tools.rent_pod("node-a", "job", template_id="tpl-1", timeout_seconds=0)  # the host timed out and retried

    assert retry["error"]["code"] == "rent_in_progress"
    assert len(sdk.posts) == (0 if first_call_is == "about_to_send" else 1)
    if first_call_is == "waiting_for_the_pod_to_start":
        assert retry["in_progress"][0]["pod_ids"] == ["pod-1"]
    release.set()
    first.join(5)
    assert results[0]["pod_id"] == "pod-1"
    again = tools.rent_pod("node-a", "job", template_id="tpl-1", timeout_seconds=0)
    assert again["status"] == "recovered" and again["recovered_rentals"][0]["pod_ids"] == ["pod-1"]
    assert len(sdk.posts) == 1 and len(outcomes) == 1  # one POST, one key, one pod


def test_a_cancelled_pod_with_a_known_id_is_removed_after_a_restart_without_a_replay(monkeypatch, rental_state_dir):
    sdk = _real_sdk(monkeypatch, [_Answer(201, {"id": "pod-1"})])
    monkeypatch.setattr(sdk, "wait_ready", lambda *a, **k: None)
    before, rental = _real_tools(sdk), tools_module._Rental()
    monkeypatch.setattr(sdk, "rm", lambda pod: (_ for _ in ()).throw(LiumError("API error 400: pod is PENDING")))
    before.rent_pod("node-a", "job", template_id="tpl-1", rental=rental)
    before.cancel_rental(rental)  # the removal fails before the server stops

    removed = []
    monkeypatch.setattr(sdk, "rm", lambda pod: removed.append(pod.id))
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 86_401)  # the next day: past any replay
    _real_tools(sdk).list_pods()

    assert removed == ["pod-1"]
    assert len(sdk.posts) == 1  # deletion requires no replay
    assert list(rental_state_dir.glob("*.json")) == []


def test_a_failed_capability_lookup_does_not_switch_replays_off(monkeypatch, rental_state_dir):
    outcomes = [_no_answer(), _no_answer()]
    sdk = _real_sdk(monkeypatch, outcomes)
    tools = _real_tools(sdk)
    rental = tools_module._Rental()
    tools.rent_pod("node-a", "job", template_id="tpl-1", rental=rental)
    tools.cancel_rental(rental)
    outcomes.append(_Answer(201, {"id": "pod-1"}))

    sdk._features = None
    answers = iter([_no_answer(), _Answer(200, {"features": ["rent_idempotency"]})])

    def version_request(*args, **kwargs):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(sdk, "_request", version_request)
    tools.list_pods()
    tools.list_pods()

    assert sdk.removed == ["pod-1"]


def test_a_pod_recovered_for_one_account_is_never_handed_to_another(monkeypatch, rental_state_dir):
    outcomes = [_no_answer(), _no_answer()]
    sdk = _real_sdk(monkeypatch, outcomes)
    tools = _real_tools(sdk)
    tools.rent_pod("node-a", "job", template_id="tpl-1")
    outcomes.append(_Answer(201, {"id": "pod-1"}))
    receiver = tools_module._Rental()
    tools.list_pods(receiver)
    tools.cancel_rental(receiver)  # recovered and journaled, and the server stops before the host hears of it

    other = _real_sdk(monkeypatch, [], api_key="different-account")
    listed = _real_tools(other).list_pods()

    assert "recovered_rentals" not in listed and "unresolved_rentals" not in listed
    assert other.posts == [] and other.removed == []
    assert list(rental_state_dir.glob("*.json"))


def test_a_rental_tracked_while_the_background_reconciler_exits_is_still_reconciled(tools, fake, monkeypatch):
    tools.BACKGROUND_CLEANUP, tools.CLEANUP_RETRY_SECONDS = True, 0.01
    late = tools_module._Rental()
    late.submitted, late.cancelled, late.outcome, late.pod_ids = True, True, "created", {"p1"}
    original, calls = tools._reconcile, []

    def reconcile():
        result = original()
        calls.append(result)
        if len(calls) == 1:
            # arrives after this pass read the list and before the loop decides to exit: _track sees the thread
            # still running, so it starts none
            tools._track(late)
        return result

    monkeypatch.setattr(tools, "_reconcile", reconcile)
    first = tools_module._Rental()
    first.submitted, first.cancelled, first.outcome = True, True, "rejected"
    tools._track(first)

    deadline = time.monotonic() + 5
    while "p1" not in fake.removed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert fake.removed == ["p1"]


def test_a_failed_lock_release_neither_fails_the_cleanup_nor_stops_the_worker(tools, fake, monkeypatch, rental_state_dir):
    """On Windows another server that just opened a released lock file makes its unlink fail. The journal is
    still forgotten, and the background worker goes on to remove a pod cancelled after that pass."""
    unlink = tools_module.Path.unlink

    def lock_in_use(path, missing_ok=False):
        if path.suffix == ".lock":
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(tools_module.Path, "unlink", lock_in_use)
    rental = tools_module._Rental()
    rental.idempotency_key, rental.replay = "key-1", type("E", (), {"endpoint": "/x", "executor_id": None, "payload": {}})()
    tools._store.save(rental)
    tools._store.forget(rental)
    assert not (rental_state_dir / "key-1.json").exists() and tools._store._held == {}

    tools.BACKGROUND_CLEANUP, tools.CLEANUP_RETRY_SECONDS = True, 0.01
    original, calls = tools._reconcile, []

    def reconcile():
        calls.append(1)
        if len(calls) == 1:
            late = tools_module._Rental()
            late.submitted, late.cancelled, late.outcome, late.pod_ids = True, True, "created", {"p1"}
            tools._track(late)  # the worker is running, so this starts none
            raise PermissionError(13, "lock file in use")
        return original()

    monkeypatch.setattr(tools, "_reconcile", reconcile)
    first = tools_module._Rental()
    first.submitted, first.cancelled, first.outcome = True, True, "rejected"
    tools._track(first)

    deadline = time.monotonic() + 5
    while "p1" not in fake.removed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert fake.removed == ["p1"]


def test_a_rental_cancelled_before_its_worker_starts_never_rents_and_never_waits(tools, fake):
    from lium.cli.mcp.tools import _Rental

    rental = _Rental()
    tools.cancel_rental(rental)
    done = threading.Event()

    def run():
        try:
            tools.rent_pod("cheap", "cancelled", rental=rental)
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()

    assert done.wait(0.5)
    assert fake.up_calls == 0
    assert tools.rent_pod("cheap", "next")["ok"] is True


def test_a_rental_cancelled_while_waiting_for_cleanup_leaves_without_renting(tools, fake, monkeypatch):
    from lium.cli.mcp.tools import _Rental

    first = _Rental()
    tools.rent_pod("node-a", "first", rental=first)
    monkeypatch.setattr(fake, "rm", lambda pod: (_ for _ in ()).throw(LiumError("Cannot delete a PENDING pod")))
    tools.cancel_rental(first)
    tools.CLEANUP_WAIT_SECONDS = 30

    waiter, done = _Rental(), threading.Event()
    threading.Thread(target=lambda: (tools.rent_pod("node-b", "waiter", rental=waiter), done.set()), daemon=True).start()
    time.sleep(0.1)
    tools.cancel_rental(waiter)

    assert done.wait(1)
    assert fake.up_calls == 1


def test_help_works_without_the_mcp_package_being_imported():
    result = CliRunner().invoke(cli, ["mcp", "--help"])

    assert result.exit_code == 0
    assert "list_gpus" in result.output and '"command": "lium"' in result.output


def test_the_cli_does_not_import_mcp_until_lium_mcp_runs():
    code = "import sys, lium.cli.cli; print('mcp' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()

    assert out == "False"
