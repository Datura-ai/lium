"""Text for ``lium provider node status`` from the portal's
``GET /executors/{id}/verification`` body (DAH-3019).

Pure functions over the response dict so the rendering is testable without a
terminal: one headline (``verifying · step 3/6 Bandwidth & GPU proof · 42 s
elapsed · ~1 min 10 s left``) and one line per step with ✓ / ✗ / … and its duration.
"""

from __future__ import annotations

from typing import Any, Mapping

_ICONS = {
    "passed": "✓",
    "failed": "✗",
    "running": "…",
    "pending": "·",
    "skipped": "–",
}


def format_seconds(value: Any) -> str:
    """``42 s`` / ``2 min 05 s`` / ``—`` for None."""
    if value is None:
        return "—"
    try:
        whole = max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return "—"
    if whole < 60:
        return f"{whole} s"
    return f"{whole // 60} min {whole % 60:02d} s"


def _outcome(run: Mapping[str, Any]) -> str:
    outcome = run.get("outcome")
    total = run.get("total_s")
    suffix = f" in {format_seconds(total)}" if total is not None else ""
    if outcome == "passed":
        return f"passed{suffix}"
    if outcome == "failed":
        reason = run.get("reason_code")
        return f"failed{f' ({reason})' if reason else ''}{suffix}"
    if outcome == "incomplete":
        return "did not complete"
    return str(outcome or "unknown")


def headline(body: Mapping[str, Any]) -> str:
    """One line a provider (or an agent's grep) reads first."""
    phase = body.get("phase")
    if phase == "verifying":
        parts = ["verifying"]
        current = body.get("current") or {}
        steps = body.get("steps") or []
        if current:
            parts.append(f"step {current.get('index')}/{len(steps)} {current.get('name')}")
        parts.append(f"{format_seconds(body.get('elapsed_s'))} elapsed")
        if body.get("eta_s") is not None:
            parts.append(f"~{format_seconds(body.get('eta_s'))} left")
        return " · ".join(parts)
    if phase == "publishing":
        parts = ["checks done", "waiting for the validator to publish the result"]
        if body.get("eta_s") is not None:
            parts.append(f"~{format_seconds(body.get('eta_s'))}")
        return " · ".join(parts)
    if phase == "never_validated":
        line = "not verified yet"
        if body.get("next_check_expected_at"):
            line += f" · next validator cycle around {body['next_check_expected_at']}"
        return line
    last = body.get("last_run") or {}
    if last:
        line = f"idle · last run {_outcome(last)}"
        if last.get("cycle_time"):
            line += f" (cycle {last['cycle_time']})"
        if body.get("next_check_expected_at"):
            line += f" · next check around {body['next_check_expected_at']}"
        return line
    return "idle"


def step_lines(body: Mapping[str, Any]) -> list[str]:
    """One line per step of the live run (while verifying) or the last run."""
    phase = body.get("phase")
    run = body.get("run") if phase in ("verifying", "publishing") else body.get("last_run")
    steps = (run or {}).get("steps") or body.get("steps") or []
    lines: list[str] = []
    for step in steps:
        status = step.get("status", "pending")
        icon = _ICONS.get(status, "·")
        name = step.get("name") or step.get("check_id")
        if status == "running":
            detail = f"{format_seconds(step.get('duration_s'))} so far"
            if step.get("p50_s") is not None:
                detail += f" · typically {format_seconds(step['p50_s'])}"
        elif status == "pending":
            detail = f"typically {format_seconds(step['p50_s'])}" if step.get("p50_s") is not None else ""
        elif status == "skipped":
            detail = "not reached"
        elif step.get("duration_s") is None:
            detail = "estimated" if step.get("estimated") else "under 1 s"
        else:
            detail = format_seconds(step["duration_s"]) + (" (estimated)" if step.get("estimated") else "")
        lines.append(f"  {icon} {step.get('index', len(lines) + 1)}. {name}" + (f" — {detail}" if detail else ""))
    return lines


def basis_line(body: Mapping[str, Any]) -> str | None:
    basis = body.get("basis") or "none"
    if basis == "none":
        return None
    kind, _, rest = basis.partition(":")
    if kind == "total-only":
        source, _, runs = rest.partition(":")
        owner = "the fleet's" if source == "fleet" else "this node's"
        return f"typical total from {owner} last {runs} (per-step timings not available yet)"
    owner = "the fleet's" if kind == "fleet" else "this node's"
    return f"typical durations from {owner} last {rest}"


def render_text(body: Mapping[str, Any]) -> str:
    lines = [headline(body)]
    lines.extend(step_lines(body))
    basis = basis_line(body)
    if basis:
        live = body.get("phase") in ("verifying", "publishing")
        lines.append(("position and time left are estimates: " if live else "") + basis)
    return "\n".join(lines)


__all__ = ["basis_line", "format_seconds", "headline", "render_text", "step_lines"]
