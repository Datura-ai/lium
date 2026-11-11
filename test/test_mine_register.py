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


def _token(
    exp: int | None = NOW + 3600,
    hotkey: str | None = HOTKEY,
    opt_in: bool | None = True,
    node_hotkey: str | None = None,
    node_claim: str | None = None,
    **extra,
) -> str:
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
    return pyjwt.encode(
        claims, "not-the-portal-secret-but-long-enough-32b", algorithm="HS256"
    )


_orig_confirm = reg.confirm_register_token


@pytest.fixture(autouse=True)
def _portal_accepts_the_token(monkeypatch):
    """The command's portal check of the token; the test of it below calls the real one."""
    monkeypatch.setattr(reg, "confirm_register_token", lambda http: None)


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
    assert (
        t.miner_hotkey == HOTKEY
        and t.exp is None
        and t.seconds_left() is None
        and t.opt_in_status is None
    )


def test_parse_register_token_node_identity_is_the_account_key_unless_the_token_names_another() -> (
    None
):
    """lium-platform#294: ``node_hotkey`` is what the executor reports under. An own-key token (with or without
    the claim) keeps the account's key; a custodied token pairs an opaque account id with the pool's key."""
    own_without_claim = reg.parse_register_token(_token(), now=NOW)
    own_with_claim = reg.parse_register_token(_token(node_hotkey=HOTKEY), now=NOW)
    assert (own_without_claim.miner_hotkey, own_without_claim.node_hotkey) == (
        HOTKEY,
        HOTKEY,
    )
    assert (own_with_claim.miner_hotkey, own_with_claim.node_hotkey) == (HOTKEY, HOTKEY)
    custodied = reg.parse_register_token(
        _token(hotkey=ACCOUNT_ID, node_hotkey=POOL_HOTKEY), now=NOW
    )
    assert (custodied.miner_hotkey, custodied.node_hotkey) == (ACCOUNT_ID, POOL_HOTKEY)


def test_parse_register_token_reads_the_node_claim_and_drops_a_non_hex_one() -> None:
    claim = "ab12" * 8
    assert (
        reg.parse_register_token(_token(node_claim=claim), now=NOW).account_claim
        == claim
    )
    assert reg.parse_register_token(_token(), now=NOW).account_claim is None
    # it is written into .env verbatim: a newline must never get there
    assert (
        reg.parse_register_token(
            _token(node_claim="ab12\nSSH_PORT=1"), now=NOW
        ).account_claim
        is None
    )


