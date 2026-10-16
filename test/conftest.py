import pytest


@pytest.fixture(autouse=True)
def _no_persistent_ssh(monkeypatch):
    """`lium exec` starts a background OpenSSH master; a test opts in with LIUM_SSH_PERSIST."""
    monkeypatch.setenv("LIUM_SSH_PERSIST", "0")
