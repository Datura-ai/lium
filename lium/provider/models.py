"""Hand-written Pydantic models for the provider SDK.

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

from lium.provider.errors import PORTS_INVALID, ProviderError

# --- Auth ---------------------------------------------------------------


class SafeProviderResponse(BaseModel):
    """Mirrors ``lium-miner-portal/src/dtos/miner.py::SafeMinerResponse``.

    The wire still uses protocol-level ``miner_coldkey``; the user-facing
    rename ("Miner" -> "Provider") landed in the CLI/SDK but not the
    portal payload. Both names are accepted here and exposed on the model
    as ``provider_coldkey``.
    """

    model_config = {"populate_by_name": True}

    id: str
    miner_hotkey: str
    provider_coldkey: str = Field(alias="miner_coldkey")
    email: str | None = None
    discord_id: str | None = None
    machine_request_subscription: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


class LoginResponse(BaseModel):
    """Mirrors ``LoginResponse`` returned by ``POST /auth/login-flexible``.

    The wire field is ``miner`` (protocol-level naming); we expose it as
    ``provider`` to match the user-facing rename. ``populate_by_name``
    accepts either.
    """

    model_config = {"populate_by_name": True}

    provider: SafeProviderResponse = Field(alias="miner")
    token: str


class ProviderCredentials(BaseModel):
    """Wire payload for ``POST /auth/login-flexible``.

    The portal expects ``signature`` as hex *without* the ``0x`` prefix; the
    portal code adds it before calling ``verify_miner_signature``.
    """

    miner_hotkey: str
    message: str
    signature: str


class SetPasswordPayload(ProviderCredentials):
    """Wire payload for ``POST /auth/set-password``."""

    new_password: str = Field(min_length=8, max_length=128)


# --- Executors ----------------------------------------------------------


class AddExecutorPayload(BaseModel):
    """Mirrors ``lium-miner-portal/src/dtos/executor.py::AddExecutorPayload``.

    Field constraints mirror the portal's own validators so the CLI rejects
    a bad request before the round-trip.
    """

    gpu_type: str = Field(min_length=1, max_length=64)
    ip_address: str = Field(min_length=1, max_length=64)
    port: int = Field(ge=1, le=65535)
    price_per_gpu: float = Field(ge=0)
    gpu_count: int = Field(ge=1, le=64)


class UpdatePricePayload(BaseModel):
    price_per_gpu: float = Field(ge=0)


class UpdateGpuPayload(BaseModel):
    gpu_type: str = Field(min_length=1, max_length=64)
    gpu_count: int = Field(ge=1, le=64)


class SetMinGpuCountForRentalPayload(BaseModel):
    """Payload for ``POST /executors/{id}/min-gpu-count-for-rental``."""

    min_gpu_count_for_rental: int = Field(ge=1, le=64)


class NoticePeriodPayload(BaseModel):
    """Payload for ``POST /executors/{id}/notice-period``.

    The portal accepts an empty body today; we forbid extras so a future
    portal change requires an SDK model bump rather than silently honouring
    smuggled keys from a caller dict.
    """

    model_config = {"extra": "forbid"}


class NotifyMachineAddedPayload(BaseModel):
    """Payload for ``POST /executors/{id}/machine-added``."""

    machine_request_id: str = Field(min_length=1, max_length=128)


class SetEmailPayload(BaseModel):
    """Payload for ``POST /auth/set-email``.

    Light syntactic check; the portal performs full RFC-5322 validation.
    """

    email: str = Field(
        min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    )


class SetMachineRequestSubscriptionPayload(BaseModel):
    """Payload for ``POST /auth/set-machine-request-subscription``."""

    machine_request_subscription: list[str] = Field(default_factory=list)


class SetOptInRequest(BaseModel):
    """Payload for ``POST /miners/opt-in``."""

    opt_in_status: bool


class OptInStatusResponse(BaseModel):
    """Mirrors ``lium-miner-portal/src/dtos/miner.py::OptInStatusResponse``."""

    miner_hotkey: str
    miner_coldkey: str
    central_miner_ip: str
    central_miner_port: int


class ExecutorInfo(BaseModel):
    """A reduced view of ``ExecutorResponse`` used by ``ProviderClient``.

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
    """One row of validator -> provider weight read from the metagraph."""

    validator_hotkey: str
    weight: float


class ProviderStatus(BaseModel):
    """Composite output of ``lium provider status``.

    All fields are optional because ``status`` degrades gracefully when one
    source (subtensor / portal / SSH) is unavailable.
    """

    hotkey: str | None = None
    coldkey: str | None = None
    registered_on_subnet: bool | None = None
    netuid: int | None = None
    portal_session_active: bool | None = None
    provider_id: str | None = None
    discord_connected: bool | None = None
    extra_incentive_eligible: bool | None = None
    node_count: int | None = None
    nodes: list[ExecutorInfo] = Field(default_factory=list)
    validator_weights: list[ValidatorWeight] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class NodeInstallResult(BaseModel):
    """Outcome of ``lium provider node install``."""

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
    """Port specification for ``lium provider node install --ports``.

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

        Raises ``ProviderError(code=PORTS_INVALID)`` on any parse failure.
        Empty / ``None`` input returns the default ``NodePorts()``.
        """
        if raw is None or raw.strip() == "":
            return cls()
        kwargs: dict[str, Any] = {}
        for part in raw.split(","):
            match = _PORT_FIELD_RE.match(part)
            if not match:
                raise ProviderError(
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
                raise ProviderError(
                    f"invalid port value in segment {part!r}: {e}",
                    code=PORTS_INVALID,
                ) from e
        try:
            return cls(**kwargs)
        except Exception as e:  # pydantic ValidationError or our ValueError
            raise ProviderError(
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
    "NodeInstallResult",
    "NodePorts",
    "NoticePeriodPayload",
    "NotifyMachineAddedPayload",
    "OptInStatusResponse",
    "ProviderCredentials",
    "ProviderStatus",
    "SafeProviderResponse",
    "SetEmailPayload",
    "SetMachineRequestSubscriptionPayload",
    "SetMinGpuCountForRentalPayload",
    "SetOptInRequest",
    "UpdateGpuPayload",
    "UpdatePricePayload",
    "ValidatorWeight",
]