def test_register_path_writes_the_account_claim_to_env(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("MINER_HOTKEY_SS58_ADDRESS=x\n")
    mine._apply_env_overrides(
        tmp_path, "8080", "8080", "2200", "", "", account_claim="ab12" * 8
    )
    assert (
        "MINER_ACCOUNT_CLAIM=" + "ab12" * 8
        in (tmp_path / ".env").read_text().splitlines()
    )


def test_rerun_without_register_keeps_the_account_claim(tmp_path: Path) -> None:
    """A later `lium mine` without --register rebuilds .env from the template; the claim must survive it."""
    (tmp_path / ".env.template").write_text(
        "MINER_HOTKEY_SS58_ADDRESS=\nMINER_ACCOUNT_CLAIM=\nSSH_PORT=2200\n"
    )
    (tmp_path / ".env").write_text(
        "MINER_HOTKEY_SS58_ADDRESS=x\nMINER_ACCOUNT_CLAIM=" + "ab12" * 8 + "\n"
    )
    mine._setup_executor_env(tmp_path, hotkey=HOTKEY)
    mine._apply_env_overrides(
        tmp_path, "8080", "8080", "2200", "", "", account_claim=None
    )
    assert (
        "MINER_ACCOUNT_CLAIM=" + "ab12" * 8
        in (tmp_path / ".env").read_text().splitlines()
    )
    # a template without the key still gets it
    (tmp_path / ".env.template").write_text("MINER_HOTKEY_SS58_ADDRESS=\n")
    mine._setup_executor_env(tmp_path, hotkey=HOTKEY)
    assert (
        "MINER_ACCOUNT_CLAIM=" + "ab12" * 8
        in (tmp_path / ".env").read_text().splitlines()
    )


def test_recreate_waits_for_a_new_healthy_container(
    tmp_path: Path, monkeypatch
) -> None:
    """After --force-recreate the old container can still read healthy; the wait must not return on it."""
    ps = iter(
        ["old\n", "old\n", "new\n"]
    )  # before up, then the old container lingers once, then the new one
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


def test_parse_register_token_refuses_a_custodied_token_without_a_usable_node_identity() -> (
    None
):
    """Negative control: an opaque account id with no ``node_hotkey`` (or a non-SS58 one) cannot fill the .env."""
    for claim in (None, "", "0xnot-ss58"):
        with pytest.raises(reg.RegisterError, match="no usable node identity"):
            reg.parse_register_token(
                _token(hotkey=ACCOUNT_ID, node_hotkey=claim), now=NOW
            )


# --- host facts ------------------------------------------------------------------------------------------------------


# --- price -----------------------------------------------------------------------------------------------------------


class _Snapshot:
    machine_prices = {
        "NVIDIA L4": 0.11,
        "NVIDIA A10 Tensor Core GPU": 0.2,
        "NVIDIA GeForce RTX 4090": 0.3,
    }


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
    return PortalHTTP(
        base_url="https://portal.example",
        token_provider=lambda: "TOKEN",
        session=portal,
    )  # type: ignore[arg-type]


def _listing(
    node_id: str = "node-1", ip: str = "203.0.113.7", port: int = 8080
) -> _Resp:
    return _Resp(
        200,
        {
            "success": True,
            "data": [
                {
                    "id": node_id,
                    "executor_ip_address": ip,
                    "executor_ip_port": str(port),
                },
            ],
            "total": 1,
        },
    )


def test_register_node_posts_and_finds_the_id_with_the_token() -> None:
    portal = _Portal()
    portal.on(
        "POST",
        "/executors",
        _Resp(200, {"success": True, "data": {"message": "queued"}}),
    )
    portal.on("GET", "/executors", _listing())
    record = reg.register_node(
        _http(portal),
        miner_hotkey=HOTKEY,
        gpu_type="NVIDIA L4",
        gpu_count=1,
        ip_address="203.0.113.7",
        port=8080,
        price_per_gpu=0.11,
    )
    assert record == reg.NodeRecord(node_id="node-1", already_registered=False)
    post, get = portal.calls
    assert post["headers"]["Authorization"] == "Bearer TOKEN"
    assert post["json"] == {
        "gpu_type": "NVIDIA L4",
        "ip_address": "203.0.113.7",
        "port": 8080,
        "price_per_gpu": 0.11,
        "gpu_count": 1,
    }
    assert (
        get["headers"]["Authorization"] == "Bearer TOKEN"
    )  # the portal shows the address to the owner only
    assert get["params"] == {"miner_hotkey": HOTKEY, "page": 1, "limit": 100}


def _status(status: str, message: str = "", last_error: dict | None = None) -> _Resp:
    computed = {"status": status, "message": message, "last_error": last_error}
    return _Resp(200, {"id": "node-1", "computed_status": computed})


# --- the command -----------------------------------------------------------------------------------------------------


_TEMPLATE = "MINER_HOTKEY_SS58_ADDRESS=\nINTERNAL_PORT=8080\nEXTERNAL_PORT=8080\nSSH_PORT=2200\nSSH_PUBLIC_PORT=2200\n"


def _stub_host(
    monkeypatch, tmp_path: Path, *, nvidia_smi: str = "NVIDIA L4, 23034\n"
) -> tuple[Path, Path]:
    """Steps 1–6 with every host action stubbed: the clone drops an executor ``.env.template``, install/prereqs/start/
    validate/port checks are no-ops, ``nvidia-smi`` answers ``nvidia_smi``, the public IP is TEST-NET. Returns
    ``(target, executor_dir)``; the portal is the caller's."""
    target = tmp_path / "compute-subnet"
    executor_dir = target / "neurons" / "executor"

    def fake_clone(target_dir: Path, branch: str) -> None:
        executor_dir.mkdir(parents=True, exist_ok=True)
        (executor_dir / ".env.template").write_text(_TEMPLATE)

    monkeypatch.setattr(mine, "_clone_or_update_repo", fake_clone)
    for name in (
        "_install_executor_tools",
        "_check_prereqs",
        "_check_free_disk",
        "_start_executor",
        "_validate_executor",
        "_check_ports_free",
    ):
        monkeypatch.setattr(mine, name, lambda *a, **k: None)
    monkeypatch.setattr(mine, "_docker_storage_limited", lambda: True)

    class _Pull:
        def poll(self):
            return 0

        def wait(self):
            return 0

    monkeypatch.setattr(mine, "_start_preflight_pull", lambda: _Pull())
    monkeypatch.setattr(
        mine,
        "_run",
        lambda cmd, **k: (nvidia_smi, "") if "nvidia-smi" in cmd else ("", ""),
    )
    monkeypatch.setattr(mine, "_get_public_ip", lambda: "203.0.113.7")
    monkeypatch.setattr(reg, "fetch_shared_config", _Snapshot)
    return target, executor_dir


def test_mine_register_never_formats_a_disk_for_the_storage_limit(
    monkeypatch, tmp_path: Path
) -> None:
    """--register implies --auto, and the portal's command runs unattended: on a host whose Docker cannot cap a
    container's disk it prints the manual setup command and installs on, it never plans or runs the disk setup."""
    import lium.cli.mine.gpu_splitting as gs

    target, executor_dir = _stub_host(monkeypatch, tmp_path)
    monkeypatch.setattr(mine, "_docker_storage_limited", lambda: False)
    touched: list[str] = []
    monkeypatch.setattr(gs, "_preflight", lambda: touched.append("preflight"))
    monkeypatch.setattr(gs, "_execute_setup", lambda plan: touched.append("execute"))
    portal = _Portal()
    portal.on(
        "POST",
        "/executors",
        _Resp(200, {"success": True, "data": {"message": "queued"}}),
    )
    portal.on("GET", "/executors", _listing())
    portal.on("GET", "/executors/node-1", _status("AVAILABLE"))
    monkeypatch.setattr(reg, "build_http", lambda url, token: _http(portal))
    monkeypatch.setattr(reg, "wait_until_listed", _wait_no_sleep)

    result = CliRunner().invoke(
        mine.mine_command,
        [
            "--register",
            _token(exp=int(time.time()) + 3600),
            "--dir",
            str(target),
            "--portal-url",
            "https://provider-api.example",
            "--wait",
            "5",
        ],
    )
    assert result.exit_code == 0, result.output
    assert touched == []
    assert "lium gpu-splitting setup --device" in result.output


def test_mine_register_repeated_with_the_same_claim_does_not_recreate_the_executor(
    monkeypatch, tmp_path: Path
) -> None:
    """A recreate restarts the executor under its rentals; only a claim the executor does not already run with needs
    one, so a rerun after a restart skipped under rentals still recreates though .env already holds the claim."""
    target, _ = _stub_host(monkeypatch, tmp_path)
    recreates: list[bool] = []
    executor = {"claim": None, "rentals": False}

    def start(d, recreate=False):
        recreates.append(recreate)
        if executor["claim"] is None or (recreate and not executor["rentals"]):
            executor["claim"] = mine._env_value(d / ".env", "MINER_ACCOUNT_CLAIM")

    monkeypatch.setattr(mine, "_start_executor", start)
    monkeypatch.setattr(
        mine,
        "_executor_env_value",
        lambda d, key: (
            executor["claim"]
            if key == "MINER_ACCOUNT_CLAIM" or executor["claim"] is None
            else mine._env_value(d / ".env", key)
        ),
    )
    portal = _Portal()
    portal.on(
        "POST",
        "/executors",
        _Resp(200, {"success": True, "data": {"message": "queued"}}),
    )
    portal.on("GET", "/executors", _listing())
    portal.on("GET", "/executors/node-1", _status("AVAILABLE"))
    monkeypatch.setattr(reg, "build_http", lambda url, token: _http(portal))
    monkeypatch.setattr(reg, "wait_until_listed", _wait_no_sleep)

    for claim, rentals in (
        ("ab12" * 8, False),
        ("ab12" * 8, False),
        ("cd34" * 8, True),
        ("cd34" * 8, False),
        ("cd34" * 8, False),
    ):
        executor["rentals"] = rentals
        result = CliRunner().invoke(
            mine.mine_command,
            [
                "--register",
                _token(exp=int(time.time()) + 3600, node_claim=claim),
                "--dir",
                str(target),
                "--portal-url",
                "https://provider-api.example",
                "--wait",
                "5",
            ],
        )
        assert result.exit_code == 0, result.output
    assert recreates == [False, False, True, True, False]


def _wait_no_sleep(http, node_id, **kw):
    """The real wait loop on a fake clock: every sleep advances it by the interval, so OFFLINE_CONFIRM_S elapses in no time."""
    now = {"t": 0.0}
    kw.setdefault("sleep", lambda secs: now.__setitem__("t", now["t"] + secs))
    kw.setdefault("clock", lambda: now["t"])
    return _orig_wait(http, node_id, **kw)


_orig_wait = reg.wait_until_listed


_PROBE = "/executors/00000000-0000-0000-0000-000000000000"


@pytest.mark.parametrize(
    "resp, refused",
    [
        (_Resp(404, {"detail": "Not found node"}), None),
        (
            _Resp(
                404, {"detail": {"message": "Not found node", "code": "node_not_found"}}
            ),
            None,
        ),
        (_Resp(404, {"detail": {"code": "node_not_found"}}), None),
        (_Resp(404, {"detail": {"message": "Not found node"}}), None),
        (
            _Resp(404, {"detail": {"message": "Not Found", "code": "route_not_found"}}),
            "did not answer as the portal",
        ),
        (
            _Resp(401, {"detail": "Invalid or expired token"}),
            "The portal refused the register token (Invalid or expired token)",
        ),
        (_Resp(404, {"detail": "Not Found"}), "did not answer as the portal"),
        (_Resp(200, {"id": "x"}), "did not answer as the portal"),
    ],
)
def test_confirm_register_token_asks_the_portal_with_the_token(resp, refused) -> None:
    portal = _Portal()
    portal.on("GET", _PROBE, resp)
    if refused is None:
        _orig_confirm(_http(portal))
    else:
        with pytest.raises(reg.RegisterError, match=re.escape(refused)):
            _orig_confirm(_http(portal))
    assert portal.calls[0]["headers"]["Authorization"] == "Bearer TOKEN"


def test_mine_register_refuses_a_token_the_portal_rejects_before_touching_the_host(
    monkeypatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        mine, "_clone_or_update_repo", lambda *a, **k: calls.append("clone")
    )
    monkeypatch.setattr(reg, "confirm_register_token", _orig_confirm)
    portal = _Portal()
    portal.on("GET", _PROBE, _Resp(401, {"detail": "Invalid or expired token"}))
    monkeypatch.setattr(reg, "build_http", lambda url, token: _http(portal))
    result = CliRunner().invoke(
        mine.mine_command, ["--register", _token(exp=int(time.time()) + 3600)]
    )
    assert (
        result.exit_code == 1
        and "refused the register token" in result.output
        and calls == []
    )


def _rate_limited(retry_after: str = "7") -> _Resp:
    resp = _Resp(429, {"detail": "rate_limited"})
    resp.headers = {"Retry-After": retry_after}
    return resp


def test_a_429_on_the_node_list_is_rate_limiting_not_a_missing_node() -> None:
    portal = _Portal()
    portal.on("GET", "/executors", _rate_limited())
    slept: list[float] = []
    with pytest.raises(reg.ProviderError, match=r"rate limit, retry in 7 s") as err:
        reg.find_node_id_after_add(
            _http(portal),
            miner_hotkey=HOTKEY,
            ip_address="203.0.113.7",
            port=8080,
            sleep=slept.append,
        )
    assert err.value.code == "PORTAL_RATE_LIMIT"
    assert slept == [7.0] * (
        reg.FIND_NODE_ATTEMPTS - 1
    )  # waits out Retry-After (>= the 5 s default) between tries


def test_a_429_then_the_listing_finds_the_node() -> None:
    portal = _Portal()
    portal.on("GET", "/executors", _rate_limited("120"), _listing())
    slept: list[float] = []
    assert (
        reg.find_node_id_after_add(
            _http(portal),
            miner_hotkey=HOTKEY,
            ip_address="203.0.113.7",
            port=8080,
            sleep=slept.append,
        )
        == "node-1"
    )
    assert slept == [reg.FIND_NODE_MAX_WAIT_S]  # a long Retry-After is bounded


def test_an_already_registered_node_behind_a_429_is_not_reported_as_another_accounts(
    monkeypatch,
) -> None:
    monkeypatch.setattr(reg, "FIND_NODE_ATTEMPTS", 1)
    portal = _Portal()
    portal.on("POST", "/executors", _Resp(400, {"detail": reg._ALREADY_EXISTS}))
    portal.on("GET", "/executors", _rate_limited())
    with pytest.raises(reg.ProviderError, match="rate limit"):
        reg.register_node(
            _http(portal),
            miner_hotkey=HOTKEY,
            gpu_type="NVIDIA L4",
            gpu_count=1,
            ip_address="203.0.113.7",
            port=8080,
            price_per_gpu=0.11,
        )
    portal.queues[("POST", "/executors")] = [
        _Resp(200, {"success": True, "data": {"message": "queued"}})
    ]
    record = reg.register_node(
        _http(portal),
        miner_hotkey=HOTKEY,
        gpu_type="NVIDIA L4",
        gpu_count=1,
        ip_address="203.0.113.7",
        port=8080,
        price_per_gpu=0.11,
    )
    assert record == reg.NodeRecord(
        node_id=None, already_registered=False
    )  # added; the list read is what failed


def test_an_unknown_nvidia_model_without_price_asks_for_price_and_keeps_its_name(
    monkeypatch,
):
    monkeypatch.setattr(
        reg,
        "fetch_shared_config",
        lambda: type("S", (), {"machine_prices": {"NVIDIA A100 80GB PCIe": 1.0}})(),
    )

    with pytest.raises(reg.RegisterError) as e:
        reg.resolve_price("NVIDIA A100-SXM4-40GB", None)

    assert "--price" in str(e.value) and "--gpu-type" not in str(e.value)


def test_an_unknown_nvidia_model_with_price_resolves_to_it():
    assert reg.resolve_price("NVIDIA A100-SXM4-40GB", 0.30) == 0.30


def test_parse_register_token_reads_a_dev_stack_and_its_central_miner() -> None:
    """DAH-6051: a staging/dev portal's token names the dev runner and that stack's central miner (SS58 only)."""
    central = "5E9mXBKNan8FbRGs71LnXaP8dAVkCVViDvkhbuNsfLwBzLsi"  # gitleaks:allow (public ss58 address)

    def tok(**sub):
        subject = {"miner_hotkey": HOTKEY, "scope": "node:register", **sub}
        return pyjwt.encode({"subject": subject, "exp": NOW + 3600}, "k" * 32, algorithm="HS256")

    t = reg.parse_register_token(tok(dev_stack=True, central_miner_hotkey=central), now=NOW)
    assert t.dev_stack is True and t.central_miner_hotkey == central
    assert reg.parse_register_token(tok(dev_stack=True, central_miner_hotkey="x; rm -rf /"), now=NOW).central_miner_hotkey is None
    prod = reg.parse_register_token(tok(), now=NOW)
    assert prod.dev_stack is False and prod.central_miner_hotkey is None


def test_a_dev_stack_token_starts_the_dev_runner_and_a_plain_rerun_keeps_it(monkeypatch, tmp_path: Path) -> None:
    """DAH-6051: a staging/dev portal's token puts the runner commands on docker-compose.dev.yml, and a rerun with
    no token still does (the choice is read back from .env)."""
    central = "5E9mXBKNan8FbRGs71LnXaP8dAVkCVViDvkhbuNsfLwBzLsi"  # gitleaks:allow (public ss58 address)
    target, _ = _stub_host(monkeypatch, tmp_path)
    monkeypatch.setattr(mine, "_RUNNER_FILE_OPT", "")   # restored after the test; the command sets it
    started: list[str] = []
    monkeypatch.setattr(mine, "_start_executor", lambda d, recreate=False: started.append(mine._RUNNER_FILE_OPT))
    portal = _Portal()
    portal.on("POST", "/executors", _Resp(200, {"success": True, "data": {"message": "queued"}}))
    portal.on("GET", "/executors", _listing())
    portal.on("GET", "/executors/node-1", _status("AVAILABLE"))
    monkeypatch.setattr(reg, "build_http", lambda url, token: _http(portal))
    monkeypatch.setattr(reg, "wait_until_listed", _wait_no_sleep)
    subject = {"miner_hotkey": HOTKEY, "opt_in_status": True, "scope": "node:register", "dev_stack": True,
               "central_miner_hotkey": central}
    token = pyjwt.encode({"subject": subject, "exp": int(time.time()) + 3600}, "k" * 32, algorithm="HS256")

    result = CliRunner().invoke(mine.mine_command, ["--register", token, "--dir", str(target),
                                                    "--portal-url", "https://provider-api.example", "--wait", "5"])
    assert result.exit_code == 0, result.output
    result = CliRunner().invoke(mine.mine_command, ["--auto", "-k", HOTKEY, "--dir", str(target)])
    assert result.exit_code == 0, result.output
    assert started == ["-f docker-compose.dev.yml "] * 2


def _listing_with_gpus(gpu_type: str, gpu_count: int) -> _Resp:
    return _Resp(
        200,
        {
            "success": True,
            "data": [
                {
                    "id": "node-1",
                    "executor_ip_address": "203.0.113.7",
                    "executor_ip_port": "8080",
                    "gpu_type": gpu_type,
                    "gpu_count": gpu_count,
                },
            ],
            "total": 1,
        },
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"gpu_type": "NVIDIA GeForce RTX 5060 Ti", "gpu_count": 1},
        {
            "gpu_type": None,
            "gpu_count": None,
            "executor_info": {"machine_name": "NVIDIA L4", "gpu_count": 1},
        },
        {"gpu_type": None, "gpu_count": 1},
    ],
)
def test_a_second_machine_on_the_first_ones_address_is_refused_with_the_port_flags(
    fields,
) -> None:
    portal = _Portal()
    portal.on(
        "POST",
        "/executors",
        _Resp(
            400,
            {
                "detail": {
                    "message": f"Node node-1 {reg._ALREADY_EXISTS} on your account"
                }
            },
        ),
    )
    portal.on(
        "GET",
        "/executors",
        _Resp(
            200,
            {
                "data": [
                    {
                        "id": "node-1",
                        "executor_ip_address": "203.0.113.7",
                        "executor_ip_port": "8080",
                        **fields,
                    },
                ]
            },
        ),
    )
    with pytest.raises(reg.RegisterError) as err:
        reg.register_node(
            _http(portal),
            miner_hotkey=HOTKEY,
            gpu_type="NVIDIA GeForce RTX 5060",
            gpu_count=2,
            ip_address="203.0.113.7",
            port=8080,
            price_per_gpu=0.11,
        )
    assert all(
        flag in str(err.value)
        for flag in ("--external-port", "--ssh-port", "--renting-port-range")
    )
    assert "not registered" in str(err.value)


