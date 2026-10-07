"""`lium ls` with no API key: the public listing answers, so a visitor sees what is on offer and at what
price before signing up, and is told how to rent."""

from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.ls import command as ls_module
from lium.cli.utils import SIGNUP_NUDGE
from lium.sdk import ExecutorInfo

NODE = ExecutorInfo(
    id="id-n1", huid="keyless-node", machine_name="m", gpu_type="H100", gpu_count=1, price_per_hour=2.0,
    price_per_gpu=2.0, location={"country": "United States", "country_code": "US"},
    specs={"gpu": {"details": [{"name": "H100", "capacity": 81559}]}}, status="available",
    docker_in_docker=False, ip="",
)


def test_ls_without_key_lists_nodes_and_says_how_to_rent(monkeypatch):
    configs = []

    class KeylessLium:
        def __init__(self, config=None, **kwargs):
            if config is None:
                raise ValueError("No API key found. Set LIUM_API_KEY or ~/.lium/config.ini")
            configs.append(config)

        def ls(self, **kwargs):
            return [NODE]

    monkeypatch.setattr(ls_module, "Lium", KeylessLium)
    monkeypatch.setattr(ls_module, "store_executor_selection", lambda executors: None)

    result = CliRunner().invoke(cli, ["ls"], env={"COLUMNS": "400"})

    assert result.exit_code == 0, result.output
    assert "keyless-node" in result.output and SIGNUP_NUDGE in result.output
    assert [c.api_key for c in configs] == [""]
