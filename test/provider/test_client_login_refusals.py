"""Ticket-1343: a hotkey with no portal account that is not registered on the subnet is told so, not "re-login"."""

from __future__ import annotations

import pytest

from lium.provider.auth import LocalKeypairSigner
from lium.provider.errors import (
    HOTKEY_NOT_REGISTERED,
    PORTAL_AUTH_INVALID,
    ProviderAuthError,
)
from lium.provider.token_store import TokenStore

from .test_client_totp import _build_client, _FakePortal


@pytest.mark.parametrize(
    ("detail", "code"),
    [
        (
            {"miner": "Miner 5X not registered in Bittensor subnet 51"},
            HOTKEY_NOT_REGISTERED,
        ),
        ({"signature": "Invalid miner signature."}, PORTAL_AUTH_INVALID),
    ],
    ids=["not_registered", "bad_signature"],
)
def test_a_sign_in_401_says_which_refusal_it_was(
    fake_signer: LocalKeypairSigner,
    tmp_token_store: TokenStore,
    detail: dict,
    code: str,
) -> None:
    portal = _FakePortal()
    # what PortalHTTP raises for any 401, with the portal's body in its context
    portal.next_post_raises = ProviderAuthError(
        "portal rejected credentials",
        code=PORTAL_AUTH_INVALID,
        context={
            "url": "https://portal.example.com/auth/login-flexible",
            "status": 401,
            "body": {"detail": detail},
        },
    )
    client = _build_client(portal, tmp_token_store, fake_signer)

    with pytest.raises(ProviderAuthError) as excinfo:
        client.login(force=True)

    assert excinfo.value.code == code
    if code == HOTKEY_NOT_REGISTERED:
        assert "not registered" in str(excinfo.value)
