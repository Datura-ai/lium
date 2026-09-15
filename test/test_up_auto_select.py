"""DAH-2980: `lium up --gpu X` must rent the cheapest optimal node, and say which.

`ResolveExecutorAction` took `pareto_executors[0]` — the first Pareto-optimal
node in the API's return order — so `lium up --gpu RTX4090` rented a $0.58/h
node while `lium ls` listed starred RTX 4090s at $0.30/h, and with `-y`
nothing named the pick before the pod was billing.
"""

from types import SimpleNamespace

from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.up import command as up_module
from lium.cli.up.actions import ResolveExecutorAction
from lium.sdk import ExecutorInfo

GIB_KB = 1024 * 1024


def _executor(huid: str, price: float, ram_gb: int = 64, download: float = 1000.0) -> ExecutorInfo:
    return ExecutorInfo(
        id=f"id-{huid}",
        huid=huid,
        machine_name="NVIDIA GeForce RTX 4090",
        gpu_type="RTX4090",
        gpu_count=1,
        price_per_hour=price,
        price_per_gpu=price,
        location={"country": "Germany", "country_code": "DE"},
        specs={
            "gpu": {"count": 1, "details": [{"name": "RTX 4090", "capacity": 24564, "pcie_speed": 16}]},
            "ram": {"total": ram_gb * GIB_KB},
            "hard_disk": {"total": 1000 * GIB_KB},
        },
        status="available",
        docker_in_docker=False,
        ip="1.2.3.4",
        effective_download_speed_mbps=download,
        effective_upload_speed_mbps=download,
    )


# Both Pareto-optimal: same download, the pricier one has more RAM. The API
# happens to list the expensive one first — the order the bug depended on.
EXPENSIVE = _executor("pricey-node-aa", 0.58, ram_gb=256)
CHEAP = _executor("thrifty-node-bb", 0.30, ram_gb=64)
# Cheaper still, but too slow to be starred: must not be picked over the frontier.
SLOW_AND_CHEAP = _executor("sluggish-node-cc", 0.20, ram_gb=64, download=50.0)


class _FakeLium:
    def __init__(self, *args, **kwargs):
        pass

    def supports(self, feature):
        return False  # an older backend: the client-side pick under test here

    def ls(self, gpu_type=None, **kwargs):
        return [EXPENSIVE, CHEAP, SLOW_AND_CHEAP]

    def get_executor(self, executor_id):
        return EXPENSIVE


def _resolve(monkeypatch, **ctx):
    monkeypatch.setattr("lium.cli.ls.command.ls_store_executor", lambda **kwargs: [])
    return ResolveExecutorAction().execute({"lium": _FakeLium(), **ctx})


def test_auto_select_picks_the_cheapest_pareto_node(monkeypatch):
    result = _resolve(monkeypatch, gpu="RTX4090")

    assert result.ok
    assert result.data["executor"].huid == "thrifty-node-bb"
    assert result.data["auto_selected"] is True
    assert result.data["candidates"] == 2


def test_equal_prices_keep_the_listing_order(monkeypatch):
    same_price = _executor("same-price-dd", 0.30, ram_gb=64)
    monkeypatch.setattr(_FakeLium, "ls", lambda self, **kwargs: [same_price, CHEAP])

    result = _resolve(monkeypatch, gpu="RTX4090")

    assert result.data["executor"].huid == "same-price-dd"


def test_explicit_node_id_is_not_re_ranked(monkeypatch):
    result = _resolve(monkeypatch, executor_id="id-pricey-node-aa")

    assert result.data["executor"] is EXPENSIVE
    assert "auto_selected" not in result.data


def test_up_names_the_pick_and_its_price_before_renting(monkeypatch):
    rented: dict = {}

    class _RentingLium(_FakeLium):
        # a server without workspaces: `up` reads it for its workspace line (DAH-3033)
        workspaces = SimpleNamespace(current=lambda: None)

        def default_docker_template(self, executor_id):
            return SimpleNamespace(id="tpl-1", name="pytorch")

        def get_deployment_estimate(self, executor_id, template_id):
            return {}

        def up(self, **kwargs):
            rented.update(kwargs)
            return {"id": "pod-uuid-1", "name": kwargs["name"]}

        def ps(self):
            return [SimpleNamespace(id="pod-uuid-1", huid="thrifty-node-bb", name="thrifty-node-bb",
                                    status="RUNNING", ssh_cmd="ssh root@1.2.3.4", ports={"22": 10022})]

        def wait_ready(self, pod, *, timeout=None, poll_interval=None, on_poll=None):
            # the CLI waits through Lium.wait_ready (DAH-2558); the pod here is ready on the first look
            return self.ps()[0]

    monkeypatch.setattr(up_module, "Lium", _RentingLium)
    monkeypatch.setattr(up_module, "ensure_config", lambda: None)
    monkeypatch.setattr("lium.cli.ls.command.ls_store_executor", lambda **kwargs: [])

    result = CliRunner().invoke(cli, ["up", "--gpu", "RTX4090", "-y", "--no-ssh"])

    assert result.exit_code == 0, result.output
    assert rented["executor_id"] == "id-thrifty-node-bb"
    selected_line = next(line for line in result.output.splitlines() if "Selected" in line)
    assert "thrifty-node-bb" in selected_line
    assert "$0.30/h" in selected_line
    assert "cheapest of 2 optimal" in selected_line
    assert result.output.index("Selected") < result.output.index("ready")


def test_help_states_the_cheapest_pareto_rule():
    result = CliRunner().invoke(up_module.up_command, ["--help"])

    assert result.exit_code == 0
    assert "cheapest" in result.output
    assert "best node" not in result.output
