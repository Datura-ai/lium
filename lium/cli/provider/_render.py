"""Output rendering + exit-code mapping for ``lium provider …``.

Two output modes:

- ``--json``: deterministic ``{ok, data, error, warnings}`` envelope, one
  line per result. Driven by an agent.
- TTY default: Rich tables (one table per known DTO), key/value panels
  for single-record endpoints, and a multi-section snapshot for
  ``ProviderStatus``. Curated columns combine related fields (e.g.
  ``8×H100``, ``ip:port``) so the output stays readable on an 80-column
  terminal.

Exit codes follow the taxonomy in :mod:`lium.provider.errors` (see module
docstring). Returning the int from a Click command via
``ctx.exit(emit_error(ctx, err))`` keeps every command's error path
identical.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Iterable, Mapping

import click
from rich.box import SIMPLE_HEAVY
from rich.table import Table

from lium.cli.utils import console
from lium.provider.errors import (
    ARG_INVALID,
    CONFIG_MISSING,
    HOTKEY_NOT_REGISTERED,
    PORTAL_AUTH_EXPIRED,
    PORTAL_AUTH_INVALID,
    PORTAL_AUTH_REFRESH_RACE,
    PORTAL_CONTRACT_DRIFT,
    PORTAL_NOT_FOUND,
    PORTAL_RATE_LIMIT,
    PORTAL_SERVER_ERROR,
    PORTS_INVALID,
    SSH_AUTH_FAILED,
    SSH_UNREACHABLE,
    WALLET_NOT_FOUND,
    ProviderError,
)
from lium.provider.models import ProviderStatus


DISCORD_INCENTIVE_WARNING_CODE = "DISCORD_REQUIRED_FOR_EXTRA_INCENTIVES"
DISCORD_INCENTIVE_WARNING_MESSAGE = (
    "Discord is not connected. No Discord = no extra incentives. "
    "Run `lium provider config connect-discord` to become eligible."
)
DISCORD_CONNECT_COMMAND = "lium provider config connect-discord"
_DISCORD_STATUS_WARNING = "discord: not connected; extra incentives are disabled"

# code -> exit-code mapping. Anything missing falls back to ``1``.
_EXIT_CODES: dict[str, int] = {
    # 1: user/arg errors
    ARG_INVALID: 1,
    PORTS_INVALID: 1,
    HOTKEY_NOT_REGISTERED: 1,
    # 2: auth errors
    PORTAL_AUTH_INVALID: 2,
    PORTAL_AUTH_EXPIRED: 2,
    WALLET_NOT_FOUND: 2,
    # 3: portal (server-side)
    PORTAL_SERVER_ERROR: 3,
    PORTAL_NOT_FOUND: 3,
    PORTAL_CONTRACT_DRIFT: 3,
    PORTAL_RATE_LIMIT: 3,
    # 5: ssh
    SSH_UNREACHABLE: 5,
    SSH_AUTH_FAILED: 5,
    # 6: config
    CONFIG_MISSING: 6,
    # 7: token-cache contention
    PORTAL_AUTH_REFRESH_RACE: 7,
}


def exit_code_for(err: ProviderError) -> int:
    """Map a :class:`ProviderError` to its CLI exit status."""
    return _EXIT_CODES.get(err.code, 1)


def render(
    ctx: click.Context,
    payload: Any,
    *,
    summary: str | None = None,
    warnings: Iterable[Mapping[str, str]] | None = None,
) -> None:
    """Render a successful result.

    JSON mode emits a single line ``{"ok": true, "data": ...}``; TTY mode
    prints ``summary`` (if provided) followed by a Rich table or panel
    chosen by payload shape.
    """
    structured_warnings = _normalise_warnings(warnings)
    if _json_mode(ctx):
        envelope = {"ok": True, "data": _to_serialisable(payload)}
        if structured_warnings:
            envelope["warnings"] = structured_warnings
        click.echo(json.dumps(envelope, sort_keys=True, default=str))
        return

    body = _to_serialisable(payload)

    if summary:
        click.echo(summary)

    # Special-case: aggregated status snapshot.
    if isinstance(payload, ProviderStatus):
        _render_provider_status(payload)
        return

    # List envelope: ``{data: [...], total, page, limit}``.
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        rows = body["data"]
        meta = {k: v for k, v in body.items() if k != "data"}
        _render_rows(rows, meta=meta)
        return

    # Top-level list (some endpoints return raw arrays).
    if isinstance(body, list):
        _render_rows(body)
        return

    # Single-record dict -> 2-column key/value table.
    if isinstance(body, dict) and body:
        _render_record(body)
        return

    # Empty body / scalar -- the summary already covered it.


def emit_error(ctx: click.Context, err: ProviderError) -> int:
    """Format a :class:`ProviderError` and return its exit code."""
    code = exit_code_for(err)
    if _json_mode(ctx):
        envelope = {"ok": False, "error": err.to_dict()}
        click.echo(json.dumps(envelope, sort_keys=True, default=str), err=True)
    else:
        prefix = click.style(f"[{err.code}]", fg="red", bold=True)
        click.echo(f"{prefix} {err.message}", err=True)
        if err.hint:
            click.echo(f"  hint: {err.hint}", err=True)
        if _debug_mode(ctx) and err.context:
            click.echo(f"  context: {err.context}", err=True)
    return code


def emit_warning(ctx: click.Context, code: str, message: str) -> None:
    """Surface a non-fatal warning code + message on stderr."""
    if _json_mode(ctx):
        # The status command surfaces warnings via ProviderStatus.warnings;
        # for one-shot warnings we deliberately keep stdout clean.
        return
    _emit_structured_warnings([{"code": code, "message": message}])


def discord_incentive_warnings(
    discord_connected: bool | None,
) -> list[dict[str, str]]:
    if discord_connected is not False:
        return []
    return [
        {
            "code": DISCORD_INCENTIVE_WARNING_CODE,
            "message": DISCORD_INCENTIVE_WARNING_MESSAGE,
        }
    ]


def _json_mode(ctx: click.Context) -> bool:
    return bool(_opts(ctx).get("json"))


def _debug_mode(ctx: click.Context) -> bool:
    return bool(_opts(ctx).get("debug"))


def _opts(ctx: click.Context) -> Mapping[str, Any]:
    obj = ctx.obj or {}
    return obj.get("provider_opts") or {}


def _to_serialisable(payload: Any) -> Any:
    """Convert pydantic models / dataclasses / nested structures to JSON-safe."""
    if hasattr(payload, "model_dump"):
        return payload.model_dump()
    if isinstance(payload, dict):
        return {k: _to_serialisable(v) for k, v in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_to_serialisable(v) for v in payload]
    return payload


def _normalise_warnings(
    warnings: Iterable[Mapping[str, str]] | None,
) -> list[dict[str, str]]:
    if not warnings:
        return []
    normalised: list[dict[str, str]] = []
    for warning in warnings:
        code = str(warning.get("code", "")).strip()
        message = str(warning.get("message", "")).strip()
        if code and message:
            normalised.append({"code": code, "message": message})
    return normalised


def _emit_structured_warnings(warnings: Iterable[Mapping[str, str]]) -> None:
    for warning in warnings:
        click.echo(
            click.style(f"[{warning['code']}]", fg="yellow", bold=True)
            + f" {warning['message']}",
            err=True,
        )


def fatal(ctx: click.Context, err: ProviderError) -> None:
    """Convenience: emit the error and exit with the mapped status code."""
    code = emit_error(ctx, err)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Rich rendering
# ---------------------------------------------------------------------------


def _new_table(*, headers: bool = True, expand: bool = True) -> Table:
    return Table(
        show_header=headers,
        header_style="dim bold",
        box=SIMPLE_HEAVY,
        pad_edge=False,
        expand=expand,
        padding=(0, 1),
    )


def _human_label(key: str) -> str:
    return key.replace("_", " ").title()


def _bool_icon(value: Any, *, true_label: str = "yes", false_label: str = "no") -> str:
    if value is None:
        return console.get_styled("—", "dim")
    if bool(value):
        return console.get_styled(f"✓ {true_label}", "success")
    return console.get_styled(f"✗ {false_label}", "error")


def _money(value: Any, *, decimals: int = 2) -> str:
    if value is None:
        return console.get_styled("—", "dim")
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    text = f"${v:.{decimals}f}"
    return text


def _truncate_id(value: Any, width: int = 14) -> str:
    if value is None or value == "":
        return console.get_styled("—", "dim")
    text = str(value)
    if len(text) <= width:
        return console.get_styled(text, "id")
    keep = width - 1
    left = keep // 2
    right = keep - left
    return console.get_styled(f"{text[:left]}…{text[-right:]}", "id")


def _truncate_hotkey(value: Any, width: int = 18) -> str:
    return _truncate_id(value, width)


def _short_timestamp(value: Any) -> str:
    if value is None or value == "":
        return console.get_styled("—", "dim")
    text = str(value)
    if "T" not in text:
        return console.get_styled(text, "dim")
    head, _, tail = text.partition("T")
    parts = tail.split(":")
    if len(parts) >= 2:
        return console.get_styled(f"{head} {parts[0]}:{parts[1]}", "dim")
    return console.get_styled(text, "dim")


def _gpu_config(row: Mapping[str, Any]) -> str:
    gpu_count = row.get("gpu_count")
    gpu_type = row.get("gpu_type") or row.get("executor_machine_name") or "—"
    if gpu_count:
        return f"{gpu_count}×{gpu_type}"
    return str(gpu_type)


def _ip_port(row: Mapping[str, Any]) -> str:
    ip = row.get("executor_ip_address") or row.get("ip_address")
    port = row.get("executor_ip_port") or row.get("port")
    if not ip and not port:
        return console.get_styled("—", "dim")
    if ip and port:
        return f"{ip}:{port}"
    return str(ip or port)


def _rented_fraction(row: Mapping[str, Any]) -> str:
    used = row.get("rented_gpu_count")
    total = row.get("gpu_count")
    rented_flag = row.get("rented")
    if total is None and rented_flag is None:
        return console.get_styled("—", "dim")
    if used is not None and total is not None:
        if used:
            return console.get_styled(f"{used}/{total}", "warning")
        return console.get_styled(f"{used}/{total}", "dim")
    return _bool_icon(rented_flag)


def _value_or_dash(v: Any) -> str:
    if v is None or v == "":
        return console.get_styled("—", "dim")
    return str(v)


# ---------------------------------------------------------------------------
# Curated row tables, keyed by sentinel field set.
#
# Each entry registers a (column-header, cell-extractor) list. The first
# preset whose sentinel set is a subset of the row keys wins. This keeps
# the human view stable even when the portal adds new fields.

_RowFn = Callable[[Mapping[str, Any]], str]
# (header, extractor, justify, ratio, min_width, no_wrap)
_TablePreset = list[tuple[str, _RowFn, str, int, int, bool]]


def _format_score(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        rendered = f"{value:.4f}".rstrip("0").rstrip(".")
        return rendered or "0"
    return _value_or_dash(value)


def _trim_money(value: Any, decimals: int = 4) -> str:
    """``$X.YY`` style price, trailing zeros trimmed."""
    if value is None:
        return console.get_styled("—", "dim")
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    text = f"${v:.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "$0"


def _node_preset() -> _TablePreset:
    return [
        ("ID",        lambda r: _truncate_id(r.get("id"), 14),                      "left",  3, 12, True),
        ("GPUs",      _gpu_config,                                                   "left",  4, 14, True),
        ("Endpoint",  _ip_port,                                                      "left",  3, 15, True),
        ("$/GPU·h",   lambda r: _trim_money(r.get("price_per_gpu")),                 "right", 2,  8, True),
        ("Rented",    _rented_fraction,                                              "right", 2,  6, True),
        ("Rev/h",     lambda r: _trim_money(r.get("revenue_per_hour"), decimals=2),  "right", 2,  6, True),
    ]


def _billing_preset() -> _TablePreset:
    return [
        ("Day",      lambda r: _value_or_dash(r.get("billing_day")),                 "left",  2, 10, True),
        (
            "Machine",
            lambda r: f"{r.get('executor_gpu_count') or '?'}× "
            + str(r.get("executor_machine_name") or "—"),
            "left", 4, 14, True,
        ),
        ("Amount",   lambda r: _trim_money(r.get("amount"), decimals=2),             "right", 2,  8, True),
        ("Miner $",  lambda r: _trim_money(r.get("miner_amount"), decimals=2),       "right", 2,  8, True),
        ("Paid",     lambda r: _bool_icon(r.get("paid")),                            "center",1,  6, True),
    ]


def _machine_preset() -> _TablePreset:
    return [
        ("Name",      lambda r: _value_or_dash(r.get("name")),                        "left",  4, 14, True),
        ("Price",     lambda r: _trim_money(r.get("price"), decimals=4),              "right", 2,  8, True),
        ("Supported", lambda r: _bool_icon(r.get("supported")),                       "center",1,  9, True),
        ("Score",     lambda r: _format_score(r.get("score_portion")),                "right", 2,  6, True),
        ("Hourly $",  lambda r: _trim_money(r.get("hourly_rewards_in_usd"), 4),       "right", 2,  8, True),
    ]


def _machine_request_preset() -> _TablePreset:
    return [
        ("ID",       lambda r: _truncate_id(r.get("id"), 14),                         "left",  3, 12, True),
        ("Machine",  lambda r: _value_or_dash(r.get("machine_name")),                 "left",  4, 14, True),
        ("GPUs",     lambda r: _value_or_dash(r.get("gpu_count")),                    "right", 1,  4, True),
        ("CPU",      lambda r: _value_or_dash(r.get("cpu")),                          "left",  2,  6, True),
        ("RAM",      lambda r: _value_or_dash(r.get("ram")),                          "left",  2,  6, True),
        ("Achieved", lambda r: _bool_icon(r.get("achieved")),                         "center",1,  9, True),
    ]


def _pod_preset() -> _TablePreset:
    return [
        ("ID",      lambda r: _truncate_id(r.get("id"), 14),                          "left",  3, 12, True),
        ("Name",    lambda r: _value_or_dash(r.get("pod_name")),                      "left",  4, 14, True),
        ("Status",  lambda r: _value_or_dash(r.get("status")),                        "left",  2,  8, True),
        ("GPUs",    lambda r: _value_or_dash(r.get("gpu_count")),                     "right", 1,  4, True),
        ("$/h",     lambda r: _trim_money(r.get("price"), decimals=4),                "right", 2,  6, True),
        ("Created", lambda r: _short_timestamp(r.get("created_at")),                  "left",  3, 14, True),
    ]


def _validator_weight_preset() -> _TablePreset:
    return [
        ("Validator", lambda r: _truncate_hotkey(r.get("validator_hotkey"), 22),      "left",  6, 22, True),
        ("Weight",    lambda r: _format_score(r.get("weight")),                       "right", 2,  8, True),
    ]


# Sentinel keys must all be present in a row for the preset to match.
_PRESETS: list[tuple[frozenset[str], _TablePreset]] = [
    (frozenset({"executor_ip_address", "price_per_gpu"}), _node_preset()),
    (frozenset({"billing_day", "miner_amount"}), _billing_preset()),
    (frozenset({"machine_name", "achieved"}), _machine_request_preset()),
    (frozenset({"pod_name", "status"}), _pod_preset()),
    (frozenset({"name", "score_portion"}), _machine_preset()),
    (frozenset({"validator_hotkey", "weight"}), _validator_weight_preset()),
]


def _match_preset(row: Mapping[str, Any]) -> _TablePreset | None:
    keys = set(row.keys())
    for sentinel, preset in _PRESETS:
        if sentinel.issubset(keys):
            return preset
    return None


def _render_rows(rows: Iterable[Any], *, meta: Mapping[str, Any] | None = None) -> None:
    items = [_to_serialisable(it) for it in (rows or [])]

    if not items:
        console.print(console.get_styled("  (no records)", "dim"))
        if meta:
            _print_meta_line(meta)
        return

    # Heterogeneous list of non-mappings: bullet list.
    if not all(isinstance(it, Mapping) for it in items):
        for it in items:
            console.print(f"  • {it}")
        if meta:
            _print_meta_line(meta)
        return

    preset = _match_preset(items[0])
    if preset is None:
        # Generic auto layout for unknown shapes.
        _render_generic_rows(items)
        if meta:
            _print_meta_line(meta)
        return

    table = _new_table()
    table.add_column("#", justify="right", style="dim", no_wrap=True, width=3)
    for header, _extract, justify, ratio, min_width, no_wrap in preset:
        table.add_column(
            header,
            justify=justify,
            no_wrap=no_wrap,
            overflow="ellipsis",
            ratio=ratio,
            min_width=min_width,
        )

    for idx, row in enumerate(items, 1):
        cells = [str(idx)]
        for _header, extract, *_meta in preset:
            try:
                cells.append(extract(row))
            except Exception:
                cells.append(console.get_styled("?", "warning"))
        table.add_row(*cells)

    console.print(table)
    if meta:
        _print_meta_line(meta)


def _render_generic_rows(items: list[Mapping[str, Any]]) -> None:
    """Fallback list renderer when no curated preset matches.

    Picks up to 6 columns from the first row, biased toward
    ``id`` / ``name`` / scalars.
    """
    keys: list[str] = []
    for k in items[0].keys():
        if k not in keys:
            keys.append(k)
    # Push complex (dict/list) keys to the back so they get pruned by the cap.
    keys.sort(
        key=lambda k: (
            isinstance(items[0].get(k), (dict, list, tuple)),
            keys.index(k),
        )
    )
    keys = keys[:6]

    table = _new_table()
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    for k in keys:
        table.add_column(_human_label(k), justify="left", no_wrap=True, overflow="ellipsis")
    for idx, row in enumerate(items, 1):
        cells = [str(idx)]
        for k in keys:
            cells.append(_format_generic_value(k, row.get(k)))
        table.add_row(*cells)
    console.print(table)


def _format_generic_value(key: str, value: Any) -> str:
    if value is None or value == "":
        return console.get_styled("—", "dim")
    if isinstance(value, bool):
        return _bool_icon(value)
    if isinstance(value, (dict, list, tuple)):
        if not value:
            return console.get_styled("—", "dim")
        if isinstance(value, (list, tuple)):
            return console.get_styled(f"[{len(value)}]", "dim")
        return console.get_styled(f"{{{len(value)}}}", "dim")
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".") or "0"
    if isinstance(value, int):
        return f"{value:,}"
    text = str(value)
    if "hotkey" in key or "coldkey" in key:
        return _truncate_hotkey(text)
    if key.endswith("_id") or key == "id":
        return _truncate_id(text)
    if key in {"created_at", "updated_at", "deleted_at"}:
        return _short_timestamp(text)
    if len(text) > 36:
        return _truncate_id(text, 36)
    return text


def _print_meta_line(meta: Mapping[str, Any]) -> None:
    interesting = ("total", "page", "limit")
    parts: list[str] = []
    for key in interesting:
        if key in meta and meta[key] not in (None, "", {}):
            parts.append(f"{key}={meta[key]}")
    if parts:
        console.print(console.get_styled("  " + ", ".join(parts), "dim"))


# ---------------------------------------------------------------------------
# Single-record renderer
# ---------------------------------------------------------------------------


def _render_record(body: Mapping[str, Any]) -> None:
    """Render a single dict as a 2-column key/value Rich table."""
    table = _new_table(headers=False, expand=False)
    table.add_column("Field", style="dim", justify="right", no_wrap=True)
    table.add_column("Value", overflow="fold")
    extra_incentives_disabled = body.get("extra_incentive_eligible") is False
    extra_incentive_note_rendered = False
    for key, value in body.items():
        if key == "extra_incentive_eligible" and extra_incentives_disabled:
            continue
        table.add_row(_human_label(key), _format_record_value(key, value))
        if key == "discord_connected" and extra_incentives_disabled:
            table.add_row("Extra Incentives", _format_discord_incentive_next_step())
            extra_incentive_note_rendered = True
    if extra_incentives_disabled and not extra_incentive_note_rendered:
        table.add_row("Extra Incentives", _format_discord_incentive_next_step())
    console.print(table)


_RAW_INT_KEYS = frozenset(
    {"port", "netuid", "uid", "miner_uid", "executor_ip_port", "central_miner_port"}
)

_PRICE_KEYS = frozenset(
    {
        "price",
        "price_per_gpu",
        "price_per_hour",
        "executor_price_per_gpu",
        "amount",
        "miner_amount",
        "machine_price",
        "rewards_on_subnet",
        "rental_bonus",
        "hourly_rewards_in_usd",
        "revenue_per_hour",
        "collateral_amount",
    }
)


def _format_record_value(key: str, value: Any) -> str:
    """Like ``_format_generic_value`` but does not truncate IDs/hotkeys --
    a single record is the right place to reveal full identifiers."""
    if value is None or value == "":
        return console.get_styled("—", "dim")
    if key == "extra_incentive_eligible" and value is False:
        return _format_extra_incentive_eligible(
            value,
            true_label="true",
            false_label="false",
        )
    if isinstance(value, bool):
        return _bool_icon(value, true_label="true", false_label="false")
    if isinstance(value, (list, tuple)):
        if not value:
            return console.get_styled("(empty)", "dim")
        if all(not isinstance(v, (dict, list, tuple)) for v in value):
            return ", ".join(str(v) for v in value)
        return console.get_styled(f"[{len(value)} items]", "dim")
    if isinstance(value, dict):
        if not value:
            return console.get_styled("(empty)", "dim")
        if len(value) <= 4 and all(
            not isinstance(v, (dict, list, tuple)) for v in value.values()
        ):
            return ", ".join(f"{k}={v}" for k, v in value.items())
        return console.get_styled(f"{{{len(value)} fields}}", "dim")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if key in _PRICE_KEYS:
            if isinstance(value, float):
                rendered = f"${value:.4f}".rstrip("0").rstrip(".")
                return rendered if rendered != "$" else "$0"
            return f"${value}"
        if isinstance(value, float):
            rendered = f"{value:.6f}".rstrip("0").rstrip(".")
            return rendered or "0"
        # Identifier-ish ints (ports, uids, netuids) shouldn't get
        # thousands separators -- ``9090`` reads better than ``9,090``.
        if key in _RAW_INT_KEYS or key.endswith("_port") or key.endswith("_uid") or key.endswith("_id"):
            return str(value)
        return f"{value:,}"
    text = str(value)
    if key in {"created_at", "updated_at", "deleted_at", "billing_day"}:
        return _short_timestamp(text)
    if "hotkey" in key or "coldkey" in key or key == "id" or key.endswith("_id"):
        return console.get_styled(text, "id")
    return text


def _format_extra_incentive_eligible(
    value: Any,
    *,
    true_label: str = "yes",
    false_label: str = "no",
) -> str:
    return _bool_icon(value, true_label=true_label, false_label=false_label)


def _format_discord_connect_command() -> str:
    return console.get_styled(DISCORD_CONNECT_COMMAND, "warning")


def _format_discord_incentive_next_step() -> str:
    return (
        "No Discord = no extra incentives.\nRun: "
        + _format_discord_connect_command()
    )


def _render_provider_status(status: ProviderStatus) -> None:
    """Multi-section render for the aggregated ``status`` command."""
    overview = _new_table(headers=False, expand=False)
    overview.add_column("Field", style="dim", justify="right", no_wrap=True)
    overview.add_column("Value", overflow="fold")

    overview.add_row("Hotkey", _format_record_value("hotkey", status.hotkey))
    overview.add_row("Coldkey", _format_record_value("coldkey", status.coldkey))
    overview.add_row("Netuid", _value_or_dash(status.netuid))
    overview.add_row(
        "Registered on subnet",
        _bool_icon(status.registered_on_subnet) if status.registered_on_subnet is not None else console.get_styled("unknown", "dim"),
    )
    overview.add_row(
        "Portal session",
        console.get_styled("active", "success")
        if status.portal_session_active
        else console.get_styled("inactive", "warning"),
    )
    overview.add_row("Provider id", _format_record_value("provider_id", status.provider_id))
    overview.add_row(
        "Discord connected",
        _bool_icon(status.discord_connected)
        if status.discord_connected is not None
        else console.get_styled("unknown", "dim"),
    )
    if status.extra_incentive_eligible is False:
        overview.add_row("Extra incentives", _format_discord_incentive_next_step())
    elif status.extra_incentive_eligible is not None:
        overview.add_row(
            "Extra incentive eligible",
            _format_extra_incentive_eligible(status.extra_incentive_eligible),
        )
    overview.add_row("Nodes", _value_or_dash(status.node_count))
    console.print(overview)

    if status.nodes:
        console.print(console.get_styled(f"\nNodes ({len(status.nodes)})", "info"))
        _render_rows([n.model_dump() for n in status.nodes])

    if status.validator_weights:
        console.print(
            console.get_styled(
                f"\nValidator weights ({len(status.validator_weights)})", "info"
            )
        )
        _render_rows([w.model_dump() for w in status.validator_weights])

    visible_warnings = [
        warning for warning in status.warnings if warning != _DISCORD_STATUS_WARNING
    ]
    if visible_warnings:
        console.print(
            console.get_styled(f"\nWarnings ({len(visible_warnings)})", "warning")
        )
        for w in visible_warnings:
            console.print("  " + console.get_styled(f"⚠ {w}", "warning"))


__all__ = [
    "discord_incentive_warnings",
    "emit_error",
    "emit_warning",
    "exit_code_for",
    "fatal",
    "render",
]
