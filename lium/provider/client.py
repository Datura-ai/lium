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
from typing import Any

from lium.provider._routes import LOGIN_FLEXIBLE, ME
from lium.provider.auth import LocalKeypairSigner, Signer, build_login_payload
from lium.provider.errors import (
    PORTAL_AUTH_INVALID,
    ProviderAuthError,
    ProviderError,
)
from lium.provider.models import (
    LoginResponse,
    ProviderStatus,
    SafeProviderResponse,
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

        # Executor list (skip silently if portal not authed).
        if out.portal_session_active:
            try:
                from lium.provider._routes import EXECUTORS
                from lium.provider.models import ExecutorInfo

                body = self._http.get(EXECUTORS)
                rows = body.get("data") if isinstance(body, dict) else body
                if not isinstance(rows, list):
                    rows = []
                executors = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    try:
                        executors.append(ExecutorInfo.model_validate(row))
                    except Exception:  # pragma: no cover - defensive
                        continue
                out.executors = executors
                out.executor_count = len(executors)
            except ProviderError as e:
                warnings.append(f"executors: {e.code}")

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
            cached = with_refresh_retry(
                lambda: self._token_store.load(ss58)
            )
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
