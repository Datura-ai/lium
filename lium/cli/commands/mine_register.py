"""``lium mine --register <token>``: post this host as a node in the provider portal and wait until it is listed.

The token is the one the portal's Add Node page prints next to the command (``POST /executors/register-token``,
lium-platform). It is a short-lived portal JWT; the CLI reads the account it was issued for out of it and uses it
as the bearer for three calls: one read the portal checks it on before anything is installed
(``confirm_register_token``), ``POST /executors``, and the node list it finds the new node's id in (the portal shows
a node's address to its owner only). The per-node status the portal shows is a public read.

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
from lium.provider.errors import PORTAL_RATE_LIMIT, ProviderError, ProviderNotFoundError
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
_NODE_CLAIM = re.compile(r"[0-9a-f]{16,128}")
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
    account_claim: str | None = None
    """``MINER_ACCOUNT_CLAIM`` for the executor's ``.env``: the token's ``node_claim``. Every custodied node reports the
    same pool hotkey, so the portal adds a pool-hotkey node only to the account whose claim it reports."""

    def seconds_left(self, now: int | None = None) -> int | None:
        if self.exp is None:
            return None
        return self.exp - (int(time.time()) if now is None else now)


def parse_register_token(token: str, *, now: int | None = None) -> RegisterToken:
    """Read the account and the expiry out of a portal token without the portal's secret.

    The signature is the portal's to check (``confirm_register_token``, before the install); here the claims only decide which
    account the executor is configured for and whether it is worth starting a ten-minute install at all. The
    ``scope`` claim lium-platform#291 writes is not read: the CLI takes any bearer ``POST /executors`` takes.
    """
    token = (token or "").strip()
    try:
        claims = pyjwt.decode(
            token, options={"verify_signature": False, "verify_exp": False}
        )
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
    # it goes into .env verbatim, so only a plain hex value; anything else is left out and Add Node says why
    node_claim = subject.get("node_claim")
    if not isinstance(node_claim, str) or not _NODE_CLAIM.fullmatch(node_claim):
        node_claim = None
    return RegisterToken(
        token=token,
        miner_hotkey=account,
        node_hotkey=node_hotkey,
        exp=exp,
        opt_in_status=opt_in if isinstance(opt_in, bool) else None,
        account_claim=node_claim,
    )


@dataclass(frozen=True)
class GpuInventory:
    gpu_type: str
    gpu_count: int
    vram_gb: int
    vram_mib: int | None = None  # raw, for the validator's MiB model windows


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
        raise RegisterError(
            "nvidia-smi reports no GPU on this host; the node was not registered."
        )
    names = sorted({name for name, _ in rows})
    if len(names) > 1:
        raise RegisterError(
            "This host has more than one GPU model ("
            + ", ".join(names)
            + "). The portal lists one model per "
            "node: register one model at a time with `lium provider node add --gpu-type ... --gpu-count ...`."
        )
    vram_gb = round(max(mem for _, mem in rows) / 1024)
    return GpuInventory(
        gpu_type=names[0],
        gpu_count=len(rows),
        vram_gb=vram_gb,
        vram_mib=max(mem for _, mem in rows),
    )


def executor_port(executor_dir: Path) -> int:
    """``EXTERNAL_PORT`` from the executor's rendered ``.env`` — the port the validator is told to reach."""
    env_f = Path(executor_dir) / ".env"
    for line in env_f.read_text().splitlines():
        if line.startswith("EXTERNAL_PORT="):
            value = line.split("=", 1)[1].split("#", 1)[0].strip()
            if value.isdigit():
                return int(value)
    raise RegisterError(
        f"EXTERNAL_PORT is missing from {env_f}; the node was not registered."
    )


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
        raise RegisterError(
            _unknown_gpu_message(gpu_type, sorted(snapshot.machine_prices))
        ) from e


