"""API keys: scopes, budgets and pod visibility (lium-platform DAH-2944 scopes; P235 budgets and visibility).

Every ``/keys`` route is session-only on the server (``utils/auth.py``: ``authenticate``, a browser JWT): a key
cannot list, mint or reshape keys, so these calls need ``Lium.workspaces.login`` or LIUM_SESSION_TOKEN and raise
:class:`LiumSessionError` without one. ``GET /keys/scopes`` is the one read that also answers a key.

``scopes()`` is the single source of the "what this key can do" words: the CLI prints the server's
descriptions and never its own copy.
"""

from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional

from .exceptions import LiumError
from .models import ApiKeyInfo, ApiKeyScope

if TYPE_CHECKING:  # pragma: no cover
    from .client import Lium

SCOPES_ROUTE = "/keys/scopes"
# What a key gets when the caller names no scope: everything but ``billing``. The server's own default for an
# omitted ``scopes`` is "every scope" (DAH-2944), which would hand a new key the money routes once ``billing``
# exists — so the list is always sent (owner, 21 Sep 2026: the elevated key is off by default).
DEFAULT_SCOPES = ("read", "rent", "manage")
BILLING_SCOPE = "billing"
POD_VISIBILITIES = ("own", "account")
BUDGET_EXCEEDED_CODE = "API_KEY_BUDGET_EXCEEDED"


def _usd(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _key(d: Dict[str, Any]) -> ApiKeyInfo:
    scopes = d.get("scopes")
    visibility = d.get("pod_visibility")
    return ApiKeyInfo(
        id=str(d.get("id", "")),
        name=str(d.get("name", "")),
        scopes=[str(s) for s in scopes] if isinstance(scopes, list) else [],
        created_at=d.get("created_at"),
        last_used=d.get("last_used"),
        workspace_id=d.get("workspace_id"),
        daily_budget_usd=_usd(d.get("daily_budget_usd")),
        max_budget_usd=_usd(d.get("max_budget_usd")),
        spent_today_usd=_usd(d.get("spent_today_usd")),
        spent_total_usd=_usd(d.get("spent_total_usd")),
        pod_visibility=visibility if isinstance(visibility, str) and visibility else None,
        key=d.get("key") if isinstance(d.get("key"), str) else None,
        raw=dict(d),
    )


def _scope(d: Dict[str, Any]) -> ApiKeyScope:
    families = d.get("route_families")
    return ApiKeyScope(
        scope=str(d.get("scope", "")),
        description=str(d.get("description", "")),
        route_families=[str(f) for f in families] if isinstance(families, list) else [],
    )


def _budget(name: str, value: Optional[float]) -> Optional[float]:
    """A budget as the number the API takes: a positive USD amount, or None for no budget."""
    if value is None:
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number of USD, not {value!r}")
    if amount <= 0:
        raise ValueError(f"{name} must be more than $0 ({amount} given); leave it out for no budget")
    return amount


class ApiKeysClient:
    def __init__(self, lium: "Lium"):
        self._lium = lium

    # ------------------------------------------------------------------ scopes (key or session)
    def scopes(self) -> List[ApiKeyScope]:
        """Every scope the server knows, with its description and route families (``GET /keys/scopes``).

        Reads with the session when there is one, else with the key. A server before P235 has no such
        route and answers 404 (:class:`LiumNotFoundError`).
        """
        response = self._lium.workspaces._read(SCOPES_ROUTE)
        data = response.json()
        rows = data.get("scopes") if isinstance(data, dict) else data
        return [_scope(row) for row in rows] if isinstance(rows, list) else []

    # ------------------------------------------------------------------ reads (session)
    def list(self, workspace_id: Optional[str] = None) -> List[ApiKeyInfo]:
        """The keys of a workspace (``GET /keys``, ``X-Lium-Workspace-Id`` when given) — never the secrets."""
        rows = self._lium.workspaces._session_request("GET", "/keys", workspace_id).json()
        return [_key(row) for row in rows] if isinstance(rows, list) else []

    def get(self, key_id: str, workspace_id: Optional[str] = None) -> ApiKeyInfo:
        """One key by id (``GET /keys/{id}``)."""
        return _key(self._lium.workspaces._session_request("GET", f"/keys/{key_id}", workspace_id).json())

    def resolve(self, name_or_id: str, workspace_id: Optional[str] = None) -> ApiKeyInfo:
        """A key by name (case-insensitive) or id among the workspace's keys.

        Names are not unique on the server: two keys with that name is an error that names both ids.
        """
        found = [key for key in self.list(workspace_id) if key.matches(name_or_id)]
        if len(found) > 1:
            ids = ", ".join(key.id for key in found)
            raise LiumError(f"{len(found)} API keys are named '{name_or_id}' ({ids}); use the id")
        if not found:
            raise LiumError(f"No API key named '{name_or_id}' in this workspace; `lium keys list` shows them")
        return found[0]

    # ------------------------------------------------------------------ writes (session)
    def create(
        self,
        name: str,
        scopes: Optional[Iterable[str]] = None,
        *,
        daily_budget_usd: Optional[float] = None,
        max_budget_usd: Optional[float] = None,
        pod_visibility: str = "own",
        workspace_id: Optional[str] = None,
    ) -> ApiKeyInfo:
        """Mint a key (``POST /keys``); the secret is in the returned ``key`` this once.

        ``scopes`` defaults to :data:`DEFAULT_SCOPES` (``read``, ``rent``, ``manage``) and is always sent, so
        ``billing`` — the money routes — is on a key only when named. ``daily_budget_usd`` / ``max_budget_usd``
        are sent as numbers (USD, > 0) only when given; ``pod_visibility`` is ``own`` (the key lists only the
        pods it rents) or ``account``. A server before P235 ignores the budget and visibility fields.
        """
        if pod_visibility not in POD_VISIBILITIES:
            raise ValueError(f"pod_visibility must be one of {', '.join(POD_VISIBILITIES)}, not {pod_visibility!r}")
        body: Dict[str, Any] = {
            "name": name,
            "scopes": list(scopes) if scopes is not None else list(DEFAULT_SCOPES),
            "pod_visibility": pod_visibility,
        }
        if not body["scopes"]:
            raise ValueError("a key needs at least one scope")
        daily = _budget("daily_budget_usd", daily_budget_usd)
        maximum = _budget("max_budget_usd", max_budget_usd)
        if daily is not None:
            body["daily_budget_usd"] = daily
        if maximum is not None:
            body["max_budget_usd"] = maximum
        return _key(self._lium.workspaces._session_request("POST", "/keys", workspace_id, json=body).json())


__all__ = [
    "ApiKeysClient",
    "SCOPES_ROUTE",
    "DEFAULT_SCOPES",
    "BILLING_SCOPE",
    "POD_VISIBILITIES",
    "BUDGET_EXCEEDED_CODE",
]
