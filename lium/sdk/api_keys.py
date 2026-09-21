"""API keys: scopes, budgets and pod visibility (lium-platform DAH-2944 scopes; P235 budgets and visibility,
lium-platform#630, not released).

Every ``/keys`` route is session-only on the server (``utils/auth.py``: ``authenticate``, a browser JWT): a key
cannot list, mint or reshape keys, so these calls need ``Lium.workspaces.login`` or LIUM_SESSION_TOKEN and raise
:class:`LiumSessionError` without one. ``GET /keys/scopes`` is static text and needs no credential at all.

``scopes()`` is the single source of the "what this key can do" words: the CLI prints the server's sentences
and never its own copy.
"""

from itertools import pairwise
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional

from .exceptions import LiumAuthError, LiumError, LiumNotFoundError
from .models import ApiKeyInfo, ApiKeyRefusal, ApiKeyScope

if TYPE_CHECKING:  # pragma: no cover
    from .client import Lium

SCOPES_ROUTE = "/keys/scopes"
# What a key gets when the caller names no scope: everything but ``billing``. The list is always sent, so a
# server whose own default for an omitted ``scopes`` is wider (DAH-2944: "every scope") cannot hand a new key
# the money routes (owner, 21 Sep 2026: the elevated key is off by default).
DEFAULT_SCOPES = ("read", "rent", "manage")
BILLING_SCOPE = "billing"
# Conductor 21 Sep 2026 12:11Z: `billing` is "and nothing else" — a key that moves money holds no other scope;
# the server answers 422 to billing + rent/manage/read (lium-platform#630, not released). Checked here first, so the
# refusal costs no request and reads the same from the CLI and the SDK.
BILLING_ALONE = (
    "The 'billing' scope stands alone: a key that moves money holds no other scope — make it a key of its own "
    "(billing with read, rent or manage is refused; lium-platform#630, not released)"
)
BUDGET_FIELDS = ("daily_budget_usd", "monthly_budget_usd", "max_budget_usd")
REFUSALS_ROUTE = "/keys/{id}/refusals"
POD_VISIBILITIES = ("own", "account")
BUDGET_EXCEEDED_CODE = "API_KEY_BUDGET_EXCEEDED"
# dtos/api_key.py BUDGET_MIN_USD: below $1 a budget stops every pod on its first accrual, so the server refuses it
BUDGET_MIN_USD = 1.0
NO_SCOPES_ROUTE = "This server has no GET /keys/scopes yet (lium-platform#630, not released): scope descriptions are unavailable"
NO_SCOPES_ROUTE_HINT = (
    "The scopes on this server are read, rent and manage (`lium keys create --scope`); `billing`, budgets and pod "
    "visibility arrive with lium-platform#630"
)


class _Unset:
    def __repr__(self) -> str:  # pragma: no cover - repr only
        return "UNSET"


UNSET: Any = _Unset()  # "leave this budget as it is" on update(); None means "clear it"


def _usd(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
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
        monthly_budget_usd=_usd(d.get("monthly_budget_usd")),
        max_budget_usd=_usd(d.get("max_budget_usd")),
        spent_today_usd=_usd(d.get("spent_today_usd")),
        spent_month_usd=_usd(d.get("spent_month_usd", d.get("spent_this_month_usd"))),
        spent_total_usd=_usd(d.get("spent_total_usd")),
        pod_visibility=visibility if isinstance(visibility, str) and visibility else None,
        pods_count=_int(d.get("pods_count")),
        key=d.get("key") if isinstance(d.get("key"), str) else None,
        raw=dict(d),
    )


def _scope(d: Dict[str, Any]) -> ApiKeyScope:
    def strings(value: Any) -> List[str]:
        return [str(item) for item in value] if isinstance(value, list) else []

    return ApiKeyScope(
        scope=str(d.get("scope", "")),
        description=str(d.get("description", "")),
        title=str(d.get("title", "")),
        can=strings(d.get("can")),
        route_families=strings(d.get("route_families")),
        default=bool(d.get("default", False)),
    )


def _refusal(d: Dict[str, Any]) -> ApiKeyRefusal:
    """A refusal row as the ledger names it; the field names follow the spec of 21 Sep 2026 (`api_key_budget_refused`:
    key id, window hit, amount asked, route) with the timestamp under `created_at` / `at` / `refused_at`."""
    at = d.get("created_at") or d.get("at") or d.get("refused_at")
    window = d.get("window")
    route = d.get("route")
    return ApiKeyRefusal(
        at=str(at) if at else None,
        window=str(window) if window else None,
        route=str(route) if route else None,
        amount_usd=_usd(d.get("amount_usd", d.get("amount_asked_usd", d.get("amount")))),
        budget_usd=_usd(d.get("budget_usd")),
        spent_usd=_usd(d.get("spent_usd")),
        raw=dict(d),
    )