def _unknown_gpu_message(gpu_type: str, known: list[str]) -> str:
    if gpu_type.startswith("NVIDIA "):
        # DAH-5088: the portal takes any NVIDIA model and prices it after the validator verifies the card
        return (
            f"The portal has no price yet for this host's GPU model ({gpu_type!r}). Re-run with --price <USD per GPU "
            "per hour> to register it under this name; a price below the minimum the portal sets once the card is "
            "verified keeps the node unlisted until you raise it."
        )
    close = difflib.get_close_matches(gpu_type, known, n=3, cutoff=0.5)
    hint = (
        ("Closest names the portal knows: " + "; ".join(close) + ". ") if close else ""
    )
    return (
        f"The portal does not list the GPU model this host reports ({gpu_type!r}). {hint}"
        "Re-run with --gpu-type '<portal name>' to register under that name, or ask support to add the model."
    )


_NO_SUCH_NODE = "00000000-0000-0000-0000-000000000000"
_NOT_FOUND_NODE = "Not found node"
"""What the portal's ``GET /executors/{id}`` answers for an id no node has (``executor_service.find_by_executor_id``)."""
_NOT_FOUND_NODE_CODE = "node_not_found"


def _is_not_found_node(e: ProviderError) -> bool:
    """The portal's unknown-node 404: the bare string, or the coded ``{message, code}`` detail newer portals send."""
    body = (e.context or {}).get("body")
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return (
            detail.get("code") == _NOT_FOUND_NODE_CODE
            or detail.get("message") == _NOT_FOUND_NODE
        )
    return detail == _NOT_FOUND_NODE


_NOT_THE_PORTAL = (
    "Could not confirm the register token: the portal address did not answer as the portal. "
    "Copy the whole command from the portal's Add Node page and run it again."
)


