"""DAH-6737: `lium spend` leaves out of the burn a pod its /pods row marks `billing_paused`, read from the row as sent;
a row without the flag (an older server) is billed by its status."""

import dataclasses
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from lium.cli.spend.report import build_report
from lium.sdk import Lium
from lium.sdk.models import PodInfo

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def _row(status, **sent):
    # a /pods row in the platform's shape: billing_paused is a top-level bool on each pod, absent on older servers
    return {
        "id": "p1",
        "huid": "calm-owl-aa",
        "pod_name": "train",
        "status": status,
        "created_at": (NOW - timedelta(hours=1)).replace(tzinfo=None).isoformat(),
        "price": 2.0,
        "executor": {"id": "e1", "specs": {"gpu": {"count": 1, "details": [{"name": "H100"}]}}},
        **sent,
    }


@pytest.mark.parametrize(
    ("status", "sent", "burn"),
    [
        pytest.param("RUNNING", {"billing_paused": True}, 0.0, id="paused"),
        pytest.param("REBOOT_FAILED", {"billing_paused": True}, 0.0, id="reboot-failed-paused"),
        pytest.param("RUNNING", {"billing_paused": False}, 2.0, id="billed"),
        pytest.param("REBOOT_FAILED", {"billing_paused": False}, 2.0, id="reboot-failed-billed"),
        pytest.param("RUNNING", {}, 2.0, id="older-server-without-the-flag"),
        pytest.param("PENDING", {}, 0.0, id="pending"),
    ],
)
def test_the_burn_follows_the_pods_rows_billing_paused(monkeypatch, status, sent, burn):
    # the row goes through the SDK's own /pods parsing, as `lium spend` reads it
    lium = Lium.__new__(Lium)
    monkeypatch.setattr(lium, "_request", lambda *a, **k: SimpleNamespace(json=lambda: [_row(status, **sent)]), raising=False)

    report = build_report(lium.ps(), balance=10.0, now=NOW)

    assert report.burn_per_hour == burn
    assert report.pods[0].billable is (burn > 0)
    assert report.runway_hours == (5.0 if burn else None)


@pytest.mark.parametrize(("sent", "parsed"), [({"billing_paused": True}, True), ({"billing_paused": False}, False), ({}, False)])
def test_ps_carries_the_rows_billing_paused(monkeypatch, sent, parsed):
    lium = Lium.__new__(Lium)
    row = {"id": "p1", "pod_name": "train", "status": "REBOOT_FAILED", "created_at": "2026-11-19T11:00:00", **sent}
    monkeypatch.setattr(lium, "_request", lambda *a, **k: SimpleNamespace(json=lambda: [row]), raising=False)

    (pod,) = lium.ps()

    assert pod.billing_paused is parsed


def test_a_positional_podinfo_call_written_before_billing_paused_keeps_backup_notice():
    # every field but billing_paused, in order, as a call written before it existed passes them
    names = [f.name for f in dataclasses.fields(PodInfo) if f.init and f.name != "billing_paused"]
    args = ["notice" if n == "backup_notice" else None for n in names]

    pod = PodInfo(*args)

    assert pod.backup_notice == "notice"
    assert pod.billing_paused is False
