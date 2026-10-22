"""DAH-4993: a 429 during `lium up`'s readiness wait slows the wait down instead of ending it.

Five parallel `lium up` from one IP (dogfood 22 Oct 2026) got 429 on the wait's `GET /pods` past the
SDK's three retries and exited `rate_limited` with the pod billing, no Jupyter and no GPU check.
"""

from __future__ import annotations

from types import SimpleNamespace

from lium.cli.actions import ActionResult
from lium.cli.up import actions as up_actions
from lium.cli.utils import EXIT_GENERAL_ERROR
from lium.sdk import LiumRateLimitError
from lium.sdk.client import Lium

from test_up_progress_timeout import _Client, _flat, _pod, _run_up


class _Throttled(_Client):
    """`ps` raises 429 ``throttled`` times (forever with None), then replays the sequence."""

    def __init__(self, sequence, throttled, retry_after=None):
        super().__init__(sequence)
        self.throttled = throttled
        self.retry_after = retry_after

    def ps(self):
        if self.throttled is None or self.throttled > 0:
            if self.throttled:
                self.throttled -= 1
            exc = LiumRateLimitError("Rate limit exceeded")
            exc.retry_after = self.retry_after
            raise exc
        return super().ps()


def _wait_with(client):
    class _Wait(up_actions.WaitReadyAction):
        def execute(self, ctx):
            return super().execute({**ctx, "lium": client})
    return _Wait


def test_two_429s_during_the_wait_slow_it_down_and_up_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr("lium.sdk.client.time.sleep", sleeps.append)
    client = _Throttled([[_pod("RUNNING")]], throttled=2, retry_after=7)

    result = _run_up(monkeypatch, wait_action=_wait_with(client), args=["--timeout", "600"])

    assert result.exit_code == 0, result.output
    assert sleeps == [7, 7]  # Retry-After, not the 1 s poll


def test_a_429_that_outlasts_the_budget_still_names_the_billing_pod(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr("lium.sdk.client.time.time", lambda: clock["now"])
    monkeypatch.setattr("lium.sdk.client.time.sleep", lambda s: clock.__setitem__("now", clock["now"] + s))
    client = _Throttled([[_pod("RUNNING")]], throttled=None, retry_after=30)

    result = _run_up(monkeypatch, wait_action=_wait_with(client), args=["--timeout", "120"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "Pod train (id: pod-1) is still starting after" in _flat(result.output)
    assert "lium rm train" in _flat(result.output)


def test_retry_after_is_read_from_the_429_response():
    resp = SimpleNamespace(ok=False, status_code=429, headers={"Retry-After": "12"}, json=lambda: {}, text="")
    try:
        Lium._raise_for_status(resp)
    except LiumRateLimitError as exc:
        assert exc.retry_after == 12.0
    else:
        raise AssertionError("no LiumRateLimitError")
