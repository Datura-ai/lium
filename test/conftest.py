import pytest


@pytest.fixture(autouse=True)
def _no_register_token_env(monkeypatch) -> None:
    """A register token exported in the shell running the tests turns every plain `lium mine` into a registration."""
    monkeypatch.delenv("LIUM_REGISTER_TOKEN", raising=False)
