"""`lium mcp`: the MCP server an agent host runs to rent and use Lium GPUs (lium/cli/mcp)."""

import asyncio
import json
import subprocess
import sys
import threading

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.mcp.tools import OUTPUT_TAIL_CHARS, LiumTools
from lium.sdk import ExecutorInfo, PodInfo
from lium.sdk.exceptions import LiumInsufficientBalanceError, PodStartError

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

    def ps(self):
        return self.pods

    def up(self, **kwargs):
        self.up_kwargs = kwargs
        self.pods = [*self.pods, _pod("p2", kwargs["name"])]
        return {"id": "p2", "name": kwargs["name"], "status": "PENDING"}

    ready_result = "ready"

    def wait_ready(self, created, timeout, on_poll=None):
        self.wait_timeout = timeout
        if on_poll:
            on_poll(None, "PENDING", 0.0)
        if isinstance(self.ready_result, Exception):
            raise self.ready_result
        return _pod(created["id"], created["name"]) if self.ready_result == "ready" else None

    exec_error = None

    def exec(self, pod, command, timeout):
        self.exec_calls.append((pod.id, command, timeout))
        if self.exec_error:
            raise self.exec_error
        return {"exit_code": 0, "stdout": "x" * (OUTPUT_TAIL_CHARS + 10), "stderr": ""}

    def rm(self, pod):
        self.removed.append(pod.id)

    def balance(self):
        raise LiumInsufficientBalanceError("Balance too low", code="insufficient_balance", hint="lium fund")


@pytest.fixture
def fake():
    return FakeLium()


@pytest.fixture
def tools(fake):
    return LiumTools(client_factory=lambda: fake)


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


def test_a_pod_that_fails_to_start_is_an_error_that_names_it(tools, fake):
    fake.ready_result = PodStartError("pod p2 FAILED", pod_id="p2", status="FAILED")

    result = tools.rent_pod("cheap", "agent-job")

    assert result["ok"] is False and result["pod_id"] == "p2"
    assert result["error"]["code"] == "pod_start_failed" and "remove_pod" in result["error"]["hint"]


def test_run_command_finds_the_pod_by_name_and_keeps_only_the_output_tail(tools, fake):
    result = tools.run_command("job", "nvidia-smi")

    assert fake.exec_calls == [("p1", "nvidia-smi", 300)]
    assert result["exit_code"] == 0
    assert result["stdout"].startswith("…[cut to the last")
    assert len(result["stdout"]) < OUTPUT_TAIL_CHARS + 100


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
            await task

    asyncio.run(check())


def test_a_rental_cancelled_mid_call_removes_its_pod_before_a_retry_rents(tools, fake, monkeypatch):
    from lium.cli.mcp.tools import tool_functions

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

    monkeypatch.setattr(fake, "up", slow_up)
    monkeypatch.setattr(fake, "rm", rm)

    async def check():
        task = asyncio.create_task(rent_pod("cheap", "job"))
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert (await rent_pod("cheap", "job"))["ok"] is True

    asyncio.run(check())
    assert events == ["up", "rm p2", "up"]


def test_a_rental_cancelled_after_it_finished_still_removes_the_unseen_pod(tools, fake):
    from lium.cli.mcp.tools import _Rental

    rental = _Rental()
    result = tools.rent_pod("cheap", "job", rental=rental)  # done, but the host cancelled before reading it

    tools.cancel_rental(rental, result)
    tools.rent_pod("cheap", "again")  # waits for the removal

    assert fake.removed == ["p2"]


def test_help_works_without_the_mcp_package_being_imported():
    result = CliRunner().invoke(cli, ["mcp", "--help"])

    assert result.exit_code == 0
    assert "list_gpus" in result.output and '"command": "lium"' in result.output


def test_the_cli_does_not_import_mcp_until_lium_mcp_runs():
    code = "import sys, lium.cli.cli; print('mcp' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()

    assert out == "False"
