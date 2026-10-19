"""`lium mine --register <token>`: the node is posted from what the host reports, then watched until listed.

Origin: design/PROVIDER_BARRIER_PROGRAM.md §B.1 PR-1 (account → first node 34 min median, 20.5 min to listed with no
signal), reports/INVALID_EXECUTOR_ROOTCAUSE.md (hand-typed node values), DAH-3075 (SSH_PUBLIC_PORT left in .env).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import jwt as pyjwt
import pytest
from click.testing import CliRunner

from lium.cli.commands import mine
from lium.cli.commands import mine_register as reg
from lium.provider.portal_http import PortalHTTP

HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
POOL_HOTKEY = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
"""What a custodied account's nodes report under (lium-platform#294 ``LIUM_POOL_HOTKEYS[0]``)."""
ACCOUNT_ID = "acct_7f3c2a"
"""A custodied account's ``miner_hotkey``: an opaque id, not an SS58 value."""
NOW = 1_800_000_000


def _token(exp: int | None = NOW + 3600, hotkey: str | None = HOTKEY, opt_in: bool | None = True,
           node_hotkey: str | None = None, node_claim: str | None = None, **extra) -> str:
    # the portal's fastapi_jwt layout (lium-platform#291): the miner's to_json() plus "scope" under "subject", exp on top;
    # lium-platform#294 adds "node_hotkey" = the miner's listing_hotkey (its own key, or the pool's when custodied)
    subject = {"id": "acct-1", "opt_in_status": opt_in, "scope": "node:register"}
    if hotkey is not None:
        subject["miner_hotkey"] = hotkey
    if node_hotkey is not None:
        subject["node_hotkey"] = node_hotkey
    if node_claim is not None:
        subject["node_claim"] = node_claim
    claims = {"subject": subject, "type": "access", **extra}
    if exp is not None:
        claims["exp"] = exp
    return pyjwt.encode(claims, "not-the-portal-secret-but-long-enough-32b", algorithm="HS256")


_orig_confirm = reg.confirm_register_token


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


def test_parse_register_token_reads_the_node_claim_and_drops_a_non_hex_one() -> None:
    claim = "ab12" * 8
    assert reg.parse_register_token(_token(node_claim=claim), now=NOW).account_claim == claim
    assert reg.parse_register_token(_token(), now=NOW).account_claim is None
    # it is written into .env verbatim: a newline must never get there
    assert reg.parse_register_token(_token(node_claim="ab12\nSSH_PORT=1"), now=NOW).account_claim is None


