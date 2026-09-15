"""``lium mine --register <token>``: post this host as a node in the provider portal and wait until it is listed.

The token is the one the portal's Add Node page prints next to the command (``POST /executors/register-token``,
lium-platform). It is a short-lived portal JWT; the CLI reads the account it was issued for out of it and uses it
as the bearer for exactly one call, ``POST /executors``. Everything else here is unauthenticated: the node list and
the per-node status the portal shows are public reads.

Nothing about the node is typed by hand: GPU model and count come from ``nvidia-smi`` on this host, the port from
the executor's own ``.env``, the address from the host's public IPv4, the price is the model's base price from the
public shared-config (the Add Node form pre-fills the 30-day median when it has one).
``reports/INVALID_EXECUTOR_ROOTCAUSE.md``: hand-typed values are one of the ways a node ends up unrentable.
"""

from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import jwt as pyjwt

from lium.provider._routes import EXECUTOR_BY_ID, EXECUTORS
from lium.provider._shared_config import default_price_for_gpu, fetch_shared_config
from lium.provider.errors import ProviderError
from lium.provider.portal_http import DEFAULT_PORTAL_URL, PortalHTTP

LISTED_STATUSES = frozenset({"AVAILABLE", "RENTED"})
"""Computed statuses in which the node is in the market."""

FIX_STATUSES = frozenset({"VALIDATION_FAILED", "OFFLINE"})
"""Computed statuses that carry a ``last_error`` naming what the provider has to fix; waiting changes nothing.

``NOT_DETECTED`` is not here on purpose: the portal shows it for any node the validator has not reached in
30 min (``recently_created`` in the portal's ``ExecutorResponse``), which a first validation at the p90 of
44 min hits with no error attached — the poll keeps going and the deadline decides. ``OFFLINE`` comes from a
reachability flag the backend refreshes once a minute, so a node re-registered right after a reinstall can
still read OFFLINE on the first tick: it counts only once it has been read for ``OFFLINE_CONFIRM_S``.
"""

OFFLINE_CONFIRM_S = 60.0
"""How long OFFLINE has to persist before it is reported as the fix (one refresh of the backend's ping flag)."""

_SS58 = re.compile(r"[1-9A-HJ-NP-Za-km-z]{40,60}")
_IPV4 = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")

# what the portal answers on the two 400s a first-time provider can hit (apps/portal/backend executor_service.py)
_ALREADY_EXISTS = "already exists with the same ip and port"
_UNSUPPORTED_GPU = "Unsupported gpu type"


class RegisterError(Exception):
    """A registration step failed; ``str(e)`` is the whole message for the provider, fix included."""


@dataclass(frozen=True)
class RegisterToken:
    token: str
    miner_hotkey: str
    """The account the token was issued for: what ``GET /executors?miner_hotkey=`` lists the node under. For an
    account created with e-mail or Google (lium-platform#294, ``custody='lium'``) this is an opaque account id."""
    node_hotkey: str
    """What the executor reports under — ``MINER_HOTKEY_SS58_ADDRESS`` in its ``.env``: the token's ``node_hotkey``
    claim (the portal's key for an account without one of its own), else ``miner_hotkey`` for a portal that predates
    the claim."""
    exp: int | None
    opt_in_status: bool | None

    def seconds_left(self, now: int | None = None) -> int | None:
        if self.exp is None:
            return None
        return self.exp - (int(time.time()) if now is None else now)


