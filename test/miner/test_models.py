"""Tests for hand-written portal DTOs and ``NodePorts.from_string`` (A11)."""

from __future__ import annotations

import pytest

from lium.miner.errors import PORTS_INVALID, MinerError
from lium.miner.models import (
    AddExecutorPayload,
    LoginResponse,
    NodePorts,
    SafeMinerResponse,
)


def test_login_response_round_trips_minimal_body() -> None:
    body = {
        "miner": {
            "id": "m1",
            "miner_hotkey": "5HK",
            "miner_coldkey": "5CK",
            "created_at": "2026-05-06T00:00:00",
            "updated_at": "2026-05-06T00:00:00",
        },
        "token": "jwt.body.sig",
    }
    parsed = LoginResponse.model_validate(body)
    assert parsed.token == "jwt.body.sig"
    assert isinstance(parsed.miner, SafeMinerResponse)
    assert parsed.miner.machine_request_subscription == []  # default factory


def test_add_executor_payload_required_fields() -> None:
    payload = AddExecutorPayload(
        gpu_type="H100",
        ip_address="1.2.3.4",
        port=2200,
        price_per_gpu=2.5,
        gpu_count=8,
    )
    assert payload.model_dump()["gpu_count"] == 8


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, NodePorts()),
        ("", NodePorts()),
        (
            "HTTP=8080,SSH=2200,RANGE=2000-2005",
            NodePorts(http=8080, ssh=2200, range_lo=2000, range_hi=2005),
        ),
        (
            "http=80,ssh=22,range=1000-1100",
            NodePorts(http=80, ssh=22, range_lo=1000, range_hi=1100),
        ),
        ("HTTP=9000", NodePorts(http=9000)),
        # A single key keeps other defaults
        ("RANGE=5000-5500", NodePorts(range_lo=5000, range_hi=5500)),
    ],
)
def test_node_ports_from_string_valid(raw: str | None, expected: NodePorts) -> None:
    parsed = NodePorts.from_string(raw)
    assert parsed == expected


@pytest.mark.parametrize(
    "raw",
    [
        "garbage",
        "HTTP=abc",
        "HTTP=-1",
        "HTTP=70000",
        "RANGE=2000",  # missing -hi
        "RANGE=5000-4000",  # lo > hi
        "UNKNOWN=1",
    ],
)
def test_node_ports_from_string_invalid(raw: str) -> None:
    with pytest.raises(MinerError) as exc:
        NodePorts.from_string(raw)
    assert exc.value.code == PORTS_INVALID


def test_node_ports_to_install_args() -> None:
    args = NodePorts(
        http=8080, ssh=2200, range_lo=2000, range_hi=2005
    ).to_install_args()
    assert args == [
        "--http-port",
        "8080",
        "--ssh-port",
        "2200",
        "--port-range",
        "2000-2005",
    ]
