"""A 402 funding refusal inside a pod batch (`rm --in`, `reboot`, `schedules rm`) keeps its type: the pods that
went through are still reported, then the command exits 6 with the server's code and hint, not a generic failure."""

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.reboot import command as reboot_module
from lium.cli.rm import command as rm_module
from lium.cli.schedules.rm import command as schedules_rm_module
from lium.cli.utils import EXIT_PERMISSION_DENIED
from lium.sdk import LiumInsufficientBalanceError
from test_workspaces_cli import home  # noqa: F401 - the `home` fixture

pytestmark = pytest.mark.usefixtures("home")

HINT = "Add funds with 'lium topup' or 'lium fund', or on the Billing page, then retry."


def _pods():
    return [
        SimpleNamespace(
            id=f"id-{h}", huid=h, name=h, executor=SimpleNamespace(price_per_hour=1.0),
            created_at="2026-11-20T00:00:00", removal_scheduled_at="2026-11-21T00:00:00",
        )
        for h in ("pod-a", "pod-b")
    ]


def _fake(refused):
    def refuse(pod, **_):
        if pod.huid in refused:
            raise LiumInsufficientBalanceError("Insufficient balance: no balance", code="account_unfunded", hint=HINT)

    class FakeLium:
        workspaces = SimpleNamespace(current=lambda: None)
        config = SimpleNamespace(workspace=None, workspace_id=None, workspace_explicit=False)

        def __init__(self, *a, **k):
            pass

        def ps(self):
            return _pods()

        schedule_termination = staticmethod(refuse)
        reboot = staticmethod(refuse)
        cancel_scheduled_termination = staticmethod(refuse)

    return FakeLium


COMMANDS = {
    "rm --in": (rm_module, ["rm", "pod-a,pod-b", "--in", "2h", "--yes"]),
    "reboot": (reboot_module, ["reboot", "pod-a,pod-b"]),
    "schedules rm": (schedules_rm_module, ["schedules", "rm", "1,2"]),
}


@pytest.mark.parametrize("name", COMMANDS)
@pytest.mark.parametrize("refused, done", [({"pod-a", "pod-b"}, None), ({"pod-a"}, "pod-b")])
def test_a_funding_refusal_in_a_batch_exits_6_with_its_hint_after_reporting_the_rest(
    monkeypatch, name, refused, done
):
    module, args = COMMANDS[name]
    monkeypatch.setattr(module, "Lium", _fake(refused))

    result = CliRunner().invoke(cli, args)

    assert result.exit_code == EXIT_PERMISSION_DENIED, result.output
    assert "lium topup" in result.output
    if done:
        assert done in result.output