def parse_register_token(token: str, *, now: int | None = None) -> RegisterToken:
    """Read the account and the expiry out of a portal token without the portal's secret.

    The signature is the portal's to check (it does, on ``POST /executors``); here the claims only decide which
    account the executor is configured for and whether it is worth starting a ten-minute install at all. The
    ``scope`` claim lium-platform#291 writes is not read: the CLI takes any bearer ``POST /executors`` takes.
    """
    token = (token or "").strip()
    try:
        claims = pyjwt.decode(token, options={"verify_signature": False, "verify_exp": False})
    except pyjwt.PyJWTError as e:
        raise RegisterError(
            "The register token is not one the portal issued. Copy the whole command from the portal's "
            "Add Node page (the token is the long value after --register)."
        ) from e
    subject = claims.get("subject")
    if not isinstance(subject, dict):
        subject = claims
    account = subject.get("miner_hotkey")
    if not isinstance(account, str) or not account:
        raise RegisterError(
            "The register token names no account. Get a new one from the portal's Add Node page."
        )
    node_hotkey = subject.get("node_hotkey")
    if not isinstance(node_hotkey, str) or not node_hotkey:
        node_hotkey = account
    # the executor refuses to start on anything but an SS58 value here; for an account with its own key the two
    # are the same string, for a custodied account only node_hotkey is one — the account id is not checked
    if not _SS58.fullmatch(node_hotkey):
        raise RegisterError(
            "The register token carries no usable node identity for this host's .env. Get a new one from the "
            "portal's Add Node page."
        )
    exp = claims.get("exp")
    exp = int(exp) if isinstance(exp, (int, float)) else None
    current = int(time.time()) if now is None else now
    if exp is not None and exp <= current:
        raise RegisterError(
            "The register token expired at "
            f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(exp))}. Open the portal's Add Node page again "
            "and copy the new command."
        )
    opt_in = subject.get("opt_in_status")
    return RegisterToken(
        token=token,
        miner_hotkey=account,
        node_hotkey=node_hotkey,
        exp=exp,
        opt_in_status=opt_in if isinstance(opt_in, bool) else None,
    )


@dataclass(frozen=True)
class GpuInventory:
    gpu_type: str
    gpu_count: int
    vram_gb: int


def parse_nvidia_smi(output: str) -> GpuInventory:
    """``nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits`` → one model, its count, its VRAM.

    The portal keys prices and validation on the exact name ``nvidia-smi`` reports (``NVIDIA L4``,
    ``NVIDIA GeForce RTX 4090``), so the name is passed through unchanged. A host with two different
    models cannot be one node: the portal has one GPU type per node.
    """
    rows: list[tuple[str, int]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        name, _, mem = line.rpartition(",")
        name = name.strip()
        try:
            mem_mib = int(mem.strip())
        except ValueError:
            mem_mib = 0
        if name:
            rows.append((name, mem_mib))
    if not rows:
        raise RegisterError("nvidia-smi reports no GPU on this host; the node was not registered.")
    names = sorted({name for name, _ in rows})
    if len(names) > 1:
        raise RegisterError(
            "This host has more than one GPU model (" + ", ".join(names) + "). The portal lists one model per "
            "node: register one model at a time with `lium provider node add --gpu-type ... --gpu-count ...`."
        )
    vram_gb = round(max(mem for _, mem in rows) / 1024)
    return GpuInventory(gpu_type=names[0], gpu_count=len(rows), vram_gb=vram_gb)


def executor_port(executor_dir: Path) -> int:
    """``EXTERNAL_PORT`` from the executor's rendered ``.env`` — the port the validator is told to reach."""
    env_f = Path(executor_dir) / ".env"
    for line in env_f.read_text().splitlines():
        if line.startswith("EXTERNAL_PORT="):
            value = line.split("=", 1)[1].split("#", 1)[0].strip()
            if value.isdigit():
                return int(value)
    raise RegisterError(f"EXTERNAL_PORT is missing from {env_f}; the node was not registered.")


def resolve_price(gpu_type: str, explicit: float | None) -> float:
    """``--price`` when given, else the portal's public default for this GPU model.

    The default is the model's base price from ``GET /v1/shared-config`` ``machine_prices``; the portal's
    accepted range is a multiple of its own machine-table price, normally the same value (the Add Node form
    pre-fills the 30-day median when it has one; that median becomes the default here when
    ``GET /public/provider-economics`` lands). When the model is not in the table the portal will refuse the node too, so the message names the
    closest known names and the flag that overrides the model.
    """
    if explicit is not None:
        return explicit
    snapshot = fetch_shared_config()
    try:
        return default_price_for_gpu(snapshot, gpu_type)
    except ProviderError as e:
        raise RegisterError(_unknown_gpu_message(gpu_type, sorted(snapshot.machine_prices))) from e


def _unknown_gpu_message(gpu_type: str, known: list[str]) -> str:
    close = difflib.get_close_matches(gpu_type, known, n=3, cutoff=0.5)
    hint = ("Closest names the portal knows: " + "; ".join(close) + ". ") if close else ""
    return (
        f"The portal does not list the GPU model this host reports ({gpu_type!r}). {hint}"
        "Re-run with --gpu-type '<portal name>' to register under that name, or ask support to add the model."
    )


def portal_web_url(portal_api_url: str | None) -> str:
    """The browser URL for a portal API URL: ``provider-api.<host>`` → ``provider.<host>``."""
    api = (portal_api_url or DEFAULT_PORTAL_URL).rstrip("/")
    return api.replace("://provider-api.", "://provider.", 1)


def _detail(e: ProviderError) -> str:
    body = (e.context or {}).get("body")
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, dict):
            return " ".join(str(v) for v in detail.values())
        if isinstance(detail, list):
            return " ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
    return str(body) if body else str(e)