def test_a_rerun_on_the_same_host_is_still_already_registered() -> None:
    portal = _Portal()
    portal.on("POST", "/executors", _Resp(400, {"detail": reg._ALREADY_EXISTS}))
    portal.on("GET", "/executors", _listing_with_gpus("NVIDIA L4", 1))
    record = reg.register_node(
        _http(portal),
        miner_hotkey=HOTKEY,
        gpu_type="NVIDIA L4",
        gpu_count=1,
        ip_address="203.0.113.7",
        port=8080,
        price_per_gpu=0.11,
    )
    assert record == reg.NodeRecord(node_id="node-1", already_registered=True)


@pytest.mark.parametrize(
    ("fields", "host_type", "host_count", "another"),
    [
        (
            {"gpu_type": "NVIDIA A10 Tensor Core GPU", "gpu_count": 1},
            "NVIDIA A10",
            1,
            False,
        ),  # declared alias
        (
            {
                "gpu_type": None,
                "gpu_count": None,
                "executor_info": {
                    "machine_name": "NVIDIA A10 Tensor Core GPU",
                    "gpu_count": 1,
                },
            },
            "NVIDIA A10",
            1,
            False,
        ),  # measured alias
        (
            {"gpu_type": "NVIDIA H100 PCIe", "gpu_count": 8},
            "NVIDIA H100 80GB HBM3",
            8,
            True,
        ),  # SXM and PCIe H100 differ
        (
            {"gpu_type": "NVIDIA A10 Tensor Core GPU", "gpu_count": 1},
            "NVIDIA L4",
            1,
            True,
        ),  # another model
        (
            {"gpu_type": "NVIDIA A10 Tensor Core GPU", "gpu_count": 1},
            "NVIDIA A10",
            2,
            True,
        ),  # another count
        (
            {"gpu_type": None, "gpu_count": None},
            "NVIDIA L4",
            1,
            False,
        ),  # nothing known: a re-run
    ],
)
def test_another_host_compares_gpu_names_through_the_validator_aliases(
    fields, host_type, host_count, another
) -> None:
    assert (
        reg._is_another_host(fields, gpu_type=host_type, gpu_count=host_count)
        is another
    )


