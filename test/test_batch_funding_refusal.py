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
import json

from lium.sdk import LiumBudgetExceededError, LiumInsufficientBalanceError
from test_workspaces_cli import home  # noqa: F401 - the `home` fixture

pytestmark = pytest.mark.usefixtures("home")

HINT = "Add funds with 'lium topup' or 'lium fund', or on the Billing page, then retry."
BUDGET_HINT = "Raise or clear the budget on the key."


def _funding():
    return LiumInsufficientBalanceError("Insufficient balance: no balance", code="account_unfunded", hint=HINT)


def _budget():
    return LiumBudgetExceededError("API key is at its budget", code="API_KEY_BUDGET_EXCEEDED", hint=BUDGET_HINT)


def _pods():
    return [
        SimpleNamespace(
            id=f"id-{h}", huid=h, name=h, executor=SimpleNamespace(price_per_hour=1.0),
            created_at="2026-11-20T00:00:00", removal_scheduled_at="2026-11-21T00:00:00",
        )
        for h in ("pod-a", "pod-b")
    ]


def _fake(refused, error=_funding):
    # refused: a set of huids refused with `error`, or a dict huid -> error factory for a mixed batch
    def refuse(pod, **_):
        if pod.huid in refused:
            raise (refused[pod.huid] if isinstance(refused, dict) else error)()

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


@pytest.mark.parametrize("name", ["rm --in", "schedules rm"])
@pytest.mark.parametrize("refused, done", [({"pod-a", "pod-b"}, None), ({"pod-a"}, "pod-b")])
def test_a_key_budget_refusal_on_extension_or_cancellation_exits_6_with_its_hint(monkeypatch, name, refused, done):
    module, args = COMMANDS[name]
    monkeypatch.setattr(module, "Lium", _fake(refused, _budget))

    result = CliRunner().invoke(cli, args)

    assert result.exit_code == EXIT_PERMISSION_DENIED, result.output
    assert "budget on the key" in result.output
    if done:
        assert done in result.output


def test_a_mixed_rm_in_batch_reports_budget_and_funding_refusals_apart(monkeypatch):
    monkeypatch.setattr(rm_module, "Lium", _fake({"pod-a": _budget, "pod-b": _funding}))

    result = CliRunner().invoke(cli, ["rm", "pod-a,pod-b", "--in", "2h", "--yes", "--format", "json"])

    payload = json.loads(result.stdout)
    assert result.exit_code == EXIT_PERMISSION_DENIED, result.output
    assert payload["budget_refused"] == ["pod-a"]
    assert payload["funding_refused"] == [{"huid": "pod-b", "code": "account_unfunded", "hint": HINT}]


REFUSAL_TABLE = [
    {"pod-a": _funding, "pod-b": _funding},  # funding only
    {"pod-a": _budget, "pod-b": _budget},  # budget only
    {"pod-a": _budget, "pod-b": _funding},  # budget -> funding
    {"pod-a": _funding, "pod-b": _budget},  # funding -> budget
]


def _groups(refused):
    return {kind: [h for h, f in refused.items() if f is factory] for kind, factory in (("budget", _budget), ("funding", _funding))}


@pytest.mark.parametrize("name", ["rm --in", "schedules rm"])
@pytest.mark.parametrize("refused", REFUSAL_TABLE)
def test_a_refused_batch_names_every_refused_pod_by_kind_with_its_hint(monkeypatch, name, refused):
    module, args = COMMANDS[name]
    monkeypatch.setattr(module, "Lium", _fake(refused))

    result = CliRunner().invoke(cli, args)

    groups, text = _groups(refused), " ".join(result.output.split())  # the console wraps long lines
    assert result.exit_code == EXIT_PERMISSION_DENIED, result.output
    if groups["funding"]:
        assert f"No balance on the account for pods: {', '.join(groups['funding'])} (Add funds" in text
    if groups["budget"]:
        assert f"Refused by the API key's budget for pods: {', '.join(groups['budget'])} (Raise or clear" in text


@pytest.mark.parametrize("name", ["rm --in", "schedules rm"])
@pytest.mark.parametrize("refused", REFUSAL_TABLE)
def test_a_refused_batch_carries_both_groups_in_the_json_error_envelope(monkeypatch, name, refused):
    module, args = COMMANDS[name]
    monkeypatch.setattr(module, "Lium", _fake(refused))
    monkeypatch.setenv("LIUM_OUTPUT", "json")

    result = CliRunner().invoke(cli, args)

    envelope = json.loads(result.stderr.strip().splitlines()[-1])
    groups = _groups(refused)
    assert result.exit_code == EXIT_PERMISSION_DENIED, result.output
    assert [r["huid"] for r in envelope["data"].get("funding_refused", [])] == groups["funding"]
    assert [r["huid"] for r in envelope["data"].get("budget_refused", [])] == groups["budget"]
    for row in envelope["data"].get("funding_refused", []):
        assert (row["code"], row["hint"]) == ("account_unfunded", HINT)
    for row in envelope["data"].get("budget_refused", []):
        assert (row["code"], row["hint"]) == ("API_KEY_BUDGET_EXCEEDED", BUDGET_HINT)
