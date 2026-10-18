"""`lium mine --register <token>`: the node is posted from what the host reports, then watched until listed.

Origin: design/PROVIDER_BARRIER_PROGRAM.md §B.1 PR-1 (account → first node 34 min median, 20.5 min to listed with no
signal), reports/INVALID_EXECUTOR_ROOTCAUSE.md (hand-typed node values), DAH-3075 (SSH_PUBLIC_PORT left in .env).
"""

from __future__ import annotations


import jwt as pyjwt
import pytest

from lium.cli.commands import mine_register as reg

HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
POOL_HOTKEY = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
"""What a custodied account's nodes report under (lium-platform#294 ``LIUM_POOL_HOTKEYS[0]``)."""
ACCOUNT_ID = "acct_7f3c2a"
"""A custodied account's ``miner_hotkey``: an opaque id, not an SS58 value."""
NOW = 1_800_000_000


def _token(exp: int | None = NOW + 3600, hotkey: str | None = HOTKEY, opt_in: bool | None = True,
           node_hotkey: str | None = None, **extra) -> str:
    # the portal's fastapi_jwt layout (lium-platform#291): the miner's to_json() plus "scope" under "subject", exp on top;
    # lium-platform#294 adds "node_hotkey" = the miner's listing_hotkey (its own key, or the pool's when custodied)
    subject = {"id": "acct-1", "opt_in_status": opt_in, "scope": "node:register"}
    if hotkey is not None:
        subject["miner_hotkey"] = hotkey
    if node_hotkey is not None:
        subject["node_hotkey"] = node_hotkey
    claims = {"subject": subject, "type": "access", **extra}
    if exp is not None:
        claims["exp"] = exp
    return pyjwt.encode(claims, "not-the-portal-secret-but-long-enough-32b", algorithm="HS256")


# --- token -----------------------------------------------------------------------------------------------------------


def test_parse_register_token_reads_account_expiry_and_opt_in() -> None:
    t = reg.parse_register_token(_token(), now=NOW)
    assert (t.miner_hotkey, t.exp, t.opt_in_status) == (HOTKEY, NOW + 3600, True)
    assert t.seconds_left(now=NOW) == 3600


def test_parse_register_token_refuses_an_expired_token_before_any_install() -> None:
    with pytest.raises(reg.RegisterError, match="expired at .*Add Node page"):
        reg.parse_register_token(_token(exp=NOW - 1), now=NOW)


def test_parse_register_token_refuses_garbage_and_a_token_without_an_account() -> None:
    with pytest.raises(reg.RegisterError, match="not one the portal issued"):
        reg.parse_register_token("not.a.jwt", now=NOW)
    with pytest.raises(reg.RegisterError, match="names no account"):
        reg.parse_register_token(_token(hotkey=None), now=NOW)
    with pytest.raises(reg.RegisterError, match="names no account"):
        reg.parse_register_token(_token(hotkey=""), now=NOW)
    # an own-key token whose account value is not SS58: nothing usable for the executor's .env
    with pytest.raises(reg.RegisterError, match="no usable node identity"):
        reg.parse_register_token(_token(hotkey="0xnot-ss58"), now=NOW)


def test_parse_register_token_accepts_flat_claims_and_no_expiry() -> None:
    flat = pyjwt.encode({"miner_hotkey": HOTKEY}, "k" * 32, algorithm="HS256")
    t = reg.parse_register_token(flat, now=NOW)
    assert t.miner_hotkey == HOTKEY and t.exp is None and t.seconds_left() is None and t.opt_in_status is None


def test_parse_register_token_node_identity_is_the_account_key_unless_the_token_names_another() -> None:
    """lium-platform#294: ``node_hotkey`` is what the executor reports under. An own-key token (with or without
    the claim) keeps the account's key; a custodied token pairs an opaque account id with the pool's key."""
    own_without_claim = reg.parse_register_token(_token(), now=NOW)
    own_with_claim = reg.parse_register_token(_token(node_hotkey=HOTKEY), now=NOW)
    assert (own_without_claim.miner_hotkey, own_without_claim.node_hotkey) == (HOTKEY, HOTKEY)
    assert (own_with_claim.miner_hotkey, own_with_claim.node_hotkey) == (HOTKEY, HOTKEY)
    custodied = reg.parse_register_token(_token(hotkey=ACCOUNT_ID, node_hotkey=POOL_HOTKEY), now=NOW)
    assert (custodied.miner_hotkey, custodied.node_hotkey) == (ACCOUNT_ID, POOL_HOTKEY)


def test_parse_register_token_refuses_a_custodied_token_without_a_usable_node_identity() -> None:
    """Negative control: an opaque account id with no ``node_hotkey`` (or a non-SS58 one) cannot fill the .env."""
    for claim in (None, "", "0xnot-ss58"):
        with pytest.raises(reg.RegisterError, match="no usable node identity"):
            reg.parse_register_token(_token(hotkey=ACCOUNT_ID, node_hotkey=claim), now=NOW)


# --- host facts ------------------------------------------------------------------------------------------------------














# --- price -----------------------------------------------------------------------------------------------------------








# --- portal calls ----------------------------------------------------------------------------------------------------




















































# --- the command -----------------------------------------------------------------------------------------------------


_TEMPLATE = "MINER_HOTKEY_SS58_ADDRESS=\nINTERNAL_PORT=8080\nEXTERNAL_PORT=8080\nSSH_PORT=2200\nSSH_PUBLIC_PORT=2200\n"


















_orig_wait = reg.wait_until_listed