@pytest.mark.parametrize(
    ("fields", "host_count", "another"),
    [
        (
            {"gpu_count": 4, "executor_info": {"gpu_count": 8}},
            8,
            False,
        ),  # declared edit, measured matches
        (
            {"gpu_count": 4, "executor_info": {"gpu_count": 8}},
            4,
            True,
        ),  # measured conflicts
        (
            {"gpu_count": 4, "executor_info": {}},
            4,
            False,
        ),  # no measurement: declared governs
        (
            {"gpu_count": None, "executor_info": {"gpu_count": 8}},
            8,
            False,
        ),  # no declaration
        ({"gpu_count": None, "executor_info": {}}, 2, False),  # neither known
    ],
)
def test_measured_gpu_count_governs_over_a_declared_edit(
    fields, host_count, another
) -> None:
    row = {"gpu_type": "NVIDIA L4", **fields}
    assert (
        reg._is_another_host(row, gpu_type="NVIDIA L4", gpu_count=host_count) is another
    )


_TOPO_ADAPTER = (
    "\tGPU0\tGPU1\tCPU Affinity\nGPU0\t X \tPIX\t0-31\nGPU1\tPIX\t X \t0-31\n"
)
_TOPO_HGX = "\tGPU0\tGPU1\tCPU Affinity\nGPU0\t X \tNV18\t0-31\nGPU1\tNV18\t X \t0-31\n"


