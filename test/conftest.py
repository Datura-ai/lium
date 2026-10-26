import sys
import time

import pytest


@pytest.fixture(autouse=True)
def _no_persistent_ssh(monkeypatch):
    """`lium exec` starts a background OpenSSH master; a test opts in with LIUM_SSH_PERSIST."""
    monkeypatch.setenv("LIUM_SSH_PERSIST", "0")


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    """`with_retry` (lium/sdk/utils.py) backs off 1-3.5 s of real sleep per failed attempt: a third of CI's pytest time.

    Only a sleep called from lium.sdk.utils is skipped; every other caller sleeps for real, and a test that patches
    `time.sleep` itself still replaces this wrapper as before.
    """
    real_sleep = time.sleep

    def sleep(seconds):
        if sys._getframe(1).f_globals.get("__name__") != "lium.sdk.utils":
            real_sleep(seconds)

    monkeypatch.setattr(time, "sleep", sleep)
