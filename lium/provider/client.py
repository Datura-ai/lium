"""Top-level façade for the provider SDK.

``ProviderClient`` is the single class consumed by ``lium/cli/provider/*`` (M2+)
and by external agents. It composes ``PortalHTTP``, ``TokenStore``, and a
``Signer`` (defaulting to ``LocalKeypairSigner``).

M1 ships:

- constructor + lazy authentication
- ``login()``  -- hotkey-signature -> JWT exchange (A2 wire format)
- ``logout()`` -- clears the local token cache
- ``whoami()`` -- ``GET /auth/me`` for session liveness
- ``status()`` -- aggregated portal/metagraph snapshot

M3 will add the executor lifecycle; M4 the SSH node-install; M5 the AC9 demo.
"""

from __future__ import annotations

import logging
import re as _re
from typing import Any

from lium.provider._routes import (
    BILLING,
    BILLING_BY_MINER,
    ESTIMATED_REWARDS,
    EXECUTOR_BY_ID,
    EXECUTOR_MACHINE_ADDED,
    EXECUTOR_MACHINE_REQUESTS,
    EXECUTOR_MIN_GPU_FOR_RENTAL,
    EXECUTOR_NOTICE_PERIOD,
    EXECUTOR_PODS,
    EXECUTORS,
    LOGIN_FLEXIBLE,
    MACHINE_REQUEST_BY_ID,
    MACHINE_REQUESTS,
    MACHINES,
    ME,
    MINER_OPT_IN,
    SET_EMAIL,
    SET_MACHINE_REQUEST_SUBSCRIPTION,
    SYNC_EXECUTOR_CENTRAL_MINER,
    SYNC_EXECUTOR_MINER_PORTAL,
    UPDATE_GPU,
    UPDATE_PRICE,
)
from lium.provider.auth import LocalKeypairSigner, Signer, build_login_payload
from lium.provider.errors import (
    ARG_INVALID,
    PORTAL_AUTH_INVALID,
    ProviderAuthError,
    ProviderError,
)
from lium.provider.models import (
    AddExecutorPayload,
    LoginResponse,
    NoticePeriodPayload,
    NotifyMachineAddedPayload,
    OptInStatusResponse,
    ProviderStatus,
    SafeProviderResponse,
    SetEmailPayload,
    SetMachineRequestSubscriptionPayload,
    SetMinGpuCountForRentalPayload,
    SetOptInRequest,
    UpdateGpuPayload,
    UpdatePricePayload,
    ValidatorWeight,
)
from lium.provider.portal_http import DEFAULT_PORTAL_URL, PortalHTTP
from lium.provider.token_store import CachedToken, TokenStore, with_refresh_retry

logger = logging.getLogger("lium.provider.client")