def test_read_nvlink_pairs_counts_nv_cells_once_per_pair() -> None:
    assert reg.read_nvlink_pairs(lambda c: (_TOPO_ADAPTER, "")) == 0
    assert reg.read_nvlink_pairs(lambda c: (_TOPO_HGX, "")) == 1
    assert reg.read_nvlink_pairs(lambda c: ("", "")) is None

    def boom(c):
        raise RuntimeError("nvidia-smi failed")

    assert reg.read_nvlink_pairs(boom) is None


@pytest.mark.parametrize(
    ("node_type", "nvlink_pairs", "refused"),
    [
        ("NVIDIA H100 PCIe", 0, False),  # adapter host re-run: listed as PCIe, matches
        ("NVIDIA H100 PCIe", 4, True),  # a real SXM (HGX) host against a PCIe node
        ("NVIDIA H100 PCIe", None, True),  # no NVLink reading: no relabel
        ("NVIDIA H100 80GB HBM3", 4, False),  # HGX re-run keeps its name
        (
            "NVIDIA H100 80GB HBM3",
            0,
            False,
        ),  # adapter host re-run before its first validation: still the raw name
        ("NVIDIA A100 80GB PCIe", 0, True),  # another model is another host
    ],
)
def test_h100_adapter_host_is_compared_as_the_pcie_card_it_is_listed_as(
    node_type, nvlink_pairs, refused
) -> None:
    portal = _Portal()
    portal.on("POST", "/executors", _Resp(400, {"detail": reg._ALREADY_EXISTS}))
    portal.on("GET", "/executors", _listing_with_gpus(node_type, 8))

    def call():
        return reg.register_node(
            _http(portal),
            miner_hotkey=HOTKEY,
            gpu_type="NVIDIA H100 80GB HBM3",
            gpu_count=8,
            ip_address="203.0.113.7",
            port=8080,
            price_per_gpu=0.11,
            nvlink_pairs=nvlink_pairs,
        )

    if refused:
        with pytest.raises(reg.RegisterError, match="not registered"):
            call()
    else:
        assert call() == reg.NodeRecord(node_id="node-1", already_registered=True)