@dataclass(frozen=True)
class NodeRecord:
    node_id: str | None
    """None when the add succeeded but the list did not show the node in 6 tries 5 s apart: registered, not watchable here."""
    already_registered: bool


def register_node(
    http: PortalHTTP,
    *,
    miner_hotkey: str,
    gpu_type: str,
    gpu_count: int,
    ip_address: str,
    port: int,
    price_per_gpu: float,
) -> NodeRecord:
    """``POST /executors`` with the register token, then find the node's id in the account's list.

    A node that is already in the portal at this address (a re-run of ``lium mine`` on the same host) is not
    an error: the portal refuses the duplicate and the existing record is what gets polled.
    """
    payload = {
        "gpu_type": gpu_type,
        "ip_address": ip_address,
        "port": port,
        "price_per_gpu": price_per_gpu,
        "gpu_count": gpu_count,
    }
    already = False
    try:
        http.post(EXECUTORS, json_body=payload)
    except ProviderError as e:
        status = (e.context or {}).get("status")
        detail = _detail(e)
        if status == 400 and _ALREADY_EXISTS in detail:
            already = True
        elif status == 400 and _UNSUPPORTED_GPU in detail:
            raise RegisterError(_unknown_gpu_message(gpu_type, _known_gpu_types())) from e
        elif status in (401, 403):
            raise RegisterError(
                "The portal refused the register token (" + detail + "). Open the Add Node page again and copy "
                "the new command; a token is valid for one hour."
            ) from e
        else:
            raise RegisterError(f"The portal refused the node: {detail}") from e

    if already:
        node_id = find_node_id_after_add(http, miner_hotkey=miner_hotkey, ip_address=ip_address, port=port)
        if node_id is None:
            # the portal's duplicate check is global (find_by_ip_and_port has no account filter)
            raise RegisterError(
                f"A node at {ip_address}:{port} is already in the portal but not in this account's node list "
                "(another account may hold it). Remove it there first, or contact support if that account is not yours."
            )
        return NodeRecord(node_id=node_id, already_registered=True)
    node_id = find_node_id_after_add(http, miner_hotkey=miner_hotkey, ip_address=ip_address, port=port)
    return NodeRecord(node_id=node_id, already_registered=False)


FIND_NODE_ATTEMPTS = 6
FIND_NODE_RETRY_S = 5.0


def find_node_id_after_add(
    http: PortalHTTP, *, miner_hotkey: str, ip_address: str, port: int, sleep: Callable[[float], None] = time.sleep
) -> str | None:
    """``find_node_id`` with retries: the list can lag the write, and a portal hiccup must not fail an add that succeeded."""
    for attempt in range(FIND_NODE_ATTEMPTS):
        try:
            node_id = find_node_id(http, miner_hotkey=miner_hotkey, ip_address=ip_address, port=port)
        except ProviderError:
            node_id = None
        if node_id is not None:
            return node_id
        if attempt < FIND_NODE_ATTEMPTS - 1:
            sleep(FIND_NODE_RETRY_S)
    return None


