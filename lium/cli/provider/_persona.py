"""Persona-confirmation gate for ``lium provider …`` (A4).

Why this exists: ``lium mine`` (renter rent flow) and ``lium provider`` (provider
flow) live in adjacent namespaces. Spend-affecting subcommands -- adding /
removing nodes, installing a node over SSH -- get
an explicit one-shot confirmation the first time they run for a user.
After ack, subsequent invocations by the same user are silent for 24 h.

Acks short-circuit on:

- ``LIUM_PROVIDER_ACK=1`` env var (set once by an automating agent)
- ``--yes`` global flag

Otherwise the CLI prints a one-liner and reads ``y`` from stdin -- but only when
a person can answer: under ``--json``, without a terminal on stdin, or with
``LIUM_NONINTERACTIVE`` set, nothing is asked and :class:`ConfirmationRequired`
is raised (the command fails with ``input.confirmation_required``, exit 2). An
EOF at the prompt is the same "no answer". State lives at
``~/.lium/state/provider-ack.json`` keyed by ``(coldkey, hotkey, user)``: the
parent PID used to be part of the key, so every agent subprocess (a new parent
each time) was asked again.
"""

from __future__ import annotations

import getpass
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import click

from lium.cli.interactive import is_interactive, noninteractive_reason

DEFAULT_ACK_PATH = Path.home() / ".lium" / "state" / "provider-ack.json"

# Subcommand names that DO require persona confirmation.
SPEND_AFFECTING_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "node-install",
        "node:install",
        "node-add",
        "node-remove",
        "node-update",
        "validator-switch",
    }
)


class ConfirmationRequired(Exception):
    """The gate needed an answer and nobody could give one (``--json``, no terminal, EOF)."""


class Interrupted(Exception):
    """Ctrl-C at the prompt: the person stopped the command (``input.interrupted``, exit 130)."""


def _read_answer() -> str:
    # input(), not click.prompt: click turns Ctrl-C and EOF alike into Abort, and the two mean different things
    click.echo("Type 'y' to continue (or set LIUM_PROVIDER_ACK=1): ", nl=False, err=True)
    return input()


@dataclass(frozen=True)
class PersonaContext:
    """Inputs needed to compute an ack key."""

    coldkey: str | None
    hotkey: str | None
    scope: str

    def key(self) -> str:
        return f"{self.coldkey or '-'}::{self.hotkey or '-'}::{self.scope}"


def ack_scope() -> str:
    """The user an ack belongs to: one ``y`` covers every shell and subprocess of that user."""
    try:
        return f"user:{getpass.getuser()}"
    except Exception:  # no login name (a container with an unmapped uid)
        return f"uid:{os.getuid()}" if hasattr(os, "getuid") else "user:-"


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
    json_mode: bool = False,
    interactive: bool | None = None,
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
        json_mode: ``--json``/``LIUM_OUTPUT=json``: never prompt.
        interactive: whether a person can answer; defaults to a terminal on
            stdin and no ``LIUM_NONINTERACTIVE``.
        env: env mapping override.
        path: ack-cache path override.
        input_func: callable used to read stdin (defaults to ``input()`` after the prompt on stderr).
        output_func: callable used for the prompt banner (defaults to
            ``click.echo`` writing to stderr).

    Raises:
        ConfirmationRequired: no ack, and nobody can answer (``--json``, no
            terminal, or EOF at the prompt).
    """
    del ctx  # currently unused; reserved for ``--debug`` plumbing.
    persona = PersonaContext(coldkey=coldkey, hotkey=hotkey, scope=ack_scope())
    if yes_flag or auto_ack or is_acked(persona, env=env, path=path):
        return True

    if json_mode:
        raise ConfirmationRequired("no prompt is shown under --json")
    can_ask = is_interactive() if interactive is None else interactive
    if not can_ask:
        raise ConfirmationRequired(f"no prompt is shown because {noninteractive_reason()}")

    output = output_func or (lambda m: click.echo(m, err=True))
    output(
        "You are operating as a PROVIDER (provider persona), not a renter. "
        "Spend-affecting actions (node / install) follow."
    )

    reader = input_func or _read_answer
    try:
        answer = (reader() or "").strip().lower()
    except KeyboardInterrupt as e:
        raise Interrupted("interrupted at the confirmation prompt") from e
    except (EOFError, click.Abort) as e:
        raise ConfirmationRequired("stdin closed before an answer") from e
    if answer not in ("y", "yes"):
        return False
    mark_acked(persona, path=path)
    return True


__all__ = [
    "DEFAULT_ACK_PATH",
    "SPEND_AFFECTING_SUBCOMMANDS",
    "ConfirmationRequired",
    "Interrupted",
    "PersonaContext",
    "ack_scope",
    "confirm_persona",
    "is_acked",
    "mark_acked",
]
