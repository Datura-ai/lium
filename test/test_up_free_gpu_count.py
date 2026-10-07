"""`lium up -c N` without a NODE_ID matches the free GPUs a rent gets, and `up` keeps `ls` row numbers.

Self-contained on purpose: no import from test_up_auto_select.py, so these cases
survive whatever happens to that file.
"""

from lium.cli.up.actions import ResolveExecutorAction
from lium.sdk import ExecutorInfo


def _executor(huid: str, price: float, gpu_count: int = 1, available_gpu_count=None) -> ExecutorInfo:
    return ExecutorInfo(
        id=f"id-{huid}",
        huid=huid,
        machine_name="NVIDIA GeForce RTX 4090",
        gpu_type="RTX4090",
        gpu_count=gpu_count,
        price_per_hour=price * gpu_count,
        price_per_gpu=price,
        available_gpu_count=available_gpu_count,
        location={"country": "Germany", "country_code": "DE"},
        specs={
            "gpu": {"count": gpu_count, "details": [{"name": "RTX 4090", "capacity": 24564, "pcie_speed": 16}]},
            "ram": {"total": 64 * 1024 * 1024},
            "hard_disk": {"total": 1000 * 1024 * 1024},
        },
        status="available",
        docker_in_docker=False,
        ip="1.2.3.4",
        effective_download_speed_mbps=1000.0,
        effective_upload_speed_mbps=1000.0,
    )


class _FakeLium:
    nodes: list = []

    def __init__(self, *args, **kwargs):
        pass

    def supports(self, feature):
        return False  # an older backend: the client-side pick under test here

    def ls(self, gpu_type=None, **kwargs):
        return list(self.nodes)


def _resolve(monkeypatch, nodes=(), **ctx):
    monkeypatch.setattr(_FakeLium, "nodes", list(nodes))
    monkeypatch.setattr("lium.cli.ls.command.ls_store_executor", lambda **kwargs: [])
    return ResolveExecutorAction().execute({"lium": _FakeLium(), **ctx})


def test_count_matches_the_free_gpus_a_rent_gets_not_the_host_total(monkeypatch):
    # A rent with no NODE_ID names no count and takes the free GPUs: the 4-GPU host with
    # 2 free would bill 2 GPUs to a renter who asked for 4, then fail gpu_count_mismatch.
    half_free = _executor("half-free-node-aa", 0.10, gpu_count=4, available_gpu_count=2)
    all_free = _executor("all-free-node-bb", 0.30, gpu_count=4, available_gpu_count=4)
    nodes = [half_free, all_free]

    four = _resolve(monkeypatch, nodes, count=4)
    two = _resolve(monkeypatch, nodes, count=2)

    assert four.data["executor"].huid == "all-free-node-bb"
    assert four.data["candidates"] == 1
    assert two.data["executor"].huid == "half-free-node-aa"


def test_count_with_no_node_that_has_that_many_free_gpus_rents_nothing(monkeypatch):
    nodes = [_executor("half-free-node-aa", 0.10, gpu_count=4, available_gpu_count=2)]

    result = _resolve(monkeypatch, nodes, count=4)

    assert not result.ok
    assert "GPU count=4" in result.error


def test_auto_select_keeps_the_row_numbers_of_the_last_ls(monkeypatch):
    # `lium up 3` reads the rows `lium ls` stored; picking a node must not overwrite them.
    def _store(**kwargs):
        raise AssertionError("up rewrote the `lium ls` row cache")

    monkeypatch.setattr("lium.cli.ls.command.ls_store_executor", _store)
    monkeypatch.setattr("lium.cli.ls.command.store_executor_selection", _store, raising=False)
    monkeypatch.setattr("lium.cli.utils.store_executor_selection", _store, raising=False)

    monkeypatch.setattr(_FakeLium, "nodes", [_executor("node-aa", 0.30)])
    result = ResolveExecutorAction().execute({"lium": _FakeLium(), "gpu": "RTX4090"})

    assert result.ok