def confirm_register_token(http: PortalHTTP) -> None:
    """Have the portal check the register token before anything is installed or started.

    ``parse_register_token`` reads the claims without the portal's secret; the portal signed them, so a token it
    accepts names exactly the account it was issued for. ``GET /executors/{id}`` is one of the two routes that take
    a register token and checks it before it looks the node up: on an id no node has, a 404 naming the missing node
    means the token is good, a 401 that it is not.
    """
    try:
        http.get(EXECUTOR_BY_ID.format(id=_NO_SUCH_NODE))
    except ProviderNotFoundError as e:
        if _is_not_found_node(e):
            return
        raise RegisterError(_NOT_THE_PORTAL) from e
    except ProviderError as e:
        if (e.context or {}).get("status") in (401, 403):
            raise RegisterError(
                "The portal refused the register token ("
                + _detail(e)
                + "). Open the Add Node page again and copy "
                "the new command; a token is valid for one hour."
            ) from e
        raise RegisterError(
            f"Could not confirm the register token with the portal ({_detail(e)}). Nothing was installed; "
            "check this host's connection and run the command again."
        ) from e
    # a 200 is not a portal answer for this id: refuse rather than install under a token nobody checked
    raise RegisterError(_NOT_THE_PORTAL)


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
            return " ".join(
                str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail
            )
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
    nvlink_pairs: int | None = None,
    vram_mib: int | None = None,
    detected_gpu_type: str | None = None,
) -> NodeRecord:
    """``POST /executors`` with the register token, then find the node's id in the account's list.

    A node that is already in the portal at this address (a re-run of ``lium mine`` on the same host) is not
    an error: the portal refuses the duplicate and the existing record is what gets polled. One with other GPUs is
    a second machine behind the same IP registering on the first one's port: that is refused, naming the flags.
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
            raise RegisterError(
                _unknown_gpu_message(gpu_type, _known_gpu_types())
            ) from e
        elif status in (401, 403):
            raise RegisterError(
                "The portal refused the register token ("
                + detail
                + "). Open the Add Node page again and copy "
                "the new command; a token is valid for one hour."
            ) from e
        else:
            raise RegisterError(f"The portal refused the node: {detail}") from e

    if already:
        row = find_node_after_add(
            http, miner_hotkey=miner_hotkey, ip_address=ip_address, port=port
        )
        if row is None:
            # the portal's duplicate check is global (find_by_ip_and_port has no account filter)
            raise RegisterError(
                f"A node at {ip_address}:{port} is already in the portal but not in this account's node list "
                "(another account may hold it). Remove it there first, or contact support if that account is not yours."
            )
        # compare the measured GPU, never a --gpu-type label (a label cannot hide another machine)
        hw = detected_gpu_type or gpu_type
        listed = _listed_gpu_model(hw, gpu_count, nvlink_pairs, vram_mib)
        info = (
            row.get("executor_info")
            if isinstance(row.get("executor_info"), dict)
            else {}
        )
        if info.get("machine_name"):  # validated: compare the validator's measured name
            # the H100 adapter relabel is hardware (NVLink), so its raw name must not match an HGX node; the
            # memory-modded relabel depends on the validator's VRAM reading, so either name may be listed
            adapter = listed == "NVIDIA H100 PCIe" and not _same_gpu_model(hw, listed)
            measured = {**row, "gpu_type": info["machine_name"]}
            another = all(
                _is_another_host(measured, gpu_type=n, gpu_count=gpu_count)
                for n in ((listed,) if adapter else (hw, listed))
            )
        else:  # before its first validation the node keeps the raw name this host sent
            another = all(
                _is_another_host(row, gpu_type=n, gpu_count=gpu_count)
                for n in (hw, listed)
            )
        if another:
            node_type, node_count = _node_gpus(row)
            raise RegisterError(
                f"{ip_address}:{port} is already your node {row.get('id')} ({node_count or '?'}x "
                f"{node_type or 'unknown GPU'}), and this host has {gpu_count}x {hw}. If this is a second machine "
                "behind the same public IP, forward its own ports on the router and re-run with them, e.g. "
                "--external-port 8081 --ssh-port 2201 --renting-port-range 41000-41100. The node was not registered."
            )
        return NodeRecord(node_id=str(row.get("id")), already_registered=True)
    try:
        node_id = find_node_id_after_add(
            http, miner_hotkey=miner_hotkey, ip_address=ip_address, port=port
        )
    except (
        ProviderError
    ):  # rate limited: the add went through, only the list read did not
        node_id = None
    return NodeRecord(node_id=node_id, already_registered=False)


FIND_NODE_ATTEMPTS = 6
FIND_NODE_RETRY_S = 5.0
FIND_NODE_MAX_WAIT_S = 30.0  # bounds a portal Retry-After


def _node_gpus(row: dict) -> tuple[str | None, int | None]:
    """The node's GPU model and count: the declared fields, else what the validator measured (``executor_info``),
    as the portal's node table shows them. A node synced from a central miner has no declared fields."""
    info = (
        row.get("executor_info") if isinstance(row.get("executor_info"), dict) else {}
    )
    gpu_type = row.get("gpu_type") or info.get("machine_name") or None
    count = (
        info.get("gpu_count") or row.get("gpu_count") or None
    )  # measured inventory wins over a declared edit
    return (str(gpu_type) if gpu_type else None), (
        int(count) if str(count or "").isdigit() else None
    )


# The validator's names for one card (lium-io neurons/validators/src/services/gpu_spec_table.py NORMALIZATION_MAP):
# nvidia-smi on this host may say "NVIDIA A10" where the portal shows the canonical "NVIDIA A10 Tensor Core GPU".
_GPU_ALIASES = {
    "Tesla V100-SXM2-16GB": "NVIDIA Tesla V100 Tensor Core GPU",
    "Tesla V100-SXM2-32GB": "NVIDIA Tesla V100 Tensor Core GPU",
    "Tesla V100-PCIE-16GB": "NVIDIA Tesla V100 Tensor Core GPU",
    "Tesla V100-PCIE-32GB": "NVIDIA Tesla V100 Tensor Core GPU",
    "Tesla H100 80GB HBM3": "NVIDIA H100 80GB HBM3",
    "Tesla T4": "NVIDIA T4 Tensor Core GPU",
    "NVIDIA T4": "NVIDIA T4 Tensor Core GPU",
    "NVIDIA A10": "NVIDIA A10 Tensor Core GPU",
    "NVIDIA A10G": "NVIDIA A10 Tensor Core GPU",
}


