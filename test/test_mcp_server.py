"""`lium mcp`: the MCP server an agent host runs to rent and use Lium GPUs (lium/cli/mcp)."""

import asyncio
import json
import subprocess
import sys

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.mcp.tools import OUTPUT_TAIL_CHARS, LiumTools
from lium.sdk import ExecutorInfo, PodInfo
from lium.sdk.exceptions import LiumInsufficientBalanceError

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
        return _pod("p2", kwargs["name"])

    def exec(self, pod, command, timeout):
        self.exec_calls.append((pod.id, command, timeout))
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
    assert fake.up_kwargs["executor_id"] == "cheap" and fake.up_kwargs["wait"] is True


def test_run_command_finds_the_pod_by_name_and_keeps_only_the_output_tail(tools, fake):
    result = tools.run_command("job", "nvidia-smi")

    assert fake.exec_calls == [("p1", "nvidia-smi", 300)]
    assert result["exit_code"] == 0
    assert result["stdout"].startswith("…[cut to the last")
    assert len(result["stdout"]) < OUTPUT_TAIL_CHARS + 100


def test_remove_pod_of_an_unknown_pod_is_an_error_result_not_an_exception(tools, fake):
    result = tools.remove_pod("nope")

    assert result["ok"] is False
    assert result["error"]["code"] == "LookupError"
    assert fake.removed == []


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


def test_help_works_without_the_mcp_package_being_imported():
    result = CliRunner().invoke(cli, ["mcp", "--help"])

    assert result.exit_code == 0
    assert "list_gpus" in result.output and '"command": "lium"' in result.output


def test_the_cli_does_not_import_mcp_until_lium_mcp_runs():
    code = "import sys, lium.cli.cli; print('mcp' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()

    assert out == "False"