def test_register_path_writes_the_account_claim_to_env(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("MINER_HOTKEY_SS58_ADDRESS=x\n")
    mine._apply_env_overrides(tmp_path, "8080", "8080", "2200", "", "", account_claim="ab12" * 8)
    assert "MINER_ACCOUNT_CLAIM=" + "ab12" * 8 in (tmp_path / ".env").read_text().splitlines()


def test_rerun_without_register_keeps_the_account_claim(tmp_path: Path) -> None:
    """A later `lium mine` without --register rebuilds .env from the template; the claim must survive it."""
    (tmp_path / ".env.template").write_text("MINER_HOTKEY_SS58_ADDRESS=\nMINER_ACCOUNT_CLAIM=\nSSH_PORT=2200\n")
    (tmp_path / ".env").write_text("MINER_HOTKEY_SS58_ADDRESS=x\nMINER_ACCOUNT_CLAIM=" + "ab12" * 8 + "\n")
    mine._setup_executor_env(tmp_path, hotkey=HOTKEY)
    mine._apply_env_overrides(tmp_path, "8080", "8080", "2200", "", "", account_claim=None)
    assert "MINER_ACCOUNT_CLAIM=" + "ab12" * 8 in (tmp_path / ".env").read_text().splitlines()
    # a template without the key still gets it
    (tmp_path / ".env.template").write_text("MINER_HOTKEY_SS58_ADDRESS=\n")
    mine._setup_executor_env(tmp_path, hotkey=HOTKEY)
    assert "MINER_ACCOUNT_CLAIM=" + "ab12" * 8 in (tmp_path / ".env").read_text().splitlines()


def test_recreate_waits_for_a_new_healthy_container(tmp_path: Path, monkeypatch) -> None:
    """After --force-recreate the old container can still read healthy; the wait must not return on it."""
    ps = iter(["old\n", "old\n", "new\n"])  # before up, then the old container lingers once, then the new one
    inspected = []

    def fake_run(cmd, check=True, capture=True, cwd=None):
        if "ps -q" in cmd:
            return next(ps), ""
        if cmd.startswith("docker inspect"):
            inspected.append(cmd.split()[-1])
            return "healthy", ""
        return "", ""

    monkeypatch.setattr(mine, "_run", fake_run)
    monkeypatch.setattr(mine.time, "sleep", lambda s: None)
    mine._start_executor(tmp_path, recreate=True)
    assert inspected == ["new"]


def test_parse_register_token_refuses_a_custodied_token_without_a_usable_node_identity() -> None:
    """Negative control: an opaque account id with no ``node_hotkey`` (or a non-SS58 one) cannot fill the .env."""
    for claim in (None, "", "0xnot-ss58"):
        with pytest.raises(reg.RegisterError, match="no usable node identity"):
            reg.parse_register_token(_token(hotkey=ACCOUNT_ID, node_hotkey=claim), now=NOW)


# --- host facts ------------------------------------------------------------------------------------------------------


# --- price -----------------------------------------------------------------------------------------------------------


# --- portal calls ----------------------------------------------------------------------------------------------------


class _Resp:
    def __init__(self, status: int, body) -> None:
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _Portal:
    """A scripted portal: every request is recorded; responses come from a per-(method, path) queue."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.queues: dict[tuple[str, str], list[_Resp]] = {}

    def on(self, method: str, path: str, *responses: _Resp) -> None:
        self.queues.setdefault((method, path), []).extend(responses)

    def request(self, **kw):
        self.calls.append(kw)
        path = kw["url"].split("https://portal.example", 1)[1]
        queue = self.queues[(kw["method"], path)]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def _http(portal: _Portal) -> PortalHTTP:
    return PortalHTTP(base_url="https://portal.example", token_provider=lambda: "TOKEN", session=portal)  # type: ignore[arg-type]


# --- the command -----------------------------------------------------------------------------------------------------


_PROBE = "/executors/00000000-0000-0000-0000-000000000000"


@pytest.mark.parametrize("resp, refused", [
    (_Resp(404, {"detail": "Not found node"}), None),
    (_Resp(401, {"detail": "Invalid or expired token"}), "The portal refused the register token (Invalid or expired token)"),
    (_Resp(404, {"detail": "Not Found"}), "did not answer as the portal"),
    (_Resp(200, {"id": "x"}), "did not answer as the portal"),
])
def test_confirm_register_token_asks_the_portal_with_the_token(resp, refused) -> None:
    portal = _Portal()
    portal.on("GET", _PROBE, resp)
    if refused is None:
        _orig_confirm(_http(portal))
    else:
        with pytest.raises(reg.RegisterError, match=re.escape(refused)):
            _orig_confirm(_http(portal))
    assert portal.calls[0]["headers"]["Authorization"] == "Bearer TOKEN"


def test_mine_register_refuses_a_token_the_portal_rejects_before_touching_the_host(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(mine, "_clone_or_update_repo", lambda *a, **k: calls.append("clone"))
    monkeypatch.setattr(reg, "confirm_register_token", _orig_confirm)
    portal = _Portal()
    portal.on("GET", _PROBE, _Resp(401, {"detail": "Invalid or expired token"}))
    monkeypatch.setattr(reg, "build_http", lambda url, token: _http(portal))
    result = CliRunner().invoke(mine.mine_command, ["--register", _token(exp=int(time.time()) + 3600)])
    assert result.exit_code == 1 and "refused the register token" in result.output and calls == []
