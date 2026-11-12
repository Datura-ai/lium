"""DAH-6272: `lium up` tells the renter about a node's maintenance or removal notice before renting, and its own pick
takes such a node only when nothing else matches (an agent rented a node due for removal, owner 12 Nov 2026)."""

from types import SimpleNamespace

from lium.cli.up.actions import ResolveExecutorAction
from lium.sdk import Lium

REMOVAL = {"starting_at": "2026-11-15T11:00:00Z", "expiring_at": "2026-11-15T11:00:00Z", "permanent_removal": True}


def _pick(monkeypatch, nodes):
    lium = SimpleNamespace(supports=lambda feature: False, ls=lambda **kwargs: list(nodes))
    monkeypatch.setattr("lium.cli.ls.display.sort_executors", lambda executors, show_pareto: (executors, None))
    return ResolveExecutorAction().execute({"lium": lium}).data["executor"].huid


def test_the_pick_skips_a_node_under_notice_while_another_matches(monkeypatch):
    leaving = SimpleNamespace(huid="leaving", scheduled_notice=REMOVAL)
    staying = SimpleNamespace(huid="staying", scheduled_notice=None)

    assert _pick(monkeypatch, [leaving, staying]) == "staying"
    # never hidden: the only match is still rented
    assert _pick(monkeypatch, [leaving]) == "leaving"


def _node(**fields):
    return Lium.__new__(Lium)._dict_to_executor_info({"id": "e1", "machine_name": "NVIDIA H100", **fields})


def test_the_listing_notice_becomes_a_warning_naming_kind_and_times():
    removal = _node(scheduled_notice=REMOVAL)
    maintenance = _node(scheduled_notice={**REMOVAL, "permanent_removal": False})

    assert _node().notice_warning is None
    assert removal.notice_warning == (
        f"{removal.huid} is scheduled for removal from 2026-11-15 11:00 UTC: a pod on it is deleted at 2026-11-15 11:00 UTC."
    )
    assert "maintenance" in maintenance.notice_warning and "may go offline" in maintenance.notice_warning
    behind = _node(scheduled_notice={**REMOVAL, "permanent_removal": False}, removal_at="2026-11-20T09:30:00Z")
    assert behind.notice_warning.endswith("deleted at 2026-11-20 09:30 UTC.")