class ProviderClient:
    """Browserless mining onboarding client for SN51.

    Args:
        coldkey: bittensor coldkey name (used to load the wallet for hotkey
            signing). Only required when ``signer`` is None.
        hotkey: bittensor hotkey name. Required if ``signer`` is None.
        portal_url: portal origin; defaults to production.
        token_store: optional custom :class:`TokenStore`; tests inject one.
        http: optional custom :class:`PortalHTTP`; tests inject one.
        signer: optional custom :class:`Signer`. v1 default is
            ``LocalKeypairSigner`` constructed lazily from the wallet at
            ``~/.bittensor/wallets/<coldkey>/hotkeys/<hotkey>``.
    """

    def __init__(
        self,
        coldkey: str | None = None,
        hotkey: str | None = None,
        *,
        portal_url: str | None = None,
        token_store: TokenStore | None = None,
        http: PortalHTTP | None = None,
        signer: Signer | None = None,
    ) -> None:
        if signer is None and hotkey is None:
            raise ValueError("ProviderClient requires either signer= or hotkey=")
        self.coldkey = coldkey
        self.hotkey = hotkey
        self.portal_url = (portal_url or DEFAULT_PORTAL_URL).rstrip("/")
        self._signer: Signer | None = signer
        self._token_store = token_store or TokenStore()
        self._cached_token: CachedToken | None = None
        # ``http`` is built lazily so tests can inject a session and so we
        # don't open a connection pool until first request.
        self._http = http or PortalHTTP(
            base_url=self.portal_url,
            token_provider=self._current_token,
        )

    # ------------------------------------------------------------------
    # Public API

    @property
    def hotkey_ss58(self) -> str:
        """Return the hotkey ss58 address resolved via the active signer."""
        return self.signer.ss58_address

    @property
    def signer(self) -> Signer:
        """Lazily materialise the default :class:`Signer`."""
        if self._signer is None:
            self._signer = self._default_signer()
        return self._signer

    def login(self, *, force: bool = False) -> LoginResponse:
        """Exchange a hotkey signature for a JWT.

        If a non-expired token is cached for this hotkey and ``force`` is
        False, the cached value is returned without a network call.
        """
        signer = self.signer
        if not force:
            cached = with_refresh_retry(
                lambda: self._token_store.load(signer.ss58_address)
            )
            if cached is not None:
                self._cached_token = cached
                # We still need the SafeProviderResponse for callers; fetch
                # /auth/me lazily via ``whoami``. For the cached path we
                # synthesise a minimal LoginResponse with a hotkey-only provider
                # body so the contract is non-None.
                provider_body = SafeProviderResponse(
                    id=cached.provider_id or "",
                    miner_hotkey=signer.ss58_address,
                    provider_coldkey="",
                    created_at="",
                    updated_at="",
                )
                return LoginResponse(provider=provider_body, token=cached.token)

        payload = build_login_payload(signer)
        body = self._http.post(LOGIN_FLEXIBLE, json_body=payload, auth=False)
        try:
            response = LoginResponse.model_validate(body)
        except Exception as e:
            raise ProviderAuthError(
                "portal returned an unexpected login response shape",
                code=PORTAL_AUTH_INVALID,
                cause=e,
                context={"body": _summarise_body(body)},
            ) from e

        self._cached_token = with_refresh_retry(
            lambda: self._token_store.save(
                signer.ss58_address, response.token, provider_id=response.provider.id
            )
        )
        return response

    def logout(self) -> None:
        """Clear the local token cache for this hotkey.

        This is a client-side operation only; the portal does not expose a
        token-revocation endpoint as of the snapshot SHA. The cached entry
        is removed so the next call triggers a fresh login.
        """
        self._cached_token = None
        signer_addr: str | None
        try:
            signer_addr = self.signer.ss58_address
        except Exception:  # pragma: no cover - defensive
            signer_addr = None
        if signer_addr:
            with_refresh_retry(lambda: self._token_store.clear(signer_addr))

    def whoami(self) -> dict[str, Any]:
        """Call ``GET /auth/me`` and return the raw JSON body.

        Used by ``portal whoami`` (M2) and as a session liveness check by
        ``status``. Returns a dict so callers can introspect new fields
        without forcing a model bump.
        """
        return self._http.get(ME)

    def status(
        self,
        *,
        netuid: int = 51,
        metagraph_factory: object | None = None,
    ) -> ProviderStatus:
        """Aggregate registration / portal / executor / weights snapshot.

        ``status`` degrades gracefully: any individual source that raises is
        appended to ``warnings`` and other sources continue. AC8 is the only
        ``status`` consumer that currently requires the metagraph branch;
        portal-only flows can call ``status()`` and ignore weights.

        Args:
            netuid: subnet to inspect.
            metagraph_factory: optional callable returning a metagraph-like
                object with ``hotkeys`` (list[str]) and ``weights`` (dict or
                2D iterable). Used by tests to stub bittensor.
        """
        warnings: list[str] = []
        out = ProviderStatus(
            hotkey=self._safe_hotkey(),
            coldkey=self.coldkey,
            netuid=netuid,
        )

        # Portal / auth liveness.
        provider_id: str | None = None
        try:
            me_body = self.whoami()
            out.portal_session_active = True
            # /auth/me returns a flat dict: {provider_id, miner_hotkey, ...}.
            # Older / alternate shapes (nested {provider: {id: ...}}) are also
            # accepted so unit fixtures and any future portal rev land cleanly.
            if isinstance(me_body, dict):
                provider_id = me_body.get("provider_id") or me_body.get("id")
                if not provider_id:
                    nested = me_body.get("provider")
                    if isinstance(nested, dict):
                        provider_id = nested.get("id") or nested.get("provider_id")
        except ProviderAuthError as e:
            out.portal_session_active = False
            warnings.append(f"whoami: {e.code}")
        except ProviderError as e:
            out.portal_session_active = False
            warnings.append(f"whoami: {e.code}")
        out.provider_id = provider_id

        # Node list (skip silently if portal not authed).
        if out.portal_session_active:
            try:
                from lium.provider._routes import EXECUTORS
                from lium.provider.models import ExecutorInfo

                body = self._http.get(EXECUTORS)
                rows = body.get("data") if isinstance(body, dict) else body
                if not isinstance(rows, list):
                    rows = []
                nodes = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    try:
                        nodes.append(ExecutorInfo.model_validate(row))
                    except Exception:  # pragma: no cover - defensive
                        continue
                out.nodes = nodes
                out.node_count = len(nodes)
            except ProviderError as e:
                warnings.append(f"nodes: {e.code}")

        # Subnet registration + validator weights via metagraph.
        try:
            registered, weights = _read_metagraph(
                hotkey_ss58=out.hotkey,
                netuid=netuid,
                factory=metagraph_factory,
            )
            out.registered_on_subnet = registered
            out.validator_weights = weights
        except Exception as e:  # pragma: no cover - bittensor path
            warnings.append(f"metagraph: {type(e).__name__}")

        out.warnings = warnings
        return out

    def _safe_hotkey(self) -> str | None:
        try:
            return self.signer.ss58_address
        except Exception:  # pragma: no cover - defensive
            return None

    # ------------------------------------------------------------------
    # Profile / configuration
    #
    # Path-segment validators below: every public method that interpolates a
    # caller-supplied string into a portal URL runs that string through
    # ``_safe_id`` / ``_safe_hotkey_segment``. Catches poisoned env vars or
    # stray ``..`` segments before they hit the wire.

    def set_email(self, email: str) -> dict[str, Any]:
        """``POST /auth/set-email`` -- update the provider's contact email.

        Returns the raw envelope from the portal so callers can read the
        refreshed ``DetailResponse`` shape.
        """
        payload = _build_payload(SetEmailPayload, email=email)
        return self._http.post(SET_EMAIL, json_body=payload)

    def set_machine_request_subscription(self, gpu_types: list[str]) -> dict[str, Any]:
        """``POST /auth/set-machine-request-subscription``.

        Sets the list of GPU types the provider wants notifications about.
        """
        payload = _build_payload(
            SetMachineRequestSubscriptionPayload,
            machine_request_subscription=list(gpu_types),
        )
        return self._http.post(SET_MACHINE_REQUEST_SUBSCRIPTION, json_body=payload)

    def set_opt_in_status(self, opt_in: bool) -> OptInStatusResponse:
        """``POST /miners/opt-in`` -- toggle the lium.io central miner server.

        ``True`` opts the provider in (use lium.io's central miner), ``False``
        opts back out (provider runs their own central miner).
        """
        payload = _build_payload(SetOptInRequest, opt_in_status=opt_in)
        body = self._http.post(MINER_OPT_IN, json_body=payload)
        try:
            return OptInStatusResponse.model_validate(body)
        except Exception as e:
            raise ProviderError(
                "portal returned an unexpected opt-in response shape",
                code="PORTAL_CONTRACT_DRIFT",
                cause=e,
                context={"body": _summarise_body(body)},
            ) from e

    # ------------------------------------------------------------------
    # Node (executor) lifecycle
    #
    # User-facing terminology is "node". The HTTP route paths the portal
    # exposes still use ``/executors/...`` -- those route constants and
    # backend-DTO mirror models keep their original names since they
    # literally mirror the on-the-wire protocol.

    def list_nodes(
        self,
        *,
        miner_hotkey: str | None = None,
        page: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """``GET /executors`` -- paginated node list.

        Returns the raw envelope (``{data: [...], total, page, limit}``) so
        list callers can read pagination metadata.
        """
        params: dict[str, Any] = {}
        if miner_hotkey is not None:
            params["miner_hotkey"] = _safe_hotkey_segment(miner_hotkey)
        if page is not None:
            params["page"] = page
        if limit is not None:
            params["limit"] = limit
        return self._http.get(EXECUTORS, params=params or None)

    def get_node(self, node_id: str) -> dict[str, Any]:
        """``GET /executors/{id}`` -- fetch one node's full record."""
        return self._http.get(
            EXECUTOR_BY_ID.format(id=_safe_id(node_id, label="node_id"))
        )

    def add_node(
        self,
        *,
        gpu_type: str,
        ip_address: str,
        port: int,
        price_per_gpu: float,
        gpu_count: int,
    ) -> dict[str, Any]:
        """``POST /executors`` -- queue an add-node request to the miner.

        The portal returns a queued message; the actual add happens via the
        miner WebSocket consumer. Callers typically poll ``list_nodes``
        afterwards.
        """
        payload = _build_payload(
            AddExecutorPayload,
            gpu_type=gpu_type,
            ip_address=ip_address,
            port=port,
            price_per_gpu=price_per_gpu,
            gpu_count=gpu_count,
        )
        return self._http.post(EXECUTORS, json_body=payload)

    def delete_node(self, node_id: str) -> dict[str, Any]:
        """``DELETE /executors/{id}`` -- remove a node."""
        return self._http.delete(
            EXECUTOR_BY_ID.format(id=_safe_id(node_id, label="node_id"))
        )

    def update_node_price(self, node_id: str, price_per_gpu: float) -> dict[str, Any]:
        """``POST /executors/{id}/update-price``."""
        payload = _build_payload(UpdatePricePayload, price_per_gpu=price_per_gpu)
        return self._http.post(
            UPDATE_PRICE.format(id=_safe_id(node_id, label="node_id")),
            json_body=payload,
        )

    def update_node_gpu(
        self, node_id: str, *, gpu_type: str, gpu_count: int
    ) -> dict[str, Any]:
        """``POST /executors/{id}/update-gpu``."""
        payload = _build_payload(
            UpdateGpuPayload, gpu_type=gpu_type, gpu_count=gpu_count
        )
        return self._http.post(
            UPDATE_GPU.format(id=_safe_id(node_id, label="node_id")),
            json_body=payload,
        )

    def set_min_gpu_for_rental(self, node_id: str, min_count: int) -> dict[str, Any]:
        """``POST /executors/{id}/min-gpu-count-for-rental``."""
        payload = _build_payload(
            SetMinGpuCountForRentalPayload, min_gpu_count_for_rental=min_count
        )
        return self._http.post(
            EXECUTOR_MIN_GPU_FOR_RENTAL.format(
                id=_safe_id(node_id, label="node_id")
            ),
            json_body=payload,
        )

    def unset_min_gpu_for_rental(self, node_id: str) -> dict[str, Any]:
        """``DELETE /executors/{id}/min-gpu-count-for-rental``."""
        return self._http.delete(
            EXECUTOR_MIN_GPU_FOR_RENTAL.format(
                id=_safe_id(node_id, label="node_id")
            )
        )

    def node_pods(self, node_id: str) -> dict[str, Any]:
        """``GET /executors/{id}/pods`` -- rented pods on a node."""
        return self._http.get(
            EXECUTOR_PODS.format(id=_safe_id(node_id, label="node_id"))
        )

    def node_machine_requests(self, node_id: str) -> dict[str, Any]:
        """``GET /executors/{id}/machine-requests``."""
        return self._http.get(
            EXECUTOR_MACHINE_REQUESTS.format(id=_safe_id(node_id, label="node_id"))
        )

    def create_notice_period(
        self, node_id: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """``POST /executors/{id}/notice-period``."""
        try:
            body = NoticePeriodPayload.model_validate(payload or {}).model_dump()
        except Exception as e:
            raise ProviderError(
                f"invalid notice-period payload: {e}",
                code=ARG_INVALID,
                cause=e,
            ) from e
        return self._http.post(
            EXECUTOR_NOTICE_PERIOD.format(id=_safe_id(node_id, label="node_id")),
            json_body=body,
        )

    def delete_notice_period(self, node_id: str) -> dict[str, Any]:
        """``DELETE /executors/{id}/notice-period``."""
        return self._http.delete(
            EXECUTOR_NOTICE_PERIOD.format(id=_safe_id(node_id, label="node_id"))
        )

    def notify_machine_added(
        self, node_id: str, machine_request_id: str
    ) -> dict[str, Any]:
        """``POST /executors/{id}/machine-added``."""
        payload = _build_payload(
            NotifyMachineAddedPayload, machine_request_id=machine_request_id
        )
        return self._http.post(
            EXECUTOR_MACHINE_ADDED.format(id=_safe_id(node_id, label="node_id")),
            json_body=payload,
        )

    # ------------------------------------------------------------------
    # Sync (batch) operations
    #
    # SDK method names match the user-facing CLI verbs (``sync from-/to-
    # miner-server``). The HTTP route names retain the legacy "executor"
    # spelling and are intentionally NOT re-exposed in method names; the
    # mapping is:
    #
    #   CLI verb                  | SDK method                   | HTTP route                              | frontend button label
    #   ------------------------- | ---------------------------- | --------------------------------------- | ----------------------
    #   sync from-miner-server    | sync_nodes_from_miner_server | /executors/sync-executor-central-miner  | "Sync From Miner Server"
    #   sync to-miner-server      | sync_nodes_to_miner_server   | /executors/sync-executor-miner-portal   | "Sync Into Miner Server"

    def sync_nodes_from_miner_server(self) -> dict[str, Any]:
        """Pull node state from the central miner server (frontend label 'Sync From Miner Server')."""
        return self._http.post(SYNC_EXECUTOR_CENTRAL_MINER)

    def sync_nodes_to_miner_server(self) -> dict[str, Any]:
        """Push node state to the central miner server (frontend label 'Sync Into Miner Server')."""
        return self._http.post(SYNC_EXECUTOR_MINER_PORTAL)

    # ------------------------------------------------------------------
    # Read-only queries

    def billing_history(
        self,
        *,
        miner_hotkey: str | None = None,
        page: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """``GET /billing`` (paginated) or ``/billing/{miner_hotkey}``."""
        if miner_hotkey is not None and page is None and limit is None:
            return self._http.get(
                BILLING_BY_MINER.format(miner_hotkey=_safe_hotkey_segment(miner_hotkey))
            )
        params: dict[str, Any] = {}
        if miner_hotkey is not None:
            params["miner_hotkey"] = _safe_hotkey_segment(miner_hotkey)
        if page is not None:
            params["page"] = page
        if limit is not None:
            params["limit"] = limit
        return self._http.get(BILLING, params=params or None)

    def list_machine_requests(self) -> dict[str, Any]:
        """``GET /machine-requests`` -- pending tenant capacity asks."""
        return self._http.get(MACHINE_REQUESTS)

    def get_machine_request(self, request_id: str) -> dict[str, Any]:
        """``GET /machine-requests/{request_id}``."""
        return self._http.get(
            MACHINE_REQUEST_BY_ID.format(
                request_id=_safe_id(request_id, label="request_id")
            )
        )

    def list_machines(self) -> dict[str, Any]:
        """``GET /machines`` -- catalogue of GPU machine types."""
        return self._http.get(MACHINES)

    def estimated_rewards(self, **params: Any) -> dict[str, Any]:
        """``GET /machines/estimated-rewards`` with arbitrary query params."""
        return self._http.get(ESTIMATED_REWARDS, params=params or None)

    # ------------------------------------------------------------------
    # Internals

    def _current_token(self) -> str | None:
        """Token provider for ``PortalHTTP``: returns cached token or None.

        ``PortalHTTP`` invokes this on every request. We only return a
        token when one is cached AND not expired -- expired tokens are
        treated as "no token" so the caller gets a clean 401 path.
        """
        if self._cached_token is not None and not self._cached_token.expired():
            return self._cached_token.token
        # Try a cache load without a network round-trip. We need the signer's
        # ss58 to key the cache; materialise it lazily via the property so a
        # fresh ``ProviderClient`` (e.g. for ``whoami``) can pick up a cached
        # token from a previous ``login`` invocation. If the wallet isn't on
        # disk we can't derive ss58 -- return None and let the caller see a
        # clean 401.
        try:
            ss58 = self.signer.ss58_address
        except Exception:
            return None
        try:
            cached = with_refresh_retry(lambda: self._token_store.load(ss58))
        except Exception:
            return None
        if cached is None:
            return None
        self._cached_token = cached
        return cached.token

    def _default_signer(self) -> Signer:
        """Build ``LocalKeypairSigner`` from ``coldkey`` + ``hotkey``."""
        if self.coldkey is None or self.hotkey is None:
            raise ValueError(
                "ProviderClient(coldkey, hotkey) is required when signer= is not provided"
            )
        # Lazy import to avoid pulling bittensor at import time.
        from lium.provider.wallet import load_hotkey_keypair

        keypair = load_hotkey_keypair(self.coldkey, self.hotkey)
        return LocalKeypairSigner(keypair)


# Path-segment validators. The goal is defense-in-depth against URL-path
# injection (poisoned env var, agent feeding stray ``/`` or ``..``) -- NOT
# semantic validation of the value's format. The portal remains the
# authority on what's a valid hotkey / executor UUID.
#
# Rejects: ``/``, ``?``, ``#``, ``%``, whitespace, anything outside the
# explicit allow-list. Also rejects strings made entirely of dots
# (``.``, ``..``, ...) since those are path-traversal segments.

_SAFE_ID = _re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _reject_dot_only(value: str) -> bool:
    return bool(value) and all(ch == "." for ch in value)


def _safe_id(value: str, *, label: str) -> str:
    """Validate a path segment (UUID, request id, hotkey, etc.).

    Permits alphanumerics plus ``.-_`` up to 128 chars. Rejects the
    all-dots strings (``.``, ``..``) that the regex would otherwise accept.
    """
    if (
        not isinstance(value, str)
        or not _SAFE_ID.match(value)
        or _reject_dot_only(value)
    ):
        raise ProviderError(
            f"invalid {label}: {value!r}",
            code=ARG_INVALID,
            hint="Path segments must be alphanumeric (or .-_), <=128 chars.",
        )
    return value


def _safe_hotkey_segment(value: str, *, label: str = "miner_hotkey") -> str:
    """Validate a hotkey going into a URL path (same constraints as ``_safe_id``)."""
    return _safe_id(value, label=label)


def _build_payload(model_class, /, **kwargs) -> dict[str, Any]:
    """Construct a Pydantic payload model and serialise it.

    Converts a ``pydantic.ValidationError`` into ``ProviderError(ARG_INVALID)``
    with a flattened, human-readable hint so a CLI/agent caller sees a clean
    structured error instead of a Pydantic traceback.
    """
    try:
        return model_class(**kwargs).model_dump()
    except Exception as e:
        # Avoid importing pydantic here; ValidationError exposes ``.errors()``
        # but we only need the human-readable str() form.
        raise ProviderError(
            f"invalid payload for {model_class.__name__}: {e}",
            code=ARG_INVALID,
            cause=e,
            hint="Check field constraints: gpu_count >= 1, port 1-65535, valid email, etc.",
        ) from e


def _summarise_body(body: Any, *, max_chars: int = 240) -> str:
    """Truncate a response body for inclusion in error context."""
    text = repr(body)
    return text if len(text) <= max_chars else text[:max_chars] + "..."


def _read_metagraph(
    *,
    hotkey_ss58: str | None,
    netuid: int,
    factory: object | None = None,
) -> tuple[bool | None, list[ValidatorWeight]]:
    """Best-effort read of registration + validator weights from metagraph.

    ``factory`` is a callable returning an object exposing ``hotkeys`` (list)
    and ``W`` (NxN weight matrix) -- the shape ``bittensor.metagraph(netuid)``
    yields. Tests inject a stub; the lazy import below keeps bittensor out of
    the M1/M2 hot path for unit tests that never touch this branch.
    """
    if hotkey_ss58 is None:
        return None, []
    if factory is None:
        try:
            import bittensor  # type: ignore[import-not-found]
        except ImportError:
            return None, []
        factory = bittensor.metagraph

    metagraph = factory(netuid=netuid) if callable(factory) else factory
    hotkeys = list(getattr(metagraph, "hotkeys", []) or [])
    if hotkey_ss58 not in hotkeys:
        return False, []
    provider_uid = hotkeys.index(hotkey_ss58)

    weights_attr = (
        getattr(metagraph, "W", None) or getattr(metagraph, "weights", None) or []
    )
    rows: list[ValidatorWeight] = []
    try:
        for v_uid, row in enumerate(weights_attr):
            if v_uid >= len(hotkeys):
                break
            try:
                weight_value = float(row[provider_uid])
            except (TypeError, ValueError, IndexError):
                continue
            if weight_value > 0:
                rows.append(
                    ValidatorWeight(
                        validator_hotkey=hotkeys[v_uid],
                        weight=weight_value,
                    )
                )
    except TypeError:
        # Non-iterable weights attribute -- treat as no data.
        return True, []
    return True, rows


__all__ = ["ProviderClient"]