def _known_gpu_types() -> list[str]:
    try:
        return sorted(fetch_shared_config().machine_prices)
    except ProviderError:
        return []


def find_node_id(
    http: PortalHTTP, *, miner_hotkey: str, ip_address: str, port: int, max_pages: int = 10
) -> str | None:
    """The id of the account's node at ``ip_address:port`` from the public ``GET /executors`` list, or None."""
    for page in range(1, max_pages + 1):
        body = http.get(
            EXECUTORS,
            params={"miner_hotkey": miner_hotkey, "page": page, "limit": 100},
            auth=False,
        )
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list) or not rows:
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("executor_ip_address")) == ip_address and str(row.get("executor_ip_port")) == str(port):
                return str(row.get("id"))
        if len(rows) < 100:
            return None
    return None


@dataclass(frozen=True)
class NodeStatus:
    status: str
    message: str
    fix: str

    @property
    def listed(self) -> bool:
        return self.status in LISTED_STATUSES

    @property
    def needs_fix(self) -> bool:
        return self.status in FIX_STATUSES


def read_status(http: PortalHTTP, node_id: str) -> NodeStatus:
    """The portal's computed status for one node — the same text the Nodes page shows."""
    body = http.get(EXECUTOR_BY_ID.format(id=node_id), auth=False)
    computed = body.get("computed_status") if isinstance(body, dict) else None
    if not isinstance(computed, dict):
        return NodeStatus(status="UNKNOWN", message="", fix="")
    status = str(computed.get("status") or "UNKNOWN")
    message = str(computed.get("message") or "")
    fix = ""
    last_error = computed.get("last_error")
    if isinstance(last_error, dict):
        parts: list[str] = []
        for key in ("title", "message", "remediation"):
            value = str(last_error.get(key) or "").strip()
            # the portal sets title == message for validator errors, and message == the status message for OFFLINE
            if value and value not in parts and value != message:
                parts.append(value)
        fix = " ".join(parts)
    return NodeStatus(status=status, message=message, fix=fix)


