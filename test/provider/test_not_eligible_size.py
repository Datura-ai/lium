import pytest

from lium.cli.provider import _blocking
from lium.cli.provider._blocking import (
    NOT_GATED,
    SPLIT_TO_EARN_CODE,
    fallback_reasons,
    print_not_eligible,
)

SERVED_FIX = "Served by the portal: turn on GPU Splitting."


def _served_row() -> dict:
    entry = {
        "code": SPLIT_TO_EARN_CODE,
        "kind": "idle_pay",
        "title": "No idle pay for this node size",
        "fix": SERVED_FIX,
        "gating": False,
    }
    return {"id": "node-1", "blocking_reasons": [entry]}


def _fallback_row() -> dict:
    row: dict = {"id": "node-1"}
    row["blocking_reasons"] = fallback_reasons(
        row, [{"code": SPLIT_TO_EARN_CODE, "context": {}}]
    )
    return row


@pytest.mark.parametrize(
    ("row", "short", "expected"),
    [
        (_served_row(), False, SERVED_FIX),
        (_fallback_row(), False, "complete Docker Storage Setup first"),
        (_served_row(), True, "; turn on GPU Splitting"),
        (_fallback_row(), True, "; turn on GPU Splitting"),
    ],
)
def test_a_node_size_with_no_idle_pay_points_to_gpu_splitting(row, short, expected):
    with _blocking.console.capture() as captured:
        print_not_eligible([row], short=short)

    printed = " ".join(captured.get().split())
    assert expected in printed
    assert "no action" not in printed.lower()
    assert "room opens" not in printed and NOT_GATED[SPLIT_TO_EARN_CODE][1].startswith(
        "If GPU Splitting"
    )


def _partial_row(measured: str = "5 free ports", required: str = "at least 7 free ports") -> dict:
    # what lium-platform main serves: port_limited_remainder is in SECURE_LISTING_IDLE_PAY_GATING_CODES
    entry = {
        "code": "port_limited_remainder",
        "kind": "idle_pay",
        "title": "Too few free ports for the free GPUs",
        "message": f"The partly rented node has {measured}, so nobody can rent its free GPUs: no idle pay for them.",
        "measured": measured,
        "required": required,
        "fix": "Open more ports in the node's port range, or wait for the rental to end. The rented GPUs keep earning.",
        "gating": True,
        "secure_requirement": True,
    }
    return {"id": "node-1", "blocking_reasons": [entry]}


@pytest.mark.parametrize(
    ("short", "expected"),
    [
        (False, "Partial idle pay: the free GPUs earn only for 5 free ports; full idle pay needs at least 7 free ports"),
        (True, "node-1: partial idle pay (too few free ports for the free GPUs); open more ports"),
    ],
)
def test_a_paying_port_limited_remainder_reads_partial_idle_pay(short, expected):
    # stand check 404-08: the portal serves it with gating: true; it must not block (no panel, no exit 10)
    row = _partial_row()
    assert _blocking.node_reasons(row) == []
    assert _blocking.blocking_panel(row) is None

    with _blocking.console.capture() as captured:
        print_not_eligible([row], short=short)

    printed = " ".join(captured.get().split())
    assert expected in printed
    assert "not eligible" not in printed.lower()
    assert "no action" not in printed.lower()


@pytest.mark.parametrize(
    ("measured", "required"),
    [("2 free ports", "at least 3 free ports"), ("3 free ports", "at least 3 free ports"), (None, None)],
)
def test_a_port_limited_remainder_that_pays_no_split_still_blocks(measured, required):
    row = _partial_row(measured, required)
    entry = row["blocking_reasons"][0]
    entry.update(measured=measured, required=required)

    assert [r["code"] for r in _blocking.node_reasons(row)] == ["port_limited_remainder"]
    with _blocking.console.capture() as captured:
        print_not_eligible([row], short=False)
    assert "partial idle pay" not in captured.get().lower()


@pytest.mark.parametrize("gating", [True, False])
def test_a_port_limited_remainder_beside_another_zero_pay_reason_claims_no_pay(gating):
    # F-6: any other reason on the node excludes idle pay, so the paid-split count is not paid
    row = _partial_row()
    row["blocking_reasons"].append(
        {"code": "gpu_model_not_eligible_for_unrented_incentive", "kind": "idle_pay", "title": "GPU model earns no idle pay", "gating": gating}
    )

    assert "port_limited_remainder" in [r["code"] for r in _blocking.node_reasons(row)]
    with _blocking.console.capture() as captured:
        print_not_eligible([row], short=False)
    assert "partial idle pay" not in captured.get().lower()


def test_with_secure_listing_requiring_idle_pay_a_gating_remainder_still_blocks():
    # F-7: with SECURE_LISTING_REQUIRES_IDLE_PAY on, the served gating entry hides a Secure node
    row = {**_partial_row(), "secure_listing_requires_idle_pay": True}

    assert [r["code"] for r in _blocking.node_reasons(row)] == ["port_limited_remainder"]
    with _blocking.console.capture() as captured:
        print_not_eligible([row], short=False)
    assert "partial idle pay" not in captured.get().lower()
