"""Tests for ``ProviderClient.status`` aggregation (M2)."""

from __future__ import annotations

from typing import Any

from lium.provider.auth import LocalKeypairSigner
from lium.provider.client import ProviderClient
from lium.provider.errors import ProviderAuthError
from lium.provider.token_store import TokenStore


class _Portal:
    def __init__(
        self,
        *,
        me_body: dict | BaseException | None = None,
        executors: list | dict | BaseException | None = None,
    ) -> None:
        self._me_body = me_body
        self._executors = executors

    def get(self, path: str, *, params=None, auth=True):
        if path == "/auth/me":
            if isinstance(self._me_body, BaseException):
                raise self._me_body
            return self._me_body or {}
        if path == "/executors":
            if isinstance(self._executors, BaseException):
                raise self._executors
            return self._executors if isinstance(self._executors, dict) else {
                "data": self._executors or []
            }
        return {}

    def post(self, *a, **k) -> dict:  # pragma: no cover
        return {}

    def put(self, *a, **k) -> dict:  # pragma: no cover
        return {}

    def delete(self, *a, **k) -> dict:  # pragma: no cover
        return {}


class _StubMetagraph:
    def __init__(self, hotkeys: list[str], weights):
        self.hotkeys = hotkeys
        self.W = weights


def _stub_metagraph_factory(hotkeys, weights):
    def _factory(netuid: int):
        del netuid
        return _StubMetagraph(hotkeys, weights)

    return _factory


def _client(portal, token_store, signer) -> ProviderClient:
    return ProviderClient(
        signer=signer,
        token_store=token_store,
        http=portal,  # type: ignore[arg-type]
    )


def test_status_aggregates_portal_executors_and_metagraph(
    fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore
) -> None:
    portal = _Portal(
        me_body={"provider": {"id": "m-99"}},
        executors=[
            {"id": "exec-1", "gpu_type": "h100", "gpu_count": 1},
            {"id": "exec-2", "gpu_type": "rtx4090", "gpu_count": 8},
        ],
    )
    metagraph = _stub_metagraph_factory(
        hotkeys=["v1", fake_signer.ss58_address, "v3"],
        weights=[
            [0.0, 0.5, 0.0],
            [0.0, 0.0, 0.0],
            [0.0, 0.25, 0.0],
        ],
    )
    client = _client(portal, tmp_token_store, fake_signer)

    snapshot = client.status(metagraph_factory=metagraph)
    assert snapshot.portal_session_active is True
    assert snapshot.provider_id == "m-99"
    assert snapshot.node_count == 2
    assert {e.id for e in snapshot.nodes} == {"exec-1", "exec-2"}
    assert snapshot.registered_on_subnet is True
    weight_map = {row.validator_hotkey: row.weight for row in snapshot.validator_weights}
    assert weight_map == {"v1": 0.5, "v3": 0.25}


def test_status_extracts_provider_id_from_flat_whoami_shape(
    fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore
) -> None:
    """The portal's ``GET /auth/me`` returns a flat dict
    ``{provider_id, miner_hotkey, ...}`` (mirrors
    ``lium-miner-portal/src/routes/auth.py::_get_provider_resposne``).
    Earlier code looked for a nested ``provider.id`` and silently dropped it."""
    portal = _Portal(
        me_body={
            "provider_id": "flat-1",
            "miner_hotkey": fake_signer.ss58_address,
            "provider_coldkey": "5CK",
            "opt_in_status": True,
        },
        executors=[],
    )
    metagraph = _stub_metagraph_factory(hotkeys=[], weights=[])
    client = _client(portal, tmp_token_store, fake_signer)
    snapshot = client.status(metagraph_factory=metagraph)
    assert snapshot.portal_session_active is True
    assert snapshot.provider_id == "flat-1"


def test_status_surfaces_discord_incentive_eligibility(
    fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore
) -> None:
    portal = _Portal(
        me_body={
            "miner_id": "m-1",
            "miner_hotkey": fake_signer.ss58_address,
            "discord_id": "5477543105",
        },
        executors=[],
    )
    metagraph = _stub_metagraph_factory(hotkeys=[], weights=[])
    client = _client(portal, tmp_token_store, fake_signer)
    snapshot = client.status(metagraph_factory=metagraph)
    assert snapshot.provider_id == "m-1"
    assert snapshot.discord_connected is True
    assert snapshot.extra_incentive_eligible is True
    assert not any(w.startswith("discord:") for w in snapshot.warnings)


def test_status_warns_when_discord_missing(
    fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore
) -> None:
    portal = _Portal(
        me_body={
            "miner_id": "m-1",
            "miner_hotkey": fake_signer.ss58_address,
            "discord_id": None,
        },
        executors=[],
    )
    metagraph = _stub_metagraph_factory(hotkeys=[], weights=[])
    client = _client(portal, tmp_token_store, fake_signer)
    snapshot = client.status(metagraph_factory=metagraph)
    assert snapshot.discord_connected is False
    assert snapshot.extra_incentive_eligible is False
    assert "discord: not connected; extra incentives are disabled" in snapshot.warnings


def test_status_degrades_when_whoami_fails(
    fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore
) -> None:
    portal = _Portal(
        me_body=ProviderAuthError("token rejected"),
        executors=[],
    )
    metagraph = _stub_metagraph_factory(hotkeys=[], weights=[])
    client = _client(portal, tmp_token_store, fake_signer)
    snapshot = client.status(metagraph_factory=metagraph)
    assert snapshot.portal_session_active is False
    # Node list should not be queried when auth failed.
    assert snapshot.node_count is None
    assert any(w.startswith("whoami:") for w in snapshot.warnings)


def test_status_handles_missing_hotkey_in_metagraph(
    fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore
) -> None:
    portal = _Portal(me_body={"provider": {"id": "m-1"}}, executors=[])
    metagraph = _stub_metagraph_factory(hotkeys=["other1", "other2"], weights=[])
    client = _client(portal, tmp_token_store, fake_signer)
    snapshot = client.status(metagraph_factory=metagraph)
    assert snapshot.registered_on_subnet is False
    assert snapshot.validator_weights == []


def test_status_metagraph_unavailable_appends_warning(
    fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore
) -> None:
    portal = _Portal(me_body={"provider": {"id": "m-1"}}, executors=[])

    def _broken_factory(netuid: int) -> Any:
        raise RuntimeError("metagraph offline")

    client = _client(portal, tmp_token_store, fake_signer)
    snapshot = client.status(metagraph_factory=_broken_factory)
    assert snapshot.registered_on_subnet is None
    assert any("metagraph" in w for w in snapshot.warnings)