@pytest.mark.parametrize(
    ("node_type", "vram_mib", "refused"),
    [
        (
            "NVIDIA GeForce RTX 4090 48GB",
            44237,
            False,
        ),  # validated 48 GB host re-run: listed as the 48GB model
        (
            "NVIDIA GeForce RTX 4090",
            49140,
            False,
        ),  # not validated yet, or the flag off: the raw name
        (
            "NVIDIA GeForce RTX 4090 48GB",
            24564,
            True,
        ),  # a 24 GB card is not the 48 GB node
        ("NVIDIA GeForce RTX 4090 48GB", None, True),  # no VRAM reading: no relabel
    ],
)
def test_rtx_4090_48gb_host_is_compared_as_the_model_it_is_listed_as(
    node_type, vram_mib, refused
) -> None:
    portal = _Portal()
    portal.on("POST", "/executors", _Resp(400, {"detail": reg._ALREADY_EXISTS}))
    portal.on("GET", "/executors", _listing_with_gpus(node_type, 2))

    def call():
        return reg.register_node(
            _http(portal),
            miner_hotkey=HOTKEY,
            gpu_type="NVIDIA GeForce RTX 4090",
            gpu_count=2,
            ip_address="203.0.113.7",
            port=8080,
            price_per_gpu=0.11,
            vram_mib=vram_mib,
        )

    if refused:
        with pytest.raises(reg.RegisterError, match="not registered"):
            call()
    else:
        assert call() == reg.NodeRecord(node_id="node-1", already_registered=True)


