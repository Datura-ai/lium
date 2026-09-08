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
    available_gpu_count=None,
) -> ExecutorInfo:
    # `price` is per GPU per hour, the API's `price_per_gpu`; the SDK derives the
    # hourly total for the whole host as price_per_gpu * gpu_count. A rent with no
    # -c takes the node's free GPUs (available_gpu_count, DAH-2877) at that $/GPU.
    return ExecutorInfo(
        id=f"id-{huid}",
        huid=huid,
        machine_name="NVIDIA GeForce RTX 4090",
        gpu_type="RTX4090",
        gpu_count=gpu_count,
        price_per_hour=price * gpu_count,
        price_per_gpu=price,
        available_gpu_count=available_gpu_count,
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


def test_without_count_a_cheaper_per_gpu_8x_node_does_not_hide_an_equal_1x_node(monkeypatch):
    # Same country, equal specs: on $/GPU the 8× node dominates the 1× node and
    # would push it off a frontier drawn over both counts before the total-$/h
    # ranking runs, renting $2.00/h instead of $0.30/h. The frontier is drawn per
    # GPU count, so both are optimal and the cheaper hour wins.
    eight = _executor("octet-node-ee", 0.25, gpu_count=8)
    single = _executor("solo-node-ff", 0.30, gpu_count=1)
    monkeypatch.setattr(_FakeLium, "ls", lambda self, **kwargs: [eight, single])

    result = _resolve(monkeypatch, gpu="RTX4090")

    assert result.data["candidates"] == 2
    assert result.data["executor"].huid == "solo-node-ff"
    assert result.data["executor"].price_per_hour == 0.30


def test_without_count_a_split_host_is_ranked_by_the_gpus_it_would_rent(monkeypatch):
    # An 8× host with one GPU free rents that one GPU: $0.25/h, not the host's
    # $2.00/h. It competes with the 1× node in the 1-GPU group and wins on price.
    split = _executor("octet-node-ee", 0.25, gpu_count=8, available_gpu_count=1)
    single = _executor("solo-node-ff", 0.30, gpu_count=1)
    monkeypatch.setattr(_FakeLium, "ls", lambda self, **kwargs: [single, split])

    result = _resolve(monkeypatch, gpu="RTX4090")

    assert result.data["executor"].huid == "octet-node-ee"
    assert result.data["rent_count"] == 1
    assert result.data["rent_price"] == 0.25


def test_when_no_node_is_optimal_the_cheapest_match_is_picked_and_flagged(monkeypatch):
    # Every match is below the download floor, so the frontier is empty; the
    # cheapest match is still rented, and the caller is told nothing was optimal.
    slow_dear = _executor("sluggish-node-cc", 0.40, download=50.0)
    slow_cheap = _executor("sluggish-node-dd", 0.20, download=50.0)
    monkeypatch.setattr(_FakeLium, "ls", lambda self, **kwargs: [slow_dear, slow_cheap])

    result = _resolve(monkeypatch, gpu="RTX4090")

    assert result.data["executor"].huid == "sluggish-node-dd"
    assert result.data["pareto"] is False
    assert result.data["candidates"] == 2


def test_an_unpriced_or_fully_booked_node_never_ranks_as_the_cheapest(monkeypatch):
    # The SDK maps a missing price to 0 and a booked node reports 0 free GPUs;
    # neither is a rent, so a $0.30 node beats both.
    free = _executor("gratis-node-gg", 0.0)
    booked = _executor("booked-node-hh", 0.10, gpu_count=8, available_gpu_count=0)
    paid = _executor("solo-node-ff", 0.30)
    monkeypatch.setattr(_FakeLium, "ls", lambda self, **kwargs: [free, booked, paid])

    result = _resolve(monkeypatch, gpu="RTX4090")

    assert result.data["executor"].huid == "solo-node-ff"


def test_with_count_only_that_count_is_ranked(monkeypatch):
    eight_cheap = _executor("octet-node-ee", 0.25, gpu_count=8)
    eight_dear = _executor("octet-node-gg", 0.28, gpu_count=8)
    single = _executor("solo-node-ff", 0.30, gpu_count=1, country=("United States", "US"))
    monkeypatch.setattr(_FakeLium, "ls", lambda self, **kwargs: [eight_dear, eight_cheap, single])

    result = _resolve(monkeypatch, gpu="RTX4090", count=8)

    assert result.data["executor"].huid == "octet-node-ee"
    assert result.data["rent_count"] == 8
    assert result.data["rent_price"] == 2.0


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

        _POD = SimpleNamespace(id="pod-uuid-1", huid="thrifty-node-bb", name="thrifty-node-bb",
                               status="RUNNING", ssh_cmd="ssh root@1.2.3.4", ports={"22": 10022})

        def ps(self):
            return [self._POD]

        # main's `up` waits through Lium.wait_ready (DAH-3005) instead of polling ps()
        def wait_ready(self, pod_id, timeout=None, poll_interval=None, on_poll=None):
            return self._POD

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


def test_the_confirmation_names_the_same_rent_as_the_selected_line(monkeypatch):
    # A split host rents its one free GPU: both lines the renter reads before
    # answering must say 1× at $0.25/h, not the host's 8× at $2.00/h.
    split = _executor("octet-node-ee", 0.25, gpu_count=8, available_gpu_count=1)

    class _AskingLium(_FakeLium):
        def ls(self, gpu_type=None, **kwargs):
            return [split]

        def default_docker_template(self, executor_id):
            return SimpleNamespace(id="tpl-1", name="pytorch")

        def get_deployment_estimate(self, executor_id, template_id):
            return {}

    asked: list = []

    def _decline(message, default=False):
        asked.append(message)
        return False

    monkeypatch.setattr(up_module, "Lium", _AskingLium)
    monkeypatch.setattr(up_module, "ensure_config", lambda: None)
    monkeypatch.setattr(up_module.ui, "confirm", _decline)
    monkeypatch.setattr("lium.cli.ls.command.ls_store_executor", lambda **kwargs: [])

    result = CliRunner().invoke(cli, ["up", "--gpu", "RTX4090", "--no-ssh"])

    assert result.exit_code == 0, result.output
    selected_line = next(line for line in result.output.splitlines() if "Selected" in line)
    assert len(asked) == 1, asked
    for line in (selected_line, asked[0]):
        assert "1×RTX4090" in line, line
        assert "$0.25/h" in line, line


def test_help_states_the_cheapest_pareto_rule():
    result = CliRunner().invoke(up_module.up_command, ["--help"])

    assert result.exit_code == 0
    assert "cheapest" in result.output
    assert "best node" not in result.output
