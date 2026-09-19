"""Machine-readable output under one spelling everywhere.

`describe --json` and `ps --format json` meant the same thing with different
flags, `templates` had no JSON at all and hid the id `up --template_id` needs,
and an error under `--format json` came back as Rich text. A scripted caller
that learned one command could not carry that knowledge to the next.
"""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.sdk import Template
from lium.cli.cli import cli
from lium.cli import balance as balance_module
from lium.cli.describe import command as describe_module
from lium.cli.ls import command as ls_module
from lium.cli.ps import command as ps_module
from lium.cli.templates import command as templates_module
from lium.cli.templates import display as templates_display
from lium.cli.utils import EXIT_POD_NOT_FOUND

TEMPLATE_ID = "0f6b2c1e-7c4c-4a4e-9f1f-3d1c2b3a4d5e"


def _template() -> Template:
    return Template(
        id=TEMPLATE_ID,
        name="PyTorch 2.6",
        huid="brave-fox-3a",
        docker_image="daturaai/pytorch",
        docker_image_tag="2.6.0-py3.11-cuda12.5.1-devel-ubuntu24.04",
        category="PYTORCH",
        status="VERIFY_SUCCESS",
    )


def _pod() -> SimpleNamespace:
    return SimpleNamespace(
        id="pod-uuid-1",
        huid="eager-wolf-aa",
        name="my-pod",
        status="RUNNING",
        ssh_cmd="ssh root@203.0.113.10 -p 20022",
        ports={"22": 20022},
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        executor=None,
        template={"name": "PyTorch 2.6"},
        removal_scheduled_at=None,
        jupyter_installation_status=None,
        jupyter_url=None,
        enable_volume_encryption=None,
        volume_encryption_status=None,
    )


def _executor() -> SimpleNamespace:
    return SimpleNamespace(
        id="id-brave-orbit-b9",
        huid="brave-orbit-b9",
        gpu_type="H200",
        gpu_count=8,
        price_per_hour=16.0,
        price_per_gpu=2.0,
        location={"country": "United States", "country_code": "US"},
        download_speed=1000,
        upload_speed=1000,
        specs={"network": {"download_speed": 1000, "upload_speed": 1000}},
        docker_in_docker=False,
        max_cuda_version=12.8,
        tier="secure",
        # DAH-2924: the interconnect fields `ls`/`describe` JSON now carry; None = the node has not reported them
        interconnect=None,
        nvlink=None,
        link=None,
        p2p=None,
    )


class _FakeLium:
    def __init__(self, *args, **kwargs):
        pass

    def templates(self, search=None):
        return [_template()]

    def ps(self):
        return [_pod()]

    def pod(self, pod_id):
        # DAH-2932: `describe` asks GET /pods/{id} for the last event and node disk; nothing to add here
        return {}

    def ls(self, **kwargs):
        return [_executor()]

    def balance(self):
        return 42.5


@pytest.fixture
def fake_lium(monkeypatch):
    for module in (templates_module, ps_module, ls_module, balance_module, describe_module):
        monkeypatch.setattr(module, "Lium", _FakeLium)
    monkeypatch.setattr(ps_module, "ensure_config", lambda: None)
    monkeypatch.setattr(describe_module, "ensure_config", lambda: None)
    monkeypatch.setattr(ls_module, "store_executor_selection", lambda executors: None)


def _invoke(args):
    return CliRunner().invoke(cli, args)


def test_templates_format_json_carries_the_id_and_image(fake_lium):
    """`up --template_id` needs the id; the JSON view has to expose it."""
    result = _invoke(["templates", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == [{
        "id": TEMPLATE_ID,
        "huid": "brave-fox-3a",
        "name": "PyTorch 2.6",
        "docker_image": "daturaai/pytorch",
        "docker_image_tag": "2.6.0-py3.11-cuda12.5.1-devel-ubuntu24.04",
        "category": "PYTORCH",
        "status": "VERIFY_SUCCESS",
        "cuda_version": 12.5,
        "torch_version": "2.6.0",
        "arch": "hopper",
    }]


def test_templates_json_is_an_alias_for_format_json(fake_lium):
    assert _invoke(["templates", "--json"]).stdout == _invoke(["templates", "--format", "json"]).stdout


def test_templates_json_is_an_empty_list_when_there_is_nothing(fake_lium, monkeypatch):
    monkeypatch.setattr(_FakeLium, "templates", lambda self, search=None: [])

    result = _invoke(["templates", "--format", "json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == []


def test_templates_table_shows_the_id():
    table, _ = templates_display.build_templates_table([_template()])

    assert [column.header for column in table.columns][0] == "ID"
    assert TEMPLATE_ID in "".join(str(cell) for cell in table.columns[0]._cells)


@pytest.mark.parametrize("command", ["ps", "ls", "templates"])
def test_json_flag_matches_format_json(fake_lium, command):
    """The flag a caller learned on `describe` must work on the list commands too."""
    alias = _invoke([command, "--json"])
    explicit = _invoke([command, "--format", "json"])

    assert alias.exit_code == 0, alias.output
    assert alias.stdout == explicit.stdout
    json.loads(alias.stdout)


def test_ps_json_alias_is_hidden_from_help(fake_lium):
    """One documented spelling; the alias is there for muscle memory, not the docs."""
    result = _invoke(["ps", "--help"])

    assert "--format" in result.output
    assert "--json" not in result.output


@pytest.mark.parametrize("command", ["ps", "ls", "templates", "balance", "describe", "up"])
def test_help_mentions_the_machine_readable_format(fake_lium, command):
    result = _invoke([command, "--help"])

    assert "json" in result.output.lower()


@pytest.mark.parametrize("args", [["--format", "json"], ["--json"]])
def test_balance_json_names_the_currency(fake_lium, args):
    result = _invoke(["balance", *args])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["balance"] == 42.5
    assert payload["currency"] == "USD"
    # Existing consumers keyed on balance_usd keep working.
    assert payload["balance_usd"] == 42.5


def test_describe_accepts_format_json(fake_lium):
    result = _invoke(["describe", "my-pod", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["pod"]["huid"] == "eager-wolf-aa"


def test_errors_under_format_json_are_a_json_envelope(fake_lium):
    """A machine reader must get the envelope on stderr under either spelling."""
    result = _invoke(["ps", "no-such-pod-zz", "--format", "json"])

    assert result.exit_code == EXIT_POD_NOT_FOUND
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "pod_not_found"