@pytest.mark.parametrize(
    ("node_type", "label", "refused"),
    [
        ("NVIDIA L4", "NVIDIA L4", False),  # pending, label = hardware
        (
            "NVIDIA L4",
            "NVIDIA A10 Tensor Core GPU",
            False,
        ),  # same host re-run with a wrong label
        (
            "NVIDIA A10 Tensor Core GPU",
            "NVIDIA A10",
            True,
        ),  # aliased label of the other node, L4 hardware
        (
            "NVIDIA A10 Tensor Core GPU",
            "NVIDIA A10 Tensor Core GPU",
            True,
        ),  # conflicting: label matches, hardware not
        (
            "NVIDIA H100 PCIe",
            "NVIDIA H100 PCIe",
            False,
        ),  # validated adapter host listed under its listed name
    ],
)
def test_a_gpu_type_label_cannot_hide_another_machine(
    node_type, label, refused
) -> None:
    portal = _Portal()
    portal.on("POST", "/executors", _Resp(400, {"detail": reg._ALREADY_EXISTS}))
    portal.on(
        "GET",
        "/executors",
        _listing_with_gpus(node_type, 1 if "H100" not in node_type else 4),
    )
    hw = "NVIDIA H100 80GB HBM3" if "H100" in node_type else "NVIDIA L4"

    def call():
        return reg.register_node(
            _http(portal),
            miner_hotkey=HOTKEY,
            gpu_type=label,
            gpu_count=4 if "H100" in hw else 1,
            ip_address="203.0.113.7",
            port=8080,
            price_per_gpu=0.11,
            nvlink_pairs=0,
            detected_gpu_type=hw,
        )

    if refused:
        with pytest.raises(reg.RegisterError, match="not registered"):
            call()
    else:
        assert call() == reg.NodeRecord(node_id="node-1", already_registered=True)


def test_a_48gb_rtx_4090_at_the_low_window_edge_is_listed_as_48gb() -> None:
    inv = reg.parse_nvidia_smi("NVIDIA GeForce RTX 4090, 44237\n")
    assert inv.vram_gb == 43  # rounded, below 43.2 GiB: the MiB reading decides
    assert (
        reg._listed_gpu_model(inv.gpu_type, 1, None, inv.vram_mib)
        == "NVIDIA GeForce RTX 4090 48GB"
    )
    assert (
        reg._listed_gpu_model(inv.gpu_type, 1, None, 25805) == "NVIDIA GeForce RTX 4090"
    )
    assert (
        reg._listed_gpu_model(inv.gpu_type, 1, None, 44000)
        == "NVIDIA GeForce RTX 4090 48GB"
    )


def _measured_row(machine_name: str | None, count: int = 8) -> dict:
    row = {
        "id": "node-1",
        "executor_ip_address": "203.0.113.7",
        "executor_ip_port": "8080",
        "gpu_type": "NVIDIA H100 80GB HBM3",  # the raw name the first host sent
        "gpu_count": count,
    }
    if machine_name:
        row["executor_info"] = {"machine_name": machine_name, "gpu_count": count}
    return row


