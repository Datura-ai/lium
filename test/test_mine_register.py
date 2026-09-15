"""`lium mine --register <token>`: the node is posted from what the host reports, then watched until listed.

Origin: design/PROVIDER_BARRIER_PROGRAM.md §B.1 PR-1 (account → first node 34 min median, 20.5 min to listed with no
signal), reports/INVALID_EXECUTOR_ROOTCAUSE.md (hand-typed node values), DAH-3075 (SSH_PUBLIC_PORT left in .env).
"""

from __future__ import annotations

import json
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


def test_parse_nvidia_smi_reports_one_model_its_count_and_vram() -> None:
    out = "NVIDIA L4, 23034\nNVIDIA L4, 23034\nNVIDIA L4, 23034\nNVIDIA L4, 23034\n"
    assert reg.parse_nvidia_smi(out) == reg.GpuInventory(gpu_type="NVIDIA L4", gpu_count=4, vram_gb=22)


def test_parse_nvidia_smi_keeps_the_name_verbatim_even_with_a_comma_in_it() -> None:
    assert reg.parse_nvidia_smi("NVIDIA GeForce RTX 4090, 24564").gpu_type == "NVIDIA GeForce RTX 4090"
    # the memory column is the last comma-separated field; a comma inside the name stays in the name
    assert reg.parse_nvidia_smi("NVIDIA H100 80GB HBM3, Rev A, 81559").gpu_type == "NVIDIA H100 80GB HBM3, Rev A"


def test_parse_nvidia_smi_refuses_a_mixed_host_and_an_empty_one() -> None:
    with pytest.raises(reg.RegisterError, match="more than one GPU model .*NVIDIA A10G, NVIDIA L4"):
        reg.parse_nvidia_smi("NVIDIA L4, 23034\nNVIDIA A10G, 23028\n")
    with pytest.raises(reg.RegisterError, match="no GPU"):
        reg.parse_nvidia_smi("\n")