def read_nvlink_pairs(run: Callable[[str], tuple[str, str]]) -> int | None:
    """NVLink pairs in ``nvidia-smi topo -m`` (``NV<n>`` cells, each pair shown twice); None if it cannot be read."""
    try:
        out, _ = run("nvidia-smi topo -m")
    except Exception:  # noqa: BLE001 - an unreadable topology only skips the adapter relabel, never the registration
        return None
    # matrix rows start with "GPU<n>"; the header row starts with whitespace
    rows = [ln for ln in out.splitlines() if ln.startswith("GPU")]
    return sum(len(re.findall(r"\bNV\d+\b", ln)) for ln in rows) // 2 if rows else None


# lium-io gpu_spec_table.MODDED_VRAM_MODELS: a memory-modded card reports its base card's name, and the validator lists
# it as its own model from the VRAM reading. Base name -> (modded name, base card's top MiB); any reading above the
# base card's window counts, since the validator's own reading can sit outside the window this host computes
_MODDED_GPU_MODELS = {
    "NVIDIA GeForce RTX 4090": ("NVIDIA GeForce RTX 4090 48GB", round(24576 * 1.05)),
    "NVIDIA GeForce RTX 4080": ("NVIDIA GeForce RTX 4080 32GB", round(16384 * 1.05)),
    "NVIDIA GeForce RTX 4080 SUPER": (
        "NVIDIA GeForce RTX 4080 SUPER 32GB",
        round(16384 * 1.05),
    ),
}


def _listed_gpu_model(
    gpu_type: str, gpu_count: int, nvlink_pairs: int | None, vram_mib: int | None = None
) -> str:
    """The name the validator lists this host under: an H100 SXM on PCIe adapter boards (several GPUs, no NVLink
    pair, lium-io gpu_spec_table.is_sxm_on_pcie_adapter) is listed as H100 PCIe; an HGX board keeps its name."""
    if (
        gpu_count > 1
        and nvlink_pairs == 0
        and _GPU_ALIASES.get(gpu_type.strip(), gpu_type.strip())
        == "NVIDIA H100 80GB HBM3"
    ):
        return "NVIDIA H100 PCIe"
    modded = _MODDED_GPU_MODELS.get(
        _GPU_ALIASES.get(gpu_type.strip(), gpu_type.strip())
    )
    if modded and vram_mib is not None and vram_mib > modded[1]:
        return modded[0]
    return gpu_type


def _same_gpu_model(a: str, b: str) -> bool:
    ca, cb = (_GPU_ALIASES.get(x.strip(), x.strip()) for x in (a, b))
    return ca == cb


def _is_another_host(row: dict, *, gpu_type: str, gpu_count: int) -> bool:
    """The portal's node at this address has other GPUs than this host (each known field compared on its own):
    a second machine behind the same IP, not a re-run on the same host."""
    # shortcut: two machines with the same model and count still read as a re-run; a host fingerprint the portal
    # returns would tell them apart if that case shows up.
    node_type, node_count = _node_gpus(row)
    return (node_type is not None and not _same_gpu_model(node_type, gpu_type)) or (
        node_count is not None and node_count != gpu_count
    )


