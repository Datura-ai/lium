"""DAH-2980: `lium up <filters>` rents row 1 of `lium ls <same filters>`, and says which.

`ResolveExecutorAction` took `pareto_executors[0]` — the first Pareto-optimal
node in the API's return order — so `lium up --gpu RTX4090` rented a $0.58/h
node while `lium ls` listed RTX 4090s at $0.30/h on row 1, and with `-y`
nothing named the pick before the pod was billing. One rule now: the pick is
the first row `ls` prints for the same filters (Mikhail, PR #164 review).
"""

from types import SimpleNamespace

from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.up import command as up_module
from lium.cli.ls import display
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


# The API happens to list the expensive node first — the order the bug depended on.
EXPENSIVE = _executor("pricey-node-aa", 0.58, ram_gb=256)
CHEAP = _executor("thrifty-node-bb", 0.30, ram_gb=64)
# Slow (never ★ in `ls`) but not the cheapest either; it sits on row 2.
SLOW = _executor("sluggish-node-cc", 0.40, ram_gb=64, download=50.0)


class _FakeLium:
    def __init__(self, *args, **kwargs):
        pass

    def supports(self, feature):
        return False  # an older backend: the client-side pick under test here

    def ls(self, gpu_type=None, **kwargs):
        return [EXPENSIVE, CHEAP, SLOW]

    def get_executor(self, executor_id):
        return EXPENSIVE


def _resolve(monkeypatch, **ctx):
    monkeypatch.setattr("lium.cli.ls.command.ls_store_executor", lambda **kwargs: [])
    return ResolveExecutorAction().execute({"lium": _FakeLium(), **ctx})


def test_auto_select_rents_row_1_of_ls(monkeypatch):
    # The API lists the $0.58 node first; `lium ls` prints the $0.30 node on row 1.
    result = _resolve(monkeypatch, gpu="RTX4090")

    assert result.ok
    assert result.data["executor"].huid == "thrifty-node-bb"
    assert result.data["auto_selected"] is True
    assert result.data["candidates"] == 3


def test_the_pick_is_what_ls_prints_first_for_the_same_nodes(monkeypatch):
    # The one rule, checked against `ls` itself rather than restated here: a mixed
    # fleet (1× and 8× nodes, a split host, a slow node, an unpriced node) in an
    # order the API might return, and the pick is `sort_executors(...)[0]`.
    fleet = [
        _executor("octet-node-ee", 0.25, gpu_count=8),
        _executor("solo-node-ff", 0.30, gpu_count=1, country=("United States", "US")),
        _executor("split-node-gg", 0.27, gpu_count=8, available_gpu_count=1),
        _executor("sluggish-node-cc", 0.20, download=50.0),
        _executor("gratis-node-hh", 0.0),
    ]
    monkeypatch.setattr(_FakeLium, "ls", lambda self, **kwargs: list(fleet))

    result = _resolve(monkeypatch, gpu="RTX4090")

    ls_rows, _ = display.sort_executors(list(fleet))
    assert result.data["executor"] is ls_rows[0]
    assert result.data["executor"].huid == "sluggish-node-cc"  # cheapest $/GPU·h, as `ls` shows it
    assert result.data["candidates"] == 5


def test_up_has_no_rule_of_its_own_a_cheaper_per_gpu_8x_node_is_row_1(monkeypatch):
    # `ls` sorts by $/GPU·h, so the 8× node at $0.25/GPU is row 1 although it bills
    # $2.00/h against the 1× node's $0.30/h. `up` rents that row; the Selected line
    # names the total before anything is billed, and -c 1 pins the count.
    eight = _executor("octet-node-ee", 0.25, gpu_count=8)
    single = _executor("solo-node-ff", 0.30, gpu_count=1)
    monkeypatch.setattr(_FakeLium, "ls", lambda self, **kwargs: [single, eight])

    result = _resolve(monkeypatch, gpu="RTX4090")

    assert result.data["executor"].huid == "octet-node-ee"
    assert result.data["executor"].price_per_hour == 2.0
    assert _resolve(monkeypatch, gpu="RTX4090", count=1).data["executor"].huid == "solo-node-ff"


def test_an_unpriced_node_is_last_in_ls_and_never_the_pick_over_a_priced_one(monkeypatch):
    # The SDK maps a missing price to 0; `ls` sorts it last, so `up` does too.
    free = _executor("gratis-node-gg", 0.0)
    paid = _executor("solo-node-ff", 0.30)
    monkeypatch.setattr(_FakeLium, "ls", lambda self, **kwargs: [free, paid])

    result = _resolve(monkeypatch, gpu="RTX4090")

    assert result.data["executor"].huid == "solo-node-ff"


def test_with_count_only_that_count_is_listed(monkeypatch):
    eight_cheap = _executor("octet-node-ee", 0.25, gpu_count=8)
    eight_dear = _executor("octet-node-gg", 0.28, gpu_count=8)
    single = _executor("solo-node-ff", 0.30, gpu_count=1, country=("United States", "US"))
    monkeypatch.setattr(_FakeLium, "ls", lambda self, **kwargs: [eight_dear, eight_cheap, single])

    result = _resolve(monkeypatch, gpu="RTX4090", count=8)

    assert result.data["executor"].huid == "octet-node-ee"
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
    assert "row 1 of 'lium ls'" in selected_line
    assert result.output.index("Selected") < result.output.index("ready")


def test_help_states_the_one_rule():
    result = CliRunner().invoke(up_module.up_command, ["--help"])

    assert result.exit_code == 0
    assert "row 1 of 'lium ls' with the same filters" in result.output
    assert "optimal" not in result.output
    assert "best node" not in result.output