def check_scopes(scopes: List[str]) -> List[str]:
    """The scopes a key may be minted with: at least one, and `billing` on its own (:data:`BILLING_ALONE`).
    Raises ``ValueError`` (the CLI's exit 2) before any request."""
    if not scopes:
        raise ValueError("a key needs at least one scope")
    if BILLING_SCOPE in scopes and len(scopes) > 1:
        raise ValueError(BILLING_ALONE)
    return scopes


def check_budget_order(
    daily: Optional[float], monthly: Optional[float], maximum: Optional[float]
) -> None:
    """A wider window's budget cannot be below a narrower one's: daily ≤ monthly ≤ max, over the windows given."""
    given = [(name, value) for name, value in (("daily", daily), ("monthly", monthly), ("max", maximum)) if value is not None]
    for (narrow, low), (wide, high) in pairwise(given):
        if high < low:
            raise ValueError(
                f"the {wide} budget (${high:,.2f}) is below the {narrow} budget (${low:,.2f}); "
                "a wider window cannot hold less than a narrower one"
            )


def budget_amount(name: str, value: Optional[float]) -> Optional[float]:
    """A budget as the number the API takes: USD ≥ $1 in whole cents, or None for no budget.

    Raises ``ValueError`` (the CLI's ``value_error``, exit 2) before any request for anything else.
    """
    if value is None:
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number of USD, not {value!r}") from exc
    if amount != amount or amount < BUDGET_MIN_USD:  # NaN or below the server's floor
        raise ValueError(f"{name} must be at least ${BUDGET_MIN_USD:.0f} ({value} given); leave it out for no budget")
    if round(amount, 2) != amount:
        raise ValueError(f"{name} is billed in cents: {value} has more than two decimals")
    return amount


