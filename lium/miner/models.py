"""Hand-written Pydantic models for the miner SDK.

Field names mirror ``lium-miner-portal/src/dtos/*.py``. Portal payload drift
surfaces at runtime as ``PORTAL_CONTRACT_DRIFT``.

Vendoring portal DTOs was rejected (A10): the portal modules import
SQLModel/FastAPI-bound ORM models, which would force those frameworks into
the renter-distributed CLI runtime.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

from lium.miner.errors import PORTS_INVALID, MinerError

# --- Auth ---------------------------------------------------------------


class SafeMinerResponse(BaseModel):
    """Mirrors ``lium-miner-portal/src/dtos/miner.py::SafeMinerResponse``."""

    id: str
    miner_hotkey: str
    miner_coldkey: str
    email: str | None = None
    machine_request_subscription: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


class LoginResponse(BaseModel):
    """Mirrors ``LoginResponse`` returned by ``POST /auth/login-flexible``."""

    miner: SafeMinerResponse
    token: str


class MinerCredentials(BaseModel):
    """Wire payload for ``POST /auth/login-flexible``.

    The portal expects ``signature`` as hex *without* the ``0x`` prefix; the
    portal code adds it before calling ``verify_miner_signature``.
    """

    miner_hotkey: str
    message: str
    signature: str


# --- Executors ----------------------------------------------------------


class AddExecutorPayload(BaseModel):
    """Mirrors ``lium-miner-portal/src/dtos/executor.py::AddExecutorPayload``."""

    gpu_type: str
    ip_address: str
    port: int
    price_per_gpu: float
    gpu_count: int


class UpdatePricePayload(BaseModel):
    price_per_gpu: float


class UpdateGpuPayload(BaseModel):
    gpu_type: str
    gpu_count: int


class ExecutorInfo(BaseModel):
    """A reduced view of ``ExecutorResponse`` used by ``MinerClient``.

    Only the fields the SDK actually surfaces are typed strictly; everything
    else is stashed under ``extra`` so a portal field rename does not blow
    up parsing of unrelated views.
    """

    model_config = {"extra": "allow"}

    id: str
    gpu_type: str | None = None
    gpu_count: int | None = None
    executor_ip_address: str | None = None
    executor_ip_port: str | None = None
    price_per_gpu: float | None = None
    validator_hotkey: str | None = None
    miner_hotkey: str | None = None
    rented: bool | None = None


# --- Status / runtime models -------------------------------------------


class ValidatorWeight(BaseModel):
    """One row of validator -> miner weight read from the metagraph."""

    validator_hotkey: str
    weight: float


class MinerStatus(BaseModel):
    """Composite output of ``lium miner status``.

    All fields are optional because ``status`` degrades gracefully when one
    source (subtensor / portal / SSH) is unavailable.
    """

    hotkey: str | None = None
    coldkey: str | None = None
    registered_on_subnet: bool | None = None
    netuid: int | None = None
    portal_session_active: bool | None = None
    miner_id: str | None = None
    executor_count: int | None = None
    executors: list[ExecutorInfo] = Field(default_factory=list)
    validator_weights: list[ValidatorWeight] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class NodeInstallResult(BaseModel):
    """Outcome of ``lium miner node install``."""

    host: str
    executor_uuid: str | None = None
    log_path: str = "/tmp/lium-mine.log"
    exit_code: int | None = None


# --- NodePorts (A11) ----------------------------------------------------

# Accepts: HTTP=8080,SSH=2200,RANGE=2000-2005   (case-insensitive keys)
_PORT_FIELD_RE = re.compile(
    r"^\s*(?P<key>HTTP|SSH|RANGE)\s*=\s*(?P<value>[\d\-]+)\s*$",
    re.IGNORECASE,
)


class NodePorts(BaseModel):
    """Port specification for ``lium miner node install --ports``.

    The CLI parses the user input via ``NodePorts.from_string`` so the SDK is
    the single source of truth for port-spec parsing (Principle 1, A11).
    """

    http: int = 8080
    ssh: int = 2200
    range_lo: int = 2000
    range_hi: int = 2005

    @field_validator("http", "ssh", "range_lo", "range_hi")
    @classmethod
    def _check_positive(cls, v: int) -> int:
        if v <= 0 or v > 65535:
            raise ValueError(f"port out of range: {v}")
        return v

    @classmethod
    def from_string(cls, raw: str | None) -> "NodePorts":
        """Parse ``HTTP=8080,SSH=2200,RANGE=2000-2005`` into ``NodePorts``.

        Raises ``MinerError(code=PORTS_INVALID)`` on any parse failure.
        Empty / ``None`` input returns the default ``NodePorts()``.
        """
        if raw is None or raw.strip() == "":
            return cls()
        kwargs: dict[str, Any] = {}
        for part in raw.split(","):
            match = _PORT_FIELD_RE.match(part)
            if not match:
                raise MinerError(
                    f"could not parse port spec segment: {part!r}",
                    code=PORTS_INVALID,
                )
            key = match.group("key").upper()
            value = match.group("value")
            try:
                if key == "HTTP":
                    kwargs["http"] = int(value)
                elif key == "SSH":
                    kwargs["ssh"] = int(value)
                elif key == "RANGE":
                    if "-" not in value:
                        raise ValueError("RANGE must be lo-hi")
                    lo_s, hi_s = value.split("-", 1)
                    lo, hi = int(lo_s), int(hi_s)
                    if lo > hi:
                        raise ValueError("RANGE lo must be <= hi")
                    kwargs["range_lo"] = lo
                    kwargs["range_hi"] = hi
            except ValueError as e:
                raise MinerError(
                    f"invalid port value in segment {part!r}: {e}",
                    code=PORTS_INVALID,
                ) from e
        try:
            return cls(**kwargs)
        except Exception as e:  # pydantic ValidationError or our ValueError
            raise MinerError(
                f"invalid ports: {e}",
                code=PORTS_INVALID,
                cause=e if isinstance(e, BaseException) else None,
            ) from e

    def to_install_args(self) -> list[str]:
        """Render as `mine.sh` args (``--http-port``, ``--ssh-port``, ``--range``)."""
        return [
            "--http-port",
            str(self.http),
            "--ssh-port",
            str(self.ssh),
            "--port-range",
            f"{self.range_lo}-{self.range_hi}",
        ]


__all__ = [
    "AddExecutorPayload",
    "ExecutorInfo",
    "LoginResponse",
    "MinerCredentials",
    "MinerStatus",
    "NodeInstallResult",
    "NodePorts",
    "SafeMinerResponse",
    "UpdateGpuPayload",
    "UpdatePricePayload",
    "ValidatorWeight",
]
