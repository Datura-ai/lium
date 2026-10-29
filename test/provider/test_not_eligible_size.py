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


def _partial_row() -> dict:
    entry = {
        "code": "port_limited_remainder",
        "kind": "idle_pay",
        "title": "Too few free ports for the free GPUs",
        "message": "The partly rented node has 3 free ports for 7 free splits, one per split: idle pay for 3 of them only.",
        "fix": "Open more ports to the node.",
        "gating": False,
    }
    return {"id": "node-1", "blocking_reasons": [entry]}


@pytest.mark.parametrize(
    ("short", "expected"),
    [
        (False, "Partial idle pay: The partly rented node has 3 free ports for 7 free splits"),
        (True, "node-1: partial idle pay (too few free ports for the free GPUs); open more ports"),
    ],
)
def test_a_paying_port_limited_remainder_reads_partial_idle_pay(short, expected):
    with _blocking.console.capture() as captured:
        print_not_eligible([_partial_row()], short=short)

    printed = " ".join(captured.get().split())
    assert expected in printed
    assert "not eligible" not in printed.lower()
    assert "no action" not in printed.lower()