def test_executor_port_comes_from_the_rendered_env(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("INTERNAL_PORT=8080\nEXTERNAL_PORT=30310 # comment\nSSH_PORT=2200\n")
    assert reg.executor_port(tmp_path) == 30310
    (tmp_path / ".env").write_text("SSH_PORT=2200\n")
    with pytest.raises(reg.RegisterError, match="EXTERNAL_PORT is missing"):
        reg.executor_port(tmp_path)


def test_register_path_renders_ssh_public_port_equal_to_ssh_port(tmp_path: Path) -> None:
    """Regression pin (DAH-3075, #215) for the --auto defaults that --register implies: SSH_PUBLIC_PORT = SSH_PORT."""
    (tmp_path / ".env.template").write_text("MINER_HOTKEY_SS58_ADDRESS=\nSSH_PORT=2200\nSSH_PUBLIC_PORT=2200\nEXTERNAL_PORT=8080\n")
    answers = mine._gather_inputs(HOTKEY, auto=True)
    mine._setup_executor_env(tmp_path, hotkey=answers["hotkey"])
    mine._apply_env_overrides(tmp_path, answers["internal_port"], answers["external_port"], answers["ssh_port"],
                              answers["ssh_public_port"], answers["port_range"])
    env = dict(l.split("=", 1) for l in (tmp_path / ".env").read_text().splitlines() if "=" in l)
    assert env["SSH_PUBLIC_PORT"] == env["SSH_PORT"] == "2200"
    assert reg.executor_port(tmp_path) == 8080


def test_public_ipv4_or_fail_names_the_lookup_failure() -> None:
    assert reg.public_ipv4_or_fail("203.0.113.7") == "203.0.113.7"
    with pytest.raises(reg.RegisterError, match="public IPv4"):
        reg.public_ipv4_or_fail("Unable to determine")


# --- price -----------------------------------------------------------------------------------------------------------


class _Snapshot:
    machine_prices = {"NVIDIA L4": 0.11, "NVIDIA A10 Tensor Core GPU": 0.2, "NVIDIA GeForce RTX 4090": 0.3}


def test_resolve_price_uses_the_portal_default_unless_given(monkeypatch) -> None:
    monkeypatch.setattr(reg, "fetch_shared_config", _Snapshot)
    assert reg.resolve_price("NVIDIA L4", None) == 0.11
    assert reg.resolve_price("NVIDIA L4", 0.09) == 0.09


def test_resolve_price_names_the_closest_portal_name_for_an_unknown_model(monkeypatch) -> None:
    monkeypatch.setattr(reg, "fetch_shared_config", _Snapshot)
    with pytest.raises(reg.RegisterError) as e:
        reg.resolve_price("NVIDIA A10G", None)
    assert "does not list the GPU model this host reports ('NVIDIA A10G')" in str(e.value)
    assert "NVIDIA A10 Tensor Core GPU" in str(e.value) and "--gpu-type" in str(e.value)


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


def _listing(node_id: str = "node-1", ip: str = "203.0.113.7", port: int = 8080) -> _Resp:
    return _Resp(200, {"success": True, "data": [
        {"id": node_id, "executor_ip_address": ip, "executor_ip_port": str(port)},
    ], "total": 1})


def test_register_node_posts_with_the_token_and_finds_the_id_without_it() -> None:
    portal = _Portal()
    portal.on("POST", "/executors", _Resp(200, {"success": True, "data": {"message": "queued"}}))
    portal.on("GET", "/executors", _listing())
    record = reg.register_node(_http(portal), miner_hotkey=HOTKEY, gpu_type="NVIDIA L4", gpu_count=1,
                               ip_address="203.0.113.7", port=8080, price_per_gpu=0.11)
    assert record == reg.NodeRecord(node_id="node-1", already_registered=False)
    post, get = portal.calls
    assert post["headers"]["Authorization"] == "Bearer TOKEN"
    assert post["json"] == {"gpu_type": "NVIDIA L4", "ip_address": "203.0.113.7", "port": 8080,
                            "price_per_gpu": 0.11, "gpu_count": 1}
    assert "Authorization" not in get["headers"]
    assert get["params"] == {"miner_hotkey": HOTKEY, "page": 1, "limit": 100}


def test_register_node_retries_the_lookup_after_a_successful_add(monkeypatch) -> None:
    """A lagging or hiccupping list must not fail an add that went through."""
    monkeypatch.setattr(reg, "FIND_NODE_RETRY_S", 0.0)
    portal = _Portal()
    portal.on("POST", "/executors", _Resp(200, {"success": True, "data": {"message": "queued"}}))
    portal.on("GET", "/executors", _Resp(502, "bad gateway"), _Resp(200, {"data": []}), _listing())
    record = reg.register_node(_http(portal), miner_hotkey=HOTKEY, gpu_type="NVIDIA L4", gpu_count=1,
                               ip_address="203.0.113.7", port=8080, price_per_gpu=0.11)
    assert record == reg.NodeRecord(node_id="node-1", already_registered=False)
    assert [c["method"] for c in portal.calls] == ["POST", "GET", "GET", "GET"]


def test_register_node_gives_up_on_the_lookup_without_failing_the_add(monkeypatch) -> None:
    monkeypatch.setattr(reg, "FIND_NODE_RETRY_S", 0.0)
    portal = _Portal()
    portal.on("POST", "/executors", _Resp(200, {"success": True, "data": {"message": "queued"}}))
    portal.on("GET", "/executors", _Resp(200, {"data": []}))
    record = reg.register_node(_http(portal), miner_hotkey=HOTKEY, gpu_type="NVIDIA L4", gpu_count=1,
                               ip_address="203.0.113.7", port=8080, price_per_gpu=0.11)
    assert record == reg.NodeRecord(node_id=None, already_registered=False)
    assert len(portal.calls) == 1 + reg.FIND_NODE_ATTEMPTS


def test_register_node_names_a_duplicate_held_by_another_account(monkeypatch) -> None:
    """The portal's duplicate check is global; when the node is not in this account's list the message says so."""
    monkeypatch.setattr(reg, "FIND_NODE_RETRY_S", 0.0)
    portal = _Portal()
    portal.on("POST", "/executors", _Resp(400, {"detail": "Node already exists with the same ip and port."}))
    portal.on("GET", "/executors", _Resp(200, {"data": []}))
    with pytest.raises(reg.RegisterError, match="not in this account's node list"):
        reg.register_node(_http(portal), miner_hotkey=HOTKEY, gpu_type="NVIDIA L4", gpu_count=1,
                          ip_address="203.0.113.7", port=8080, price_per_gpu=0.11)
    assert len(portal.calls) == 1 + reg.FIND_NODE_ATTEMPTS   # the duplicate path retries the lookup too


def test_register_node_treats_the_duplicate_400_as_already_registered() -> None:
    portal = _Portal()
    portal.on("POST", "/executors", _Resp(400, {"detail": "Node already exists with the same ip and port."}))
    portal.on("GET", "/executors", _listing(node_id="old-node"))
    record = reg.register_node(_http(portal), miner_hotkey=HOTKEY, gpu_type="NVIDIA L4", gpu_count=1,
                               ip_address="203.0.113.7", port=8080, price_per_gpu=0.11)
    assert record == reg.NodeRecord(node_id="old-node", already_registered=True)


@pytest.mark.parametrize(
    ("status", "detail", "expect"),
    [
        (400, "Unsupported gpu type.", "does not list the GPU model"),
        (400, "Price per GPU should be between 0.0 and 0.22.", "refused the node: Price per GPU should be between"),
        (401, "Invalid token", "refused the register token"),
    ],
)
def test_register_node_turns_portal_refusals_into_named_fixes(monkeypatch, status, detail, expect) -> None:
    monkeypatch.setattr(reg, "fetch_shared_config", _Snapshot)
    portal = _Portal()
    portal.on("POST", "/executors", _Resp(status, {"detail": detail}))
    with pytest.raises(reg.RegisterError, match=expect):
        reg.register_node(_http(portal), miner_hotkey=HOTKEY, gpu_type="NVIDIA A10G", gpu_count=1,
                          ip_address="203.0.113.7", port=8080, price_per_gpu=0.11)
    assert len(portal.calls) == 1   # no listing lookup after a refusal


def test_find_node_id_pages_and_matches_on_address_and_port() -> None:
    portal = _Portal()
    page1 = _Resp(200, {"data": [{"id": f"n{i}", "executor_ip_address": "198.51.100.1", "executor_ip_port": "8080"}
                                 for i in range(100)]})
    portal.on("GET", "/executors", page1, _listing(node_id="mine", port=30310))
    assert reg.find_node_id(_http(portal), miner_hotkey=HOTKEY, ip_address="203.0.113.7", port=30310) == "mine"
    assert [c["params"]["page"] for c in portal.calls] == [1, 2]
    portal2 = _Portal()
    portal2.on("GET", "/executors", _Resp(200, {"data": []}))
    assert reg.find_node_id(_http(portal2), miner_hotkey=HOTKEY, ip_address="203.0.113.7", port=8080) is None


def _status(status: str, message: str = "", last_error: dict | None = None) -> _Resp:
    computed = {"status": status, "message": message, "last_error": last_error}
    return _Resp(200, {"id": "node-1", "computed_status": computed})


def test_wait_until_listed_prints_changes_only_and_stops_at_available() -> None:
    portal = _Portal()
    portal.on("GET", "/executors/node-1",
              _status("VALIDATION_PENDING", "Waiting for first validation"),
              _status("VALIDATION_PENDING", "Waiting for first validation"),
              _status("AVAILABLE"))
    seen: list[str] = []
    now = {"t": 0.0}
    final = reg.wait_until_listed(_http(portal), "node-1", timeout_s=3600, interval_s=15,
                                  on_change=lambda s, t: seen.append(reg.status_line(s, t)),
                                  sleep=lambda secs: now.__setitem__("t", now["t"] + secs), clock=lambda: now["t"])
    assert final.listed and final.status == "AVAILABLE"
    # three reads (0 s, 15 s, 30 s); the unchanged second reading prints nothing
    assert seen == ["[00:00] VALIDATION_PENDING — Waiting for first validation", "[00:30] AVAILABLE"]
    assert all("Authorization" not in c["headers"] for c in portal.calls)


def test_wait_until_listed_keeps_polling_through_not_detected() -> None:
    """NOT_DETECTED carries no error and appears 30 min after the add on any node the validator has not reached."""
    portal = _Portal()
    portal.on("GET", "/executors/node-1",
              _status("NOT_DETECTED", "The validator has not checked this node recently."),
              _status("AVAILABLE"))
    final = reg.wait_until_listed(_http(portal), "node-1", timeout_s=3600, sleep=lambda _: None)
    assert final.listed and final.status == "AVAILABLE"
    assert not reg.NodeStatus("NOT_DETECTED", "", "").needs_fix


def test_wait_until_listed_ignores_a_first_offline_reading_but_confirms_a_lasting_one() -> None:
    """The backend's reachability flag is refreshed once a minute; a node re-added after a reinstall reads
    OFFLINE on the first tick. One reading is not a fix; a minute of it is."""
    portal = _Portal()
    portal.on("GET", "/executors/node-1", _status("OFFLINE", "Node not responding to ping."),
              _status("VALIDATION_PENDING", "Waiting"), _status("AVAILABLE"))
    final = _wait_no_sleep(_http(portal), "node-1", timeout_s=3600, interval_s=15)
    assert final.listed
    lasting = _Portal()
    lasting.on("GET", "/executors/node-1", _status("OFFLINE", "Node not responding to ping.", {
        "title": "Offline", "message": "Node not responding to ping.", "source": "Pinger"}))
    seen: list[str] = []
    final = _wait_no_sleep(_http(lasting), "node-1", timeout_s=3600, interval_s=15,
                           on_change=lambda s, t: seen.append(reg.status_line(s, t)))
    assert final.needs_fix and final.status == "OFFLINE"
    assert len(lasting.calls) == 5   # 0, 15, 30, 45, 60 s: confirmed at OFFLINE_CONFIRM_S
    # the pinger's last_error repeats the status message; only the title is added
    assert seen == ["[00:00] OFFLINE — Node not responding to ping. · Offline"]


def test_wait_until_listed_keeps_offline_since_across_a_failed_read_and_gives_a_late_offline_its_minute() -> None:
    portal = _Portal()
    portal.on("GET", "/executors/node-1", _status("OFFLINE", "Node not responding to ping."),
              _Resp(502, "bad gateway"), _status("OFFLINE", "Node not responding to ping."))
    final = _wait_no_sleep(_http(portal), "node-1", timeout_s=3600, interval_s=15)
    assert final.needs_fix and len(portal.calls) == 5   # 0, 15 (failed), 30, 45, 60 s
    # OFFLINE read for the first time 15 s before the deadline: the wait stretches up to a minute and,
    # when the reading turns back to pending, ends there with exit 2 — never a single-reading fix
    late = _Portal()
    late.on("GET", "/executors/node-1", _status("VALIDATION_PENDING", "Waiting"), _status("VALIDATION_PENDING", "Waiting"),
            _status("VALIDATION_PENDING", "Waiting"), _status("OFFLINE", "Node not responding to ping."),
            _status("VALIDATION_PENDING", "Waiting"))
    final = _wait_no_sleep(_http(late), "node-1", timeout_s=60, interval_s=15)
    assert final.status == "VALIDATION_PENDING" and reg.result_summary(final, node_url="u", waited_s=75)[1] == 2
    stays = _Portal()
    stays.on("GET", "/executors/node-1", _status("VALIDATION_PENDING", "Waiting"), _status("VALIDATION_PENDING", "Waiting"),
             _status("VALIDATION_PENDING", "Waiting"), _status("OFFLINE", "Node not responding to ping."))
    final = _wait_no_sleep(_http(stays), "node-1", timeout_s=60, interval_s=15)
    assert final.status == "OFFLINE" and len(stays.calls) == 8   # confirmed at 45 + 60 = 105 s, 45 s past the 60 s deadline
    # OFFLINE once, then only failed reads until the stretched deadline: the last settled reading, not a fix
    dark = _Portal()
    dark.on("GET", "/executors/node-1", _status("VALIDATION_PENDING", "Waiting"), _status("VALIDATION_PENDING", "Waiting"),
            _status("VALIDATION_PENDING", "Waiting"), _status("OFFLINE", "Node not responding to ping."), _Resp(502, "bad gateway"))
    final = _wait_no_sleep(_http(dark), "node-1", timeout_s=60, interval_s=15)
    assert final.status == "VALIDATION_PENDING" and reg.result_summary(final, node_url="u", waited_s=120)[1] == 2


def test_wait_until_listed_prints_the_portal_remedy_on_a_pending_node() -> None:
    """A VALIDATION_PENDING node whose account is not connected carries the remedy in last_error, not in the status."""
    portal = _Portal()
    portal.on("GET", "/executors/node-1",
              _status("VALIDATION_PENDING", "Miner not answering", {
                  "title": "Miner not answering", "message": "Miner not answering", "source": "Validator",
                  "remediation": "Turn the switch ON in Settings."}),
              _status("AVAILABLE"))
    seen: list[str] = []
    _wait_no_sleep(_http(portal), "node-1", timeout_s=3600, on_change=lambda s, t: seen.append(reg.status_line(s, t)))
    # title == message == the status message: printed once, the remediation follows
    assert seen[0] == "[00:00] VALIDATION_PENDING — Miner not answering · Turn the switch ON in Settings."
    pending = reg.NodeStatus("VALIDATION_PENDING", "Miner not answering", "Turn the switch ON in Settings.")
    message, code = reg.result_summary(pending, node_url="u", waited_s=2700)
    assert code == 2 and message.endswith("Last note from the portal: Turn the switch ON in Settings.")


def test_result_summary_names_an_unreadable_status_and_a_non_validation_status() -> None:
    assert reg.result_summary(reg.NodeStatus("UNKNOWN", "", ""), node_url="u", waited_s=600)[0].startswith(
        "Could not read the node's status from the portal for 10 min")
    assert reg.result_summary(reg.NodeStatus("RECLAIMING", "", ""), node_url="u", waited_s=60) == ("Node is RECLAIMING after 1 min: u", 2)


def test_wait_until_listed_returns_the_named_fix_and_survives_a_read_error() -> None:
    portal = _Portal()
    portal.on("GET", "/executors/node-1",
              _Resp(502, "bad gateway"),
              _status("VALIDATION_FAILED", "Validation failed", {
                  "title": "Sysbox required", "message": "Sysbox required for unrented executor",
                  "source": "Validator", "remediation": "Install the sysbox runtime."}))
    final = reg.wait_until_listed(_http(portal), "node-1", timeout_s=3600, sleep=lambda _: None)
    assert final.needs_fix
    assert final.fix == "Sysbox required Sysbox required for unrented executor Install the sysbox runtime."
    message, code = reg.result_summary(final, node_url="https://provider.example/nodes/node-1", waited_s=900)
    assert code == 1 and message.startswith("FIX (VALIDATION_FAILED): Validation failed Sysbox required")


def test_result_summary_names_the_failure_and_the_remedy_for_the_portal_shape() -> None:
    """The portal's normal VALIDATION_FAILED: title == message == the status message, plus a remediation
    (the staging verdict of the AWS run)."""
    portal = _Portal()
    portal.on("GET", "/executors/node-1", _status("VALIDATION_FAILED", "Unknown NVIDIA driver version", {
        "title": "Unknown NVIDIA driver version", "message": "Unknown NVIDIA driver version", "source": "Validator",
        "reason_code": "NVML_DRIVER_UNKNOWN",
        "remediation": "Update to a supported NVIDIA driver version. Your current driver version is not recognized."}))
    status = reg.read_status(_http(portal), "node-1")
    message, code = reg.result_summary(status, node_url="u", waited_s=600)
    assert code == 1
    assert message.startswith("FIX (VALIDATION_FAILED): Unknown NVIDIA driver version Update to a supported NVIDIA driver version.")


def test_read_status_prints_the_portal_error_text_once_when_title_equals_message() -> None:
    portal = _Portal()
    portal.on("GET", "/executors/node-1", _status("VALIDATION_FAILED", "GPU count mismatch", {
        "title": "GPU count mismatch", "message": "GPU count mismatch", "source": "Validator",
        "remediation": "Fix the GPU count in the portal."}))
    # the status message already says "GPU count mismatch"; the fix text carries only what is new
    assert reg.read_status(_http(portal), "node-1").fix == "Fix the GPU count in the portal."


def test_wait_until_listed_times_out_with_the_last_reading() -> None:
    portal = _Portal()
    portal.on("GET", "/executors/node-1", _status("VALIDATION_PENDING", "Waiting"))
    now = {"t": 0.0}
    final = reg.wait_until_listed(_http(portal), "node-1", timeout_s=250, interval_s=100,
                                  sleep=lambda secs: now.__setitem__("t", now["t"] + secs), clock=lambda: now["t"])
    assert final.status == "VALIDATION_PENDING"
    message, code = reg.result_summary(final, node_url="u", waited_s=250)
    assert code == 2 and message.startswith("Still VALIDATION_PENDING after 4 min")


def test_result_summary_listed_is_exit_zero() -> None:
    message, code = reg.result_summary(reg.NodeStatus("AVAILABLE", "", ""), node_url="u", waited_s=130)
    assert (code, message) == (0, "Node listed (AVAILABLE) after 2 min. u")


def test_portal_web_url_maps_api_host_to_portal_host() -> None:
    assert reg.portal_web_url(None) == "https://provider.lium.io"
    assert reg.portal_web_url("https://provider-api.staging.lium.io/") == "https://provider.staging.lium.io"
    assert reg.portal_web_url("http://localhost:8000") == "http://localhost:8000"


def test_opt_in_fix_only_when_the_token_says_the_account_is_not_connected() -> None:
    off = reg.parse_register_token(_token(opt_in=False), now=NOW)
    assert "not connected to the Lium provider server" in (reg.opt_in_fix(off, None) or "")
    assert reg.opt_in_fix(reg.parse_register_token(_token(opt_in=True), now=NOW), None) is None


# --- the command -----------------------------------------------------------------------------------------------------


_TEMPLATE = "MINER_HOTKEY_SS58_ADDRESS=\nINTERNAL_PORT=8080\nEXTERNAL_PORT=8080\nSSH_PORT=2200\nSSH_PUBLIC_PORT=2200\n"


def _stub_host(monkeypatch, tmp_path: Path, *, nvidia_smi: str = "NVIDIA L4, 23034\n") -> tuple[Path, Path]:
    """Steps 1–6 with every host action stubbed: the clone drops an executor ``.env.template``, install/prereqs/start/
    validate/port checks are no-ops, ``nvidia-smi`` answers ``nvidia_smi``, the public IP is TEST-NET. Returns
    ``(target, executor_dir)``; the portal is the caller's."""
    target = tmp_path / "compute-subnet"
    executor_dir = target / "neurons" / "executor"

    def fake_clone(target_dir: Path, branch: str) -> None:
        executor_dir.mkdir(parents=True, exist_ok=True)
        (executor_dir / ".env.template").write_text(_TEMPLATE)

    monkeypatch.setattr(mine, "_clone_or_update_repo", fake_clone)
    for name in ("_install_executor_tools", "_check_prereqs", "_start_executor", "_validate_executor", "_check_ports_free"):
        monkeypatch.setattr(mine, name, lambda *a, **k: None)

    class _Pull:
        def poll(self): return 0
        def wait(self): return 0
    monkeypatch.setattr(mine, "_start_preflight_pull", lambda: _Pull())
    monkeypatch.setattr(mine, "_run", lambda cmd, **k: (nvidia_smi, "") if "nvidia-smi" in cmd else ("", ""))
    monkeypatch.setattr(mine, "_get_public_ip", lambda: "203.0.113.7")
    monkeypatch.setattr(reg, "fetch_shared_config", _Snapshot)
    return target, executor_dir


def _rendered_env(executor_dir: Path) -> dict[str, str]:
    return dict(l.split("=", 1) for l in (executor_dir / ".env").read_text().splitlines() if "=" in l)


def test_mine_register_refuses_an_expired_token_before_touching_the_host(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(mine, "_clone_or_update_repo", lambda *a, **k: calls.append("clone"))
    result = CliRunner().invoke(mine.mine_command, ["--register", _token(exp=int(time.time()) - 5)])
    assert result.exit_code == 1
    assert "expired at" in result.output and calls == []


def test_mine_register_only_options_are_refused_without_a_token(monkeypatch) -> None:
    result = CliRunner().invoke(mine.mine_command, ["--price", "0.5", "--wait", "3"])
    assert result.exit_code == 2 and "--price, --wait: only with --register TOKEN." in result.output
    # LIUM_PORTAL_URL in the environment (the `lium provider` group's variable) is not "given": plain `lium mine` runs
    calls: list[str] = []
    monkeypatch.setattr(mine, "_clone_or_update_repo", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop here")))
    result = CliRunner().invoke(mine.mine_command, ["-k", HOTKEY, "--auto"], env={"LIUM_PORTAL_URL": "https://provider-api.example"})
    assert "only with --register" not in result.output and "stop here" in result.output


def test_mine_register_refuses_a_conflicting_hotkey(monkeypatch) -> None:
    result = CliRunner().invoke(mine.mine_command, ["--register", _token(exp=int(time.time()) + 3600), "-k", POOL_HOTKEY])
    assert result.exit_code == 1 and "differs from what the register token says" in result.output
    # -k equal to what the node reports under is not a conflict: the portal's key on a custodied token (the Overview's
    # install command carries it), the account's own key on an own-key token — the install starts in both cases
    monkeypatch.setattr(mine, "_clone_or_update_repo", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop here")))
    for token, given in ((_token(exp=int(time.time()) + 3600, hotkey=ACCOUNT_ID, node_hotkey=POOL_HOTKEY), POOL_HOTKEY),
                         (_token(exp=int(time.time()) + 3600), HOTKEY)):
        result = CliRunner().invoke(mine.mine_command, ["--register", token, "-k", given])
        assert "differs from what the register token says" not in result.output and "stop here" in result.output


def test_mine_register_runs_the_install_then_registers_and_waits(monkeypatch, tmp_path: Path) -> None:
    """End to end through the command with every host action stubbed: the token's account lands in .env,
    --auto is implied, the node is posted from nvidia-smi + .env + public IP, and the wait ends listed."""
    target, executor_dir = _stub_host(monkeypatch, tmp_path)

    portal = _Portal()
    portal.on("POST", "/executors", _Resp(200, {"success": True, "data": {"message": "queued"}}))
    portal.on("GET", "/executors", _listing())
    portal.on("GET", "/executors/node-1", _status("VALIDATION_PENDING", "Waiting for first validation"),
              _status("AVAILABLE"))
    monkeypatch.setattr(reg, "build_http", lambda url, token: _http(portal))
    monkeypatch.setattr(reg, "wait_until_listed", _wait_no_sleep)

    result = CliRunner().invoke(
        mine.mine_command,
        ["--register", _token(exp=int(time.time()) + 3600), "--dir", str(target),
         "--portal-url", "https://provider-api.example", "--wait", "5"],
    )
    assert result.exit_code == 0, result.output
    env = _rendered_env(executor_dir)
    assert env["MINER_HOTKEY_SS58_ADDRESS"] == HOTKEY and env["SSH_PUBLIC_PORT"] == env["SSH_PORT"]
    post = next(c for c in portal.calls if c["method"] == "POST")
    assert post["json"] == {"gpu_type": "NVIDIA L4", "ip_address": "203.0.113.7", "port": 8080,
                            "price_per_gpu": 0.11, "gpu_count": 1}
    listing = next(c for c in portal.calls if c["method"] == "GET" and c["url"].endswith("/executors"))
    assert listing["params"]["miner_hotkey"] == HOTKEY
    assert "Node added: 1×NVIDIA L4 (22 GB) at 203.0.113.7:8080, $0.11/GPU/h" in result.output
    assert "https://provider.example/nodes/node-1" in result.output
    assert "VALIDATION_PENDING" in result.output and "Node listed (AVAILABLE)" in result.output


def test_mine_register_custodied_token_writes_the_pool_key_to_env_and_lists_under_the_account(monkeypatch, tmp_path: Path) -> None:
    """lium-platform#294: an account created with e-mail or Google has an opaque id as ``miner_hotkey`` and the pool's
    key as ``node_hotkey``. The .env gets the pool's key (the executor's SS58 check passes), the node is looked up
    under the account id, and nothing about the id is validated as SS58."""
    target, executor_dir = _stub_host(monkeypatch, tmp_path)

    portal = _Portal()
    portal.on("POST", "/executors", _Resp(200, {"success": True, "data": {"message": "queued"}}))
    portal.on("GET", "/executors", _listing())
    portal.on("GET", "/executors/node-1", _status("AVAILABLE"))
    monkeypatch.setattr(reg, "build_http", lambda url, token: _http(portal))
    monkeypatch.setattr(reg, "wait_until_listed", _wait_no_sleep)

    result = CliRunner().invoke(
        mine.mine_command,
        ["--register", _token(exp=int(time.time()) + 3600, hotkey=ACCOUNT_ID, node_hotkey=POOL_HOTKEY),
         "--dir", str(target), "--wait", "5"],
    )
    assert result.exit_code == 0, result.output
    assert "Invalid hotkey format" not in result.output and "no usable node identity" not in result.output
    assert _rendered_env(executor_dir)["MINER_HOTKEY_SS58_ADDRESS"] == POOL_HOTKEY
    listing = next(c for c in portal.calls if c["method"] == "GET" and c["url"].endswith("/executors"))
    assert listing["params"]["miner_hotkey"] == ACCOUNT_ID
    assert "Node listed (AVAILABLE)" in result.output


def _wait_no_sleep(http, node_id, **kw):
    """The real wait loop on a fake clock: every sleep advances it by the interval, so OFFLINE_CONFIRM_S elapses in no time."""
    now = {"t": 0.0}
    kw.setdefault("sleep", lambda secs: now.__setitem__("t", now["t"] + secs))
    kw.setdefault("clock", lambda: now["t"])
    return _orig_wait(http, node_id, **kw)


_orig_wait = reg.wait_until_listed


def test_mine_register_exit_one_on_a_named_fix_and_zero_with_wait_zero(monkeypatch, tmp_path: Path) -> None:
    target, _ = _stub_host(monkeypatch, tmp_path)
    monkeypatch.setattr(reg, "wait_until_listed", _wait_no_sleep)

    portal = _Portal()
    portal.on("POST", "/executors", _Resp(200, {"success": True, "data": {"message": "queued"}}))
    portal.on("GET", "/executors", _listing())
    portal.on("GET", "/executors/node-1", _status("OFFLINE", "Node not responding to ping."))
    monkeypatch.setattr(reg, "build_http", lambda url, token: _http(portal))
    args = ["--register", _token(exp=int(time.time()) + 3600), "--dir", str(target), "--wait", "1"]
    result = CliRunner().invoke(mine.mine_command, args)
    assert result.exit_code == 1 and "FIX (OFFLINE): Node not responding to ping." in result.output

    # --wait 0: registered, no waiting, exit 0
    result = CliRunner().invoke(mine.mine_command, args[:-2] + ["--wait", "0"])
    assert result.exit_code == 0 and "Waiting for the validator" not in result.output


def test_mine_register_reports_the_add_and_exits_two_when_the_list_lags(monkeypatch, tmp_path: Path) -> None:
    """Registered but not listed is exit 2 (docs/exit-codes.md), even when the list lagged before any wait."""
    target, _ = _stub_host(monkeypatch, tmp_path)
    monkeypatch.setattr(reg, "FIND_NODE_RETRY_S", 0.0)
    portal = _Portal()
    portal.on("POST", "/executors", _Resp(200, {"success": True, "data": {"message": "queued"}}))
    portal.on("GET", "/executors", _Resp(200, {"data": []}))
    monkeypatch.setattr(reg, "build_http", lambda url, token: _http(portal))
    result = CliRunner().invoke(mine.mine_command, ["--register", _token(exp=int(time.time()) + 3600), "--dir", str(target)])
    assert result.exit_code == 2, result.output
    flat = " ".join(result.output.split())   # the console soft-wraps at 80 columns
    assert "Node added: 1×NVIDIA L4 (22 GB) at 203.0.113.7:8080, $0.11/GPU/h" in flat
    assert "not in the node list yet" in flat and "https://provider.lium.io/nodes" in flat


def test_get_public_ip_falls_through_a_service_that_is_down(monkeypatch) -> None:
    """The first service is down (curl exit 7). `_run(check=True)` would raise there; the lookup asks with check=False."""
    answers = iter([("", "curl: (7) Failed to connect"), ("203.0.113.7\n", "")])

    def fake_run(cmd, **kwargs):
        out, err = next(answers)
        if err and kwargs.get("check", True):
            raise RuntimeError(f"Command failed (7): {cmd}")
        return out, err

    monkeypatch.setattr(mine, "_run", fake_run)
    assert mine._get_public_ip() == "203.0.113.7"


def test_mine_register_unknown_gpu_is_a_named_fix_and_nothing_is_posted(monkeypatch, tmp_path: Path) -> None:
    target, _ = _stub_host(monkeypatch, tmp_path, nvidia_smi="NVIDIA A10G, 23028\n")
    portal = _Portal()
    monkeypatch.setattr(reg, "build_http", lambda url, token: _http(portal))
    result = CliRunner().invoke(mine.mine_command, ["--register", _token(exp=int(time.time()) + 3600), "--dir", str(target)])
    assert result.exit_code == 1
    assert "does not list the GPU model this host reports ('NVIDIA A10G')" in result.output
    assert "NVIDIA A10 Tensor Core GPU" in result.output and portal.calls == []