def wait_until_listed(
    http: PortalHTTP,
    node_id: str,
    *,
    timeout_s: float,
    interval_s: float = 15.0,
    on_change: Callable[[NodeStatus, float], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> NodeStatus:
    """Poll the node's status until it is listed, names a fix, or ``timeout_s`` passes.

    ``on_change(status, elapsed_s)`` is called for the first reading and every change after it (status, message
    or the portal's fix text), never for a repeat, so a 20-minute wait prints a handful of lines. A read that
    fails (portal hiccup) is retried on the next tick; the last good reading is what a timeout returns. OFFLINE
    ends the wait only once it has been read for ``OFFLINE_CONFIRM_S`` (see ``FIX_STATUSES``).
    """
    start = clock()
    last: NodeStatus | None = None
    last_settled: NodeStatus | None = None   # the last reading that was not OFFLINE
    offline_since: float | None = None       # clock at the first OFFLINE reading of the current run of them
    offline_confirmed = False                # an OFFLINE reading OFFLINE_CONFIRM_S or more after that first one
    while True:
        try:
            current = read_status(http, node_id)
        except ProviderError:
            current = None
        if current is not None:
            now = clock()
            if last is None or current != last:
                if on_change:
                    on_change(current, now - start)
                last = current
            if current.status == "OFFLINE":
                offline_since = now if offline_since is None else offline_since
                offline_confirmed = now - offline_since >= OFFLINE_CONFIRM_S
            else:
                offline_since, offline_confirmed = None, False
                last_settled = current
            if current.listed or (current.needs_fix and (current.status != "OFFLINE" or offline_confirmed)):
                return current
        elapsed = clock() - start
        # an OFFLINE first read inside the last minute of the wait gets its minute: the deadline stretches by
        # OFFLINE_CONFIRM_S at most, so a single stale reading is never reported as the fix
        pending_offline = offline_since is not None and not offline_confirmed
        if elapsed >= timeout_s and not (pending_offline and elapsed < timeout_s + OFFLINE_CONFIRM_S):
            if pending_offline:
                # one OFFLINE reading and then only failed reads until the stretched deadline: not a fix
                return last_settled or NodeStatus(status="UNKNOWN", message="", fix="")
            return last or NodeStatus(status="UNKNOWN", message="", fix="")
        sleep(interval_s)


def opt_in_fix(token: RegisterToken, portal_api_url: str | None) -> str | None:
    """The one account-level reason a healthy node stays VALIDATION_PENDING, said up front."""
    if token.opt_in_status is False:
        return (
            "Your account is not connected to the Lium provider server yet, so no validator will check "
            f"this node. Turn it on under Settings at {portal_web_url(portal_api_url)}/settings, or run "
            "your own provider server."
        )
    return None


def public_ipv4_or_fail(ip: str) -> str:
    if not _IPV4.fullmatch(ip or ""):
        raise RegisterError(
            "Could not determine this host's public IPv4 address (the IP lookup services did not answer). "
            "The node was not registered; check outbound HTTPS from this host and re-run."
        )
    return ip


def build_http(portal_api_url: str | None, token: str) -> PortalHTTP:
    return PortalHTTP(base_url=portal_api_url or DEFAULT_PORTAL_URL, token_provider=lambda: token)


def read_gpu_inventory(run: Callable[[str], tuple[str, str]]) -> GpuInventory:
    out, _ = run("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits")
    return parse_nvidia_smi(out)


def status_line(status: NodeStatus, elapsed_s: float) -> str:
    """One poll line: elapsed, status, the portal's message, and its fix text when it has one (a
    VALIDATION_PENDING node whose account is not connected carries the remedy here, not in the status)."""
    mins, secs = divmod(int(elapsed_s), 60)
    text = f"[{mins:02d}:{secs:02d}] {status.status}"
    if status.message:
        text += f" — {status.message}"
    if status.fix and status.fix != status.message:
        text += f" · {status.fix}"
    return text


def result_summary(status: NodeStatus, *, node_url: str, waited_s: float) -> tuple[str, int]:
    """(message, exit code) for the end of the wait: 0 listed · 1 named fix · 2 still pending."""
    mins = int(waited_s // 60)
    if status.listed:
        return (f"Node listed ({status.status}) after {mins} min. {node_url}", 0)
    if status.needs_fix:
        # what failed, then the remedy: the status message names the failure, the fix text carries only what is new
        parts = [p for p in (status.message, status.fix) if p]
        fix = " ".join(dict.fromkeys(parts)) or "see the node page"
        return (f"FIX ({status.status}): {fix}\n{node_url}", 1)
    if status.status == "UNKNOWN":
        return (f"Could not read the node's status from the portal for {mins} min; the node stays registered: {node_url}", 2)
    if status.status in ("VALIDATION_PENDING", "NOT_DETECTED"):
        tail = f" Last note from the portal: {status.fix}" if status.fix else ""
        return (
            f"Still {status.status} after {mins} min; the validator keeps checking and the node page updates "
            f"on its own: {node_url}{tail}",
            2,
        )
    return (f"Node is {status.status} after {mins} min: {node_url}", 2)


__all__: list[str] = [
    "FIND_NODE_ATTEMPTS",
    "FIND_NODE_RETRY_S",
    "FIX_STATUSES",
    "GpuInventory",
    "LISTED_STATUSES",
    "NodeRecord",
    "NodeStatus",
    "OFFLINE_CONFIRM_S",
    "RegisterError",
    "RegisterToken",
    "build_http",
    "executor_port",
    "find_node_id",
    "find_node_id_after_add",
    "opt_in_fix",
    "parse_nvidia_smi",
    "parse_register_token",
    "portal_web_url",
    "public_ipv4_or_fail",
    "read_gpu_inventory",
    "read_status",
    "register_node",
    "resolve_price",
    "result_summary",
    "status_line",
    "wait_until_listed",
]
