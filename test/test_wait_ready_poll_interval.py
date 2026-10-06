"""DAH-3002: ``wait_ready`` polls every 2 s while a normal start is still plausible.

The backend marks a cached-template pod RUNNING at p50 22.5 s after the rent (7 d to 6 Sep
2026); polled every 10 s, ``lium up`` and ``Lium.wait_ready`` reported it 0–10 s late.
"""

from __future__ import annotations

import pytest

from lium.cli.utils import wait_for_pod_ready
from lium.sdk import Lium

from test_pod_start_error import _Client, _pod


@pytest.fixture
def sleeps(monkeypatch):
    recorded: list[float] = []
    monkeypatch.setattr("lium.sdk.client.time.sleep", recorded.append)
    return recorded


def test_poll_delay_is_fast_first_then_slow():
    assert Lium.poll_delay(0) == Lium.FAST_POLL_SECONDS == 2
    assert Lium.poll_delay(89.9) == 2
    assert Lium.poll_delay(90) == Lium.SLOW_POLL_SECONDS == 10
    assert Lium.poll_delay(600) == 10


def test_poll_delay_honours_a_fixed_interval():
    assert Lium.poll_delay(0, poll_interval=7) == 7
    assert Lium.poll_delay(600, poll_interval=1) == 1


def test_wait_ready_sleeps_two_seconds_between_early_polls(sleeps, monkeypatch):
    clock = iter([0, 0, 2, 4])
    monkeypatch.setattr("lium.sdk.client.time.time", lambda: next(clock))
    client = _Client([[_pod("PENDING", None)], [_pod("PENDING", None)], [_pod("RUNNING")]])

    ready = client.wait_ready("pod-1", timeout=300)

    assert ready.status == "RUNNING"
    assert sleeps == [2, 2]


def test_wait_ready_backs_off_to_ten_seconds_after_the_fast_window(sleeps, monkeypatch):
    clock = iter([0, 0, 30, 95, 200])
    monkeypatch.setattr("lium.sdk.client.time.time", lambda: next(clock))
    client = _Client([[_pod("PENDING", None)]] * 3 + [[_pod("RUNNING")]])

    client.wait_ready("pod-1", timeout=1000)

    assert sleeps == [2, 2, 10]


def test_wait_ready_uses_the_fast_schedule_for_a_pod_not_yet_listed(sleeps, monkeypatch):
    clock = iter([0, 0, 2])
    monkeypatch.setattr("lium.sdk.client.time.time", lambda: next(clock))
    client = _Client([[], [_pod("RUNNING")]])

    client.wait_ready("pod-1", timeout=300)

    assert sleeps == [2]


def test_explicit_poll_interval_is_still_a_fixed_interval(sleeps):
    client = _Client([[_pod("PENDING", None)], [_pod("RUNNING")]])

    client.wait_ready("pod-1", timeout=300, poll_interval=5)

    assert sleeps == [5]


def test_cli_wait_uses_the_adaptive_schedule():
    seen = {}

    class _Recording:
        def wait_ready(self, pod_id, *, timeout, poll_interval=None, on_poll=None):
            seen.update(pod_id=pod_id, timeout=timeout, poll_interval=poll_interval)
            return _pod("RUNNING")

    assert wait_for_pod_ready(_Recording(), "pod-1").status == "RUNNING"
    assert seen == {"pod_id": "pod-1", "timeout": None, "poll_interval": None}
