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


def _executor(
    huid: str,
    price: float,
    ram_gb: int = 64,
    download: float = 1000.0,
    gpu_count: int = 1,
    country: tuple = ("Germany", "DE"),
) -> ExecutorInfo:
    # `price` is per GPU per hour, the API's `price_per_gpu`; the SDK derives the
    # hourly total the renter pays as price_per_gpu * gpu_count.
    return ExecutorInfo(
        id=f"id-{huid}",
        huid=huid,
        machine_name="NVIDIA GeForce RTX 4090",
        gpu_type="RTX4090",
        gpu_count=gpu_count,
        price_per_hour=price * gpu_count,
        price_per_gpu=price,
        location={"country": country[0], "country_code": country[1]},
        specs={
            "gpu": {"count": gpu_count, "details": [{"name": "RTX 4090", "capacity": 24564, "pcie_speed": 16}]},
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


def test_without_count_the_cheapest_hourly_node_wins_over_the_cheapest_per_gpu(monkeypatch):
    # Both Pareto-optimal: the 8× node is cheaper per GPU, the 1× node is in the
    # US. Per GPU the 8× wins ($0.25 < $0.30); per hour the renter pays $2.00
    # against $0.30, so with no -c the 1× node must be the pick.
    eight = _executor("octet-node-ee", 0.25, gpu_count=8)
    single = _executor("solo-node-ff", 0.30, gpu_count=1, country=("United States", "US"))
    monkeypatch.setattr(_FakeLium, "ls", lambda self, **kwargs: [eight, single])

    result = _resolve(monkeypatch, gpu="RTX4090")

    assert result.data["candidates"] == 2
    assert result.data["executor"].huid == "solo-node-ff"
    assert result.data["executor"].price_per_hour == 0.30


def test_with_count_only_that_count_is_ranked(monkeypatch):
    eight_cheap = _executor("octet-node-ee", 0.25, gpu_count=8)
    eight_dear = _executor("octet-node-gg", 0.28, gpu_count=8)
    single = _executor("solo-node-ff", 0.30, gpu_count=1, country=("United States", "US"))
    monkeypatch.setattr(_FakeLium, "ls", lambda self, **kwargs: [eight_dear, eight_cheap, single])

    result = _resolve(monkeypatch, gpu="RTX4090", count=8)

    assert result.data["executor"].huid == "octet-node-ee"
    assert result.data["executor"].price_per_hour == 2.0


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
