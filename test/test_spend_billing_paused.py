"""DAH-6737: `lium spend` burns only what the platform bills. A REBOOT_FAILED pod is billed or not depending on why it
failed; the /pods row says which with `billing_paused`, and the burn rule reads that flag, not the status alone."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from lium.cli.spend.report import build_report
from lium.sdk import Lium

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def _pod(status, paused, price=2.0):
    return SimpleNamespace(
        id="p1",
        huid="calm-owl-aa",
        name="train",
        status=status,
        executor=SimpleNamespace(price_per_hour=price, gpu_count=1, gpu_type="H100"),
        gpu_count=1,
        created_at=(NOW - timedelta(hours=1)).replace(tzinfo=None).isoformat(),
        billing_paused=paused,
    )


@pytest.mark.parametrize(
    ("status", "paused", "burn"),
    [("REBOOT_FAILED", False, 2.0), ("REBOOT_FAILED", True, 0.0), ("RUNNING", True, 0.0), ("RUNNING", False, 2.0)],
)
def test_the_burn_counts_a_pod_only_while_the_api_does_not_mark_its_billing_paused(status, paused, burn):
    report = build_report([_pod(status, paused)], balance=10.0, now=NOW)

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
