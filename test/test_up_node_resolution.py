"""DAH-2930 — `lium up <node>` resolves the HUID `lium ls` prints, and says what it looked up when
nothing matches.

`Lium.get_executor()` compared only the UUID, so the HUID `ls` shows as a node's identifier (and that
`up --help` documents as accepted) never resolved: "Node 'cosmic-hawk-f2' not found" for a node listed a
second earlier.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.up import command as up_command
from lium.cli.up.actions import ResolveExecutorAction
from lium.cli.utils import EXIT_GENERAL_ERROR
from lium.sdk import Config, Lium
from lium.sdk.utils import generate_huid

NODE_ID = "6751b3b1-0d2a-4b0e-9a5f-3c2f1e8d7a60"
OTHER_ID = "0e1d2c3b-4a59-4687-9fa0-b1c2d3e4f5a6"


def _listed(node_id: str) -> SimpleNamespace:
    return SimpleNamespace(id=node_id, huid=generate_huid(node_id), gpu_count=1, gpu_type="H100", available_port_count=5)


def _client(monkeypatch, listing: list) -> Lium:
    client = Lium(Config(api_key="test"))
    monkeypatch.setattr(client, "ls", lambda **kwargs: listing)
    return client


# --------------------------------------------------------------------------- #
# SDK
# --------------------------------------------------------------------------- #


def test_get_executor_resolves_the_huid_ls_prints(monkeypatch):
    # Arrange
    client = _client(monkeypatch, [_listed(OTHER_ID), _listed(NODE_ID)])

    # Act
    found = client.get_executor(generate_huid(NODE_ID))

    # Assert
    assert found is not None
    assert found.id == NODE_ID


def test_get_executor_still_resolves_the_uuid(monkeypatch):
    client = _client(monkeypatch, [_listed(NODE_ID)])

    assert client.get_executor(NODE_ID).id == NODE_ID


def test_get_executor_refuses_a_huid_shared_by_two_nodes(monkeypatch):
    """A HUID is a 25,600-value draw; two listed nodes can share one. The first in API order must
    not win silently under `lium up <huid> --yes`."""
    twin_a, twin_b = _listed(NODE_ID), _listed(OTHER_ID)
    twin_b.huid = twin_a.huid
    client = _client(monkeypatch, [twin_a, twin_b])

    with pytest.raises(ValueError) as collision:
        client.get_executor(twin_a.huid)

    message = str(collision.value)
    assert "matches 2 listed nodes" in message and NODE_ID in message and OTHER_ID in message
    assert "lium ls --format json" in message


def test_get_executor_uuid_wins_over_a_huid_collision(monkeypatch):
    """An exact UUID is unambiguous even when another node's HUID happens to equal it."""
    twin_a, twin_b = _listed(NODE_ID), _listed(OTHER_ID)
    twin_b.huid = NODE_ID
    client = _client(monkeypatch, [twin_b, twin_a])

    assert client.get_executor(NODE_ID).id == NODE_ID


def test_resolve_executor_action_reports_a_huid_collision(monkeypatch):
    twin_a, twin_b = _listed(NODE_ID), _listed(OTHER_ID)
    twin_b.huid = twin_a.huid
    client = _client(monkeypatch, [twin_a, twin_b])

    result = ResolveExecutorAction().execute({"lium": client, "executor_id": twin_a.huid})

    assert result.ok is False
    assert "matches 2 listed nodes" in result.error


def test_get_executor_returns_none_for_an_unlisted_id(monkeypatch):
    client = _client(monkeypatch, [_listed(NODE_ID)])

    assert client.get_executor("cosmic-hawk-f2") is None


def test_up_names_the_id_it_looked_up_and_points_at_ls_json(monkeypatch):
    # Arrange
    client = _client(monkeypatch, [_listed(NODE_ID)])

    # Act / Assert
    with pytest.raises(ValueError) as failure:
        client.up(executor_id="cosmic-hawk-f2", ssh_keys=["ssh-ed25519 AAA"])
    assert "Node 'cosmic-hawk-f2' is not in the current listing" in str(failure.value)
    assert "lium ls --format json" in str(failure.value)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_resolve_executor_action_accepts_a_huid(monkeypatch):
    client = _client(monkeypatch, [_listed(NODE_ID)])

    result = ResolveExecutorAction().execute({"lium": client, "executor_id": generate_huid(NODE_ID)})

    assert result.ok is True
    assert result.data["executor"].id == NODE_ID


def test_resolve_executor_action_error_names_the_id_and_the_next_command(monkeypatch):
    client = _client(monkeypatch, [_listed(NODE_ID)])

    result = ResolveExecutorAction().execute({"lium": client, "executor_id": "cosmic-hawk-f2"})

    assert result.ok is False
    assert "Node 'cosmic-hawk-f2' is not in the current listing (looked up by UUID and HUID)" in result.error
    assert "lium ls --format json" in result.error


def test_up_with_an_unlisted_huid_fails_fast_with_the_lookup_in_the_message(monkeypatch):
    # Arrange — a configured CLI whose listing does not contain the requested node
    monkeypatch.setattr(up_command, "ensure_config", lambda: None)
    monkeypatch.setattr(up_command, "Lium", lambda **kwargs: _client(monkeypatch, [_listed(NODE_ID)]))

    # Act
    result = CliRunner().invoke(up_command.up_command, ["cosmic-hawk-f2", "--yes", "--no-ssh"])

    # Assert — the console wraps at 80 columns, so compare on one line
    output = " ".join(result.output.split())
    assert result.exit_code == EXIT_GENERAL_ERROR, result.output
    assert "Node 'cosmic-hawk-f2' is not in the current listing" in output
    assert "lium ls --format json" in output
