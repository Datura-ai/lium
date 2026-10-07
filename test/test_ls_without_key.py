"""`lium ls` with no API key: the public listing answers, so a visitor sees what is on offer and at what
price before signing up, and is told how to rent."""

import pytest
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


@pytest.mark.parametrize(
    ("error", "env"),
    [
        ("No API key found. Set LIUM_API_KEY or ~/.lium/config.ini", {}),
        ("No API key is saved for workspace 'research'; run `lium keys create`", {"LIUM_WORKSPACE": "research"}),
    ],
)
def test_ls_without_key_lists_nodes_and_says_how_to_rent(monkeypatch, error, env):
    configs = []

    class KeylessLium:
        def __init__(self, config=None, **kwargs):
            if config is None:
                raise ValueError(error)
            configs.append(config)

        def ls(self, **kwargs):
            return [NODE]

    monkeypatch.setattr(ls_module, "Lium", KeylessLium)
    monkeypatch.setattr(ls_module, "store_executor_selection", lambda executors: None)

    result = CliRunner().invoke(cli, ["ls"], env={"COLUMNS": "400", **env})

    assert result.exit_code == 0, result.output
    assert "keyless-node" in result.output and SIGNUP_NUDGE in result.output
    assert [c.api_key for c in configs] == [""]


def test_real_sdk_without_a_key_raises_the_text_browsing_client_matches(monkeypatch, tmp_path):
    from lium.cli.utils import browsing_client
    from lium.sdk import Lium

    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("LIUM_API_KEY", "LIUM_WORKSPACE", "LIUM_BASE_URL"):
        monkeypatch.delenv(var, raising=False)

    client, anonymous = browsing_client(Lium)

    assert anonymous is True
    assert client.config.api_key == ""


def test_templates_without_key_lists_and_says_how_to_rent(monkeypatch):
    from lium.cli.templates import command as templates_module

    class KeylessLium:
        def __init__(self, config=None, **kwargs):
            if config is None:
                raise ValueError("No API key found. Set LIUM_API_KEY")

        def templates(self, **kwargs):
            return []

    monkeypatch.setattr(templates_module, "Lium", KeylessLium)

    result = CliRunner().invoke(cli, ["templates"], env={"COLUMNS": "400"})

    assert result.exit_code == 0, result.output
    assert SIGNUP_NUDGE in result.output