@pytest.mark.parametrize(
    ("measured", "nvlink_pairs", "refused"),
    [
        ("NVIDIA H100 80GB HBM3", 0, True),  # adapter host vs validated HGX node
        ("NVIDIA H100 PCIe", 4, True),  # HGX host vs validated adapter node
        ("NVIDIA H100 80GB HBM3", 4, False),  # validated HGX re-run
        ("NVIDIA H100 PCIe", 0, False),  # validated adapter re-run
        (None, 0, False),  # pending: raw-name fallback
        (None, 4, False),
    ],
)
def test_h100_adapter_and_hgx_hosts_are_told_apart_once_measured(
    measured, nvlink_pairs, refused
) -> None:
    portal = _Portal()
    portal.on("POST", "/executors", _Resp(400, {"detail": reg._ALREADY_EXISTS}))
    portal.on(
        "GET",
        "/executors",
        _Resp(200, {"success": True, "data": [_measured_row(measured)]}),
    )

    def call():
        return reg.register_node(
            _http(portal),
            miner_hotkey=HOTKEY,
            gpu_type="NVIDIA H100 80GB HBM3",
            gpu_count=8,
            ip_address="203.0.113.7",
            port=8080,
            price_per_gpu=0.11,
            nvlink_pairs=nvlink_pairs,
        )

    if refused:
        with pytest.raises(reg.RegisterError, match="not registered"):
            call()
    else:
        assert call() == reg.NodeRecord(node_id="node-1", already_registered=True)


def test_a_validated_node_without_a_measured_count_keeps_its_declared_count() -> None:
    row = _measured_row("NVIDIA H100 80GB HBM3", count=8)
    del row["executor_info"]["gpu_count"]
    portal = _Portal()
    portal.on("POST", "/executors", _Resp(400, {"detail": reg._ALREADY_EXISTS}))
    portal.on("GET", "/executors", _Resp(200, {"success": True, "data": [row]}))
    with pytest.raises(reg.RegisterError, match="not registered"):
        reg.register_node(
            _http(portal),
            miner_hotkey=HOTKEY,
            gpu_type="NVIDIA H100 80GB HBM3",
            gpu_count=4,
            ip_address="203.0.113.7",
            port=8080,
            price_per_gpu=0.11,
            nvlink_pairs=2,
        )


def test_a_validated_48gb_rtx_4090_rerun_is_accepted_below_the_cli_window() -> None:
    row = {
        "id": "node-1",
        "executor_ip_address": "203.0.113.7",
        "executor_ip_port": "8080",
        "gpu_type": "NVIDIA GeForce RTX 4090",
        "gpu_count": 1,
        "executor_info": {
            "machine_name": "NVIDIA GeForce RTX 4090 48GB",
            "gpu_count": 1,
        },
    }
    portal = _Portal()
    portal.on("POST", "/executors", _Resp(400, {"detail": reg._ALREADY_EXISTS}))
    portal.on("GET", "/executors", _Resp(200, {"success": True, "data": [row]}))
    for mib, ok in ((44000, True), (24564, False)):
        call = lambda: reg.register_node(  # noqa: E731
            _http(portal),
            miner_hotkey=HOTKEY,
            gpu_type="NVIDIA GeForce RTX 4090",
            gpu_count=1,
            ip_address="203.0.113.7",
            port=8080,
            price_per_gpu=0.11,
            vram_mib=mib,
        )
        if ok:
            assert call() == reg.NodeRecord(node_id="node-1", already_registered=True)
        else:
            with pytest.raises(reg.RegisterError, match="not registered"):
                call()


@pytest.mark.parametrize(
    ("machine_name", "mib", "ok"),
    [
        (
            "NVIDIA GeForce RTX 4090",
            49140,
            True,
        ),  # validated with the flag off: plain name, 48 GB card
        ("NVIDIA GeForce RTX 4090", 32000, True),  # odd size the validator keeps plain
        ("NVIDIA GeForce RTX 4090", 24564, True),
        ("NVIDIA GeForce RTX 4090 48GB", 49140, True),
        (
            "NVIDIA GeForce RTX 4090 48GB",
            24564,
            False,
        ),  # a 24 GB card is not the 48 GB node
    ],
)
def test_a_validated_rtx_4090_rerun_matches_either_listed_name(
    machine_name, mib, ok
) -> None:
    row = {
        "id": "node-1",
        "executor_ip_address": "203.0.113.7",
        "executor_ip_port": "8080",
        "gpu_type": "NVIDIA GeForce RTX 4090",
        "gpu_count": 1,
        "executor_info": {"machine_name": machine_name, "gpu_count": 1},
    }
    portal = _Portal()
    portal.on("POST", "/executors", _Resp(400, {"detail": reg._ALREADY_EXISTS}))
    portal.on("GET", "/executors", _Resp(200, {"success": True, "data": [row]}))

    def call():
        return reg.register_node(
            _http(portal),
            miner_hotkey=HOTKEY,
            gpu_type="NVIDIA GeForce RTX 4090",
            gpu_count=1,
            ip_address="203.0.113.7",
            port=8080,
            price_per_gpu=0.11,
            vram_mib=mib,
        )

    if ok:
        assert call() == reg.NodeRecord(node_id="node-1", already_registered=True)
    else:
        with pytest.raises(reg.RegisterError, match="not registered"):
            call()