def find_node_id_after_add(
    http: PortalHTTP,
    *,
    miner_hotkey: str,
    ip_address: str,
    port: int,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """The id of ``find_node_after_add``'s row."""
    row = find_node_after_add(
        http, miner_hotkey=miner_hotkey, ip_address=ip_address, port=port, sleep=sleep
    )
    return str(row.get("id")) if row is not None else None


def find_node_after_add(
    http: PortalHTTP,
    *,
    miner_hotkey: str,
    ip_address: str,
    port: int,
    sleep: Callable[[float], None] = time.sleep,
) -> dict | None:
    """``find_node`` with retries: the list can lag the write, and a portal hiccup must not fail an add that succeeded.

    A 429 that outlasts the retries is raised, never read as "not in the list" (DAH-5161)."""
    for attempt in range(FIND_NODE_ATTEMPTS):
        wait = FIND_NODE_RETRY_S
        try:
            row = find_node(
                http, miner_hotkey=miner_hotkey, ip_address=ip_address, port=port
            )
        except ProviderError as e:
            if e.code == PORTAL_RATE_LIMIT:
                if attempt == FIND_NODE_ATTEMPTS - 1:
                    raise
                wait = max(
                    wait,
                    min(
                        (e.context or {}).get("retry_after") or 0, FIND_NODE_MAX_WAIT_S
                    ),
                )
            row = None
        if row is not None:
            return row
        if attempt < FIND_NODE_ATTEMPTS - 1:
            sleep(wait)
    return None


def _known_gpu_types() -> list[str]:
    try:
        return sorted(fetch_shared_config().machine_prices)
    except ProviderError:
        return []


def find_node_id(
    http: PortalHTTP,
    *,
    miner_hotkey: str,
    ip_address: str,
    port: int,
    max_pages: int = 10,
) -> str | None:
    """The id of ``find_node``'s row, or None."""
    row = find_node(
        http,
        miner_hotkey=miner_hotkey,
        ip_address=ip_address,
        port=port,
        max_pages=max_pages,
    )
    return str(row.get("id")) if row is not None else None


def find_node(
    http: PortalHTTP,
    *,
    miner_hotkey: str,
    ip_address: str,
    port: int,
    max_pages: int = 10,
) -> dict | None:
    """The account's node at ``ip_address:port`` from ``GET /executors``, or None. The register token goes
    along: the portal blanks every node's address but its owner's."""
    for page in range(1, max_pages + 1):
        body = http.get(
            EXECUTORS,
            params={"miner_hotkey": miner_hotkey, "page": page, "limit": 100},
        )
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list) or not rows:
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("executor_ip_address")) == ip_address and str(
                row.get("executor_ip_port")
            ) == str(port):
                return row
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
    last_settled: NodeStatus | None = None  # the last reading that was not OFFLINE
    offline_since: float | None = (
        None  # clock at the first OFFLINE reading of the current run of them
    )
    offline_confirmed = (
        False  # an OFFLINE reading OFFLINE_CONFIRM_S or more after that first one
    )
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
            if current.listed or (
                current.needs_fix and (current.status != "OFFLINE" or offline_confirmed)
            ):
                return current
        elapsed = clock() - start
        # an OFFLINE first read inside the last minute of the wait gets its minute: the deadline stretches by
        # OFFLINE_CONFIRM_S at most, so a single stale reading is never reported as the fix
        pending_offline = offline_since is not None and not offline_confirmed
        if elapsed >= timeout_s and not (
            pending_offline and elapsed < timeout_s + OFFLINE_CONFIRM_S
        ):
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
    return PortalHTTP(
        base_url=portal_api_url or DEFAULT_PORTAL_URL, token_provider=lambda: token
    )


def read_gpu_inventory(run: Callable[[str], tuple[str, str]]) -> GpuInventory:
    out, _ = run(
        "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits"
    )
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


def result_summary(
    status: NodeStatus, *, node_url: str, waited_s: float
) -> tuple[str, int]:
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
        return (
            f"Could not read the node's status from the portal for {mins} min; the node stays registered: {node_url}",
            2,
        )
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
    "read_nvlink_pairs",
    "read_status",
    "register_node",
    "resolve_price",
    "result_summary",
    "status_line",
    "wait_until_listed",
]
