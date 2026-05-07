"""Persona-confirmation gate for ``lium miner …`` (A4).

Why this exists: ``lium mine`` (renter rent flow) and ``lium miner`` (provider
flow) live in adjacent namespaces. Spend-affecting subcommands -- adding /
removing executors, switching validators, installing a node over SSH -- get
an explicit one-shot confirmation the first time they run in a shell
session. After ack, subsequent invocations in the same shell are silent.

Acks short-circuit on:

- ``LIUM_PROVIDER_ACK=1`` env var (set once by an automating agent)
- ``--yes`` global flag

Otherwise the CLI prints a one-liner and reads ``y`` from stdin. State lives
at ``~/.lium/state/provider-ack.json`` keyed by ``(coldkey, hotkey, ppid)``
so a fresh shell re-prompts but child commands within the same shell don't.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import click

DEFAULT_ACK_PATH = Path.home() / ".lium" / "state" / "provider-ack.json"

# Subcommand names that DO require persona confirmation.
SPEND_AFFECTING_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "node-install",
        "node:install",
        "executor-add",
        "executor-remove",
        "executor-update",
        "validator-switch",
    }
)


@dataclass(frozen=True)
class PersonaContext:
    """Inputs needed to compute an ack key."""

    coldkey: str | None
    hotkey: str | None
    shell_session_id: str

    def key(self) -> str:
        return f"{self.coldkey or '-'}::{self.hotkey or '-'}::{self.shell_session_id}"


def shell_session_id() -> str:
    """Best-effort stable id for the parent shell session.

    Uses ``$$``'s parent (``os.getppid()``) so two ``lium miner`` invocations
    from the same shell share an id. Agents wanting deterministic acks
    should set ``LIUM_PROVIDER_ACK=1`` instead of relying on this heuristic.
    """
    return str(os.getppid())


def is_acked(
    persona: PersonaContext,
    *,
    env: dict[str, str] | None = None,
    path: Path | None = None,
    now: float | None = None,
    ttl_seconds: int = 24 * 3600,
) -> bool:
    """Return True if a previous ack still applies for this persona."""
    source = env if env is not None else os.environ
    if source.get("LIUM_PROVIDER_ACK") == "1":
        return True
    cache_path = path or DEFAULT_ACK_PATH
    try:
        if not cache_path.exists():
            return False
        data = json.loads(cache_path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    entry = data.get(persona.key())
    if not isinstance(entry, dict):
        return False
    ts = entry.get("acked_at")
    if not isinstance(ts, (int, float)):
        return False
    current = now if now is not None else time.time()
    return current - ts < ttl_seconds


def mark_acked(persona: PersonaContext, *, path: Path | None = None) -> None:
    """Persist a successful ack."""
    cache_path = path or DEFAULT_ACK_PATH
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, object]
        try:
            existing = (
                json.loads(cache_path.read_text(encoding="utf-8") or "{}")
                if cache_path.exists()
                else {}
            )
            if not isinstance(existing, dict):
                existing = {}
        except (OSError, json.JSONDecodeError):
            existing = {}
        existing[persona.key()] = {"acked_at": int(time.time())}
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp.write_text(json.dumps(existing, sort_keys=True), encoding="utf-8")
        os.replace(tmp, cache_path)
        try:
            os.chmod(cache_path, 0o600)
        except OSError:
            pass
    except OSError:
        # Best-effort: a missing state dir should never abort a CLI command.
        return


def confirm_persona(
    ctx: click.Context,
    *,
    coldkey: str | None,
    hotkey: str | None,
    yes_flag: bool = False,
    auto_ack: bool = False,
    env: dict[str, str] | None = None,
    path: Path | None = None,
    input_func=None,
    output_func=None,
) -> bool:
    """Run the persona gate; return True iff the user (or env) confirmed.

    Args:
        ctx: Click context (used only for ``ctx.obj`` retrieval if needed).
        coldkey/hotkey: persona components for the ack key.
        yes_flag: if True (``--yes`` passed), confirms without prompting.
        auto_ack: if True (test seam), confirms without prompting.
        env: env mapping override.
        path: ack-cache path override.
        input_func: callable used to read stdin (defaults to ``click.prompt``).
        output_func: callable used for the prompt banner (defaults to
            ``click.echo`` writing to stderr).
    """
    del ctx  # currently unused; reserved for ``--debug`` plumbing.
    persona = PersonaContext(
        coldkey=coldkey,
        hotkey=hotkey,
        shell_session_id=shell_session_id(),
    )
    if yes_flag or auto_ack or is_acked(persona, env=env, path=path):
        return True

    output = output_func or (lambda m: click.echo(m, err=True))
    output(
        "You are operating as a PROVIDER (miner persona), not a renter. "
        "Spend-affecting actions (executor / node install) follow."
    )

    reader = input_func or (
        lambda: click.prompt(
            "Type 'y' to continue (or set LIUM_PROVIDER_ACK=1)",
            default="n",
            show_default=False,
        )
    )
    try:
        answer = (reader() or "").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if answer not in ("y", "yes"):
        return False
    mark_acked(persona, path=path)
    return True


__all__ = [
    "DEFAULT_ACK_PATH",
    "SPEND_AFFECTING_SUBCOMMANDS",
    "PersonaContext",
    "confirm_persona",
    "is_acked",
    "mark_acked",
    "shell_session_id",
]