class ApiKeysClient:
    def __init__(self, lium: "Lium"):
        self._lium = lium
        self._scopes_payload: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ scopes (no credential needed)
    def scopes_payload(self) -> Dict[str, Any]:
        """The body of ``GET /keys/scopes`` as the server sent it, read once per client:
        ``{"scopes": [...], "pod_visibility": [...], "money_routes": [...]}`` (lium-platform#630).

        A server before P235 has no such route: the path falls into its session-only ``GET /keys/{id}`` and
        answers 401 (lium.io on 21 Sep 2026), or 404 once that route is gone. On lium-platform#630 the route
        takes no credential at all, so neither answer can mean a bad key — both become one
        :class:`LiumNotFoundError` that names the missing route. A server that answers a bare list is read
        as the ``scopes`` list alone.
        """
        if self._scopes_payload is None:
            try:
                data = self._lium.workspaces._read(SCOPES_ROUTE).json()
            except (LiumAuthError, LiumNotFoundError) as exc:
                raise LiumNotFoundError(NO_SCOPES_ROUTE, hint=NO_SCOPES_ROUTE_HINT, request_id=exc.request_id) from exc
            if isinstance(data, list):
                data = {"scopes": data}
            self._scopes_payload = data if isinstance(data, dict) else {}
        return self._scopes_payload

    def scopes(self) -> List[ApiKeyScope]:
        """Every scope the server knows: its sentence, what a key holding it can do, the routes it opens,
        and whether a key made without naming scopes gets it."""
        rows = self.scopes_payload().get("scopes")
        return [_scope(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def pod_visibilities(self) -> Dict[str, str]:
        """``{"own": <sentence>, "account": <sentence>}`` — the server's words for each pod-visibility value."""
        rows = self.scopes_payload().get("pod_visibility")
        if not isinstance(rows, list):
            return {}
        return {
            str(row["value"]): str(row.get("description", ""))
            for row in rows
            if isinstance(row, dict) and row.get("value")
        }

    # ------------------------------------------------------------------ reads (session)
    def list(self, workspace_id: Optional[str] = None) -> List[ApiKeyInfo]:
        """The keys of a workspace (``GET /keys``, ``X-Lium-Workspace-Id`` when given)."""
        rows = self._lium.workspaces._session_request("GET", "/keys", workspace_id).json()
        return [_key(row) for row in rows] if isinstance(rows, list) else []

    def get(self, key_id: str, workspace_id: Optional[str] = None) -> ApiKeyInfo:
        """One key by id (``GET /keys/{id}``)."""
        return _key(self._lium.workspaces._session_request("GET", f"/keys/{key_id}", workspace_id).json())

    def refusals(self, key_id: str, workspace_id: Optional[str] = None) -> List[ApiKeyRefusal]:
        """The requests this key's budget refused, newest first (``GET /keys/{id}/refusals``, lium-platform#630,
        not released — the ledger's ``api_key_budget_refused`` rows). A server without the route answers 404
        (:class:`LiumNotFoundError`); a body of ``{"refusals": [...]}`` or a bare list is read alike."""
        data = self._lium.workspaces._session_request("GET", REFUSALS_ROUTE.format(id=key_id), workspace_id).json()
        rows = data.get("refusals") if isinstance(data, dict) else data
        refusals = [_refusal(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        return sorted(refusals, key=lambda r: r.at or "", reverse=True)

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
        monthly_budget_usd: Optional[float] = None,
        max_budget_usd: Optional[float] = None,
        pod_visibility: str = "own",
        workspace_id: Optional[str] = None,
    ) -> ApiKeyInfo:
        """Mint a key (``POST /keys``); the secret is in the returned ``key`` this once.

        ``scopes`` defaults to :data:`DEFAULT_SCOPES` (``read``, ``rent``, ``manage``) and is always sent, so
        ``billing`` — the money routes — is on a key only when named, and then alone (:data:`BILLING_ALONE`).
        The three budgets (``daily_budget_usd`` per UTC day, ``monthly_budget_usd`` per UTC month,
        ``max_budget_usd`` for the key's lifetime) are sent as numbers (USD ≥ 1, whole cents) only when given,
        daily ≤ monthly ≤ max; ``pod_visibility`` is ``own`` (the key sees only the pods it creates) or
        ``account``. A server before P235 ignores the budget and visibility fields and answers 422 to a
        ``billing`` scope.
        """
        if pod_visibility not in POD_VISIBILITIES:
            raise ValueError(f"pod_visibility must be one of {', '.join(POD_VISIBILITIES)}, not {pod_visibility!r}")
        body: Dict[str, Any] = {
            "name": name,
            "scopes": check_scopes(list(scopes) if scopes is not None else list(DEFAULT_SCOPES)),
            "pod_visibility": pod_visibility,
        }
        amounts = [
            budget_amount(field_name, value)
            for field_name, value in zip(BUDGET_FIELDS, (daily_budget_usd, monthly_budget_usd, max_budget_usd), strict=True)
        ]
        check_budget_order(*amounts)
        for field_name, amount in zip(BUDGET_FIELDS, amounts, strict=True):
            if amount is not None:
                body[field_name] = amount
        return _key(self._lium.workspaces._session_request("POST", "/keys", workspace_id, json=body).json())

    def update(
        self,
        key_id: str,
        *,
        daily_budget_usd: Optional[float] = UNSET,
        monthly_budget_usd: Optional[float] = UNSET,
        max_budget_usd: Optional[float] = UNSET,
        workspace_id: Optional[str] = None,
    ) -> ApiKeyInfo:
        """Set or clear a key's budgets (``PATCH /keys/{id}``, lium-platform#630).

        A budget given as a number is set, as ``None`` is cleared, left out (:data:`UNSET`) is kept as it is;
        naming none is a ``ValueError`` here (the server would answer 400). The budgets named here must keep
        daily ≤ monthly ≤ max among themselves. Scopes and pod visibility are fixed at creation and cannot be changed.
        """
        body: Dict[str, Any] = {}
        for field_name, value in zip(BUDGET_FIELDS, (daily_budget_usd, monthly_budget_usd, max_budget_usd), strict=True):
            if value is UNSET:
                continue
            body[field_name] = budget_amount(field_name, value)
        if not body:
            raise ValueError("name a budget to set or clear: daily_budget_usd, monthly_budget_usd and/or max_budget_usd")
        check_budget_order(*(body.get(field_name) for field_name in BUDGET_FIELDS))
        return _key(self._lium.workspaces._session_request("PATCH", f"/keys/{key_id}", workspace_id, json=body).json())


__all__ = [
    "ApiKeysClient",
    "SCOPES_ROUTE",
    "DEFAULT_SCOPES",
    "BILLING_SCOPE",
    "BILLING_ALONE",
    "BUDGET_FIELDS",
    "REFUSALS_ROUTE",
    "POD_VISIBILITIES",
    "BUDGET_EXCEEDED_CODE",
    "BUDGET_MIN_USD",
    "NO_SCOPES_ROUTE",
    "UNSET",
    "budget_amount",
    "check_scopes",
    "check_budget_order",
]
