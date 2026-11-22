"""SDK tests for extended ``ProviderClient`` methods.

Covers methods added to drive the full provider-portal workflow from the
CLI/agent side:

- profile / config (set-email, opt-in/out, dashboard)
- executor lifecycle (add, delete, update-price, update-gpu, min-gpu,
  notice-period, notify-added)
- batch sync (from/to miner server)
- read-only queries (validators, billing, machines, machine-requests)
"""

from __future__ import annotations

from typing import Any

import pytest

from lium.provider.client import ProviderClient


class _Portal:
    def __init__(self, *, get_body=None, post_body=None, delete_body=None):
        self._get_body = get_body
        self._post_body = post_body
        self._delete_body = delete_body
        self.posts: list[Any] = []
        self.gets: list[Any] = []
        self.deletes: list[Any] = []

    def get(self, path, *, params=None, auth=True):
        self.gets.append((path, params, auth))
        return self._get_body if self._get_body is not None else {}

    def post(self, path, *, json_body=None, auth=True):
        self.posts.append((path, json_body, auth))
        return self._post_body if self._post_body is not None else {}

    def put(self, *a, **k):  # pragma: no cover
        return {}

    def delete(self, path, *, auth=True):
        self.deletes.append((path, auth))
        return self._delete_body if self._delete_body is not None else {}


@pytest.fixture
def client(fake_signer, tmp_token_store):
    def _build(portal: _Portal) -> ProviderClient:
        return ProviderClient(
            signer=fake_signer,
            token_store=tmp_token_store,
            http=portal,  # type: ignore[arg-type]
        )

    return _build


# ---------------------------------------------------------------------------
# Profile / config


# ---------------------------------------------------------------------------
# Node lifecycle


# ---------------------------------------------------------------------------
# Batch sync
#
# SDK method names match the user-facing CLI verbs. ``sync_nodes_from_
# miner_server`` POSTs ``/executors/sync-executor-central-miner``, which
# is the route the "Sync From Miner Server" button hits in the frontend.
# Mapping is documented at the top of ``client.py``.


# ---------------------------------------------------------------------------
# Read-only queries


def test_billing_history_reads_the_signed_in_providers_daily_ledger(client) -> None:
    portal = _Portal(get_body={"data": {"rows": []}})
    c = client(portal)
    c.billing_history()
    c.billing_history(start_day="2026-10-01", end_day="2026-10-17")
    assert [g[:2] for g in portal.gets] == [
        ("/provider-ledger/daily", None),
        ("/provider-ledger/daily", {"from": "2026-10-01", "to": "2026-10-17"}),
    ]


# ---------------------------------------------------------------------------
# Path-segment validation


# --- node listing scope ------------------------------------------------------




def test_a_wallet_that_does_not_load_is_reported_before_any_portal_request(monkeypatch, tmp_token_store):
    """A lium that cannot read the hotkey file sent its request without a token, and the portal's 401 then said
    "token rejected, re-login", although a login from another lium had just worked."""
    from lium.provider import wallet
    from lium.provider.errors import WALLET_NOT_FOUND, ProviderError

    def unreadable(coldkey, hotkey):
        raise ProviderError("could not open wallet", code=WALLET_NOT_FOUND)

    monkeypatch.setattr(wallet, "load_hotkey_keypair", unreadable)
    sent = []
    client = ProviderClient("default", "default", token_store=tmp_token_store)
    monkeypatch.setattr(client._http._session, "request", lambda **kw: sent.append(kw))

    with pytest.raises(ProviderError) as e:
        client.create_discord_oauth_authorization_url()

    assert e.value.code == WALLET_NOT_FOUND
    assert sent == []
