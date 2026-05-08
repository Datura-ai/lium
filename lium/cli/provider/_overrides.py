"""Re-declare ``lium provider`` group flags on each leaf command.

Click's group flags must appear *before* the subcommand chain
(``lium provider --json status``), which trips users who write the more
natural-looking ``lium provider status --json``. This decorator adds
hidden duplicates of the seven group flags to every leaf so both
positions parse, then merges any leaf-level value into
``ctx.obj["provider_opts"]`` so downstream code (``build_client``,
``require_persona_ack``, the JSON renderer, ...) sees a single resolved
view regardless of where the user typed the flag.

Hidden=True keeps each leaf's ``--help`` clean -- the canonical
documentation lives at the group level.

Decorator placement (matters):

    @subgroup.command(...)
    @click.argument(...)        # leaf-specific args/options on top
    @with_provider_overrides    # adds the 7 group flags + merges into ctx
    @click.pass_context         # innermost; wraps the actual handler
    def handler(ctx, ...): ...

Putting ``with_provider_overrides`` directly above ``pass_context`` means
it sees ctx via :func:`click.get_current_context`, doesn't compete with
``pass_context`` for argument injection, and leaves the leaf's own
arguments / options untouched.
"""

from __future__ import annotations

import functools
from typing import Callable, TypeVar

import click

F = TypeVar("F", bound=Callable[..., object])

# (kwarg name on the wrapper, key in ``provider_opts``, is_flag).
# Kwargs are prefixed with ``_override_`` so they cannot collide with a
# leaf's own option names.
_MERGE: tuple[tuple[str, str, bool], ...] = (
    ("_override_coldkey", "coldkey", False),
    ("_override_hotkey", "hotkey", False),
    ("_override_portal_url", "portal_url", False),
    ("_override_json", "json", True),
    ("_override_debug", "debug", True),
    ("_override_yes", "yes", True),
    ("_override_dry_run", "dry_run", True),
)


def with_provider_overrides(f: F) -> F:
    """Add hidden duplicates of the group flags so they also work at leaf position."""

    @functools.wraps(f)
    def wrapper(*args: object, **kwargs: object) -> object:
        ctx = click.get_current_context()
        ctx.ensure_object(dict)
        opts = ctx.obj.setdefault("provider_opts", {})
        for src, dst, is_flag in _MERGE:
            val = kwargs.pop(src, None)
            if is_flag:
                if val:
                    opts[dst] = True
            elif val is not None:
                opts[dst] = val
        return f(*args, **kwargs)

    # Apply options bottom-up. Inner-most ``click.option`` calls are
    # applied first, so the visual order in --help (when not hidden)
    # matches the order written here. Order is irrelevant while hidden.
    wrapper = click.option(
        "--dry-run",
        "_override_dry_run",
        is_flag=True,
        default=False,
        hidden=True,
        help="Same as the group --dry-run flag; allowed at leaf position too.",
    )(wrapper)
    wrapper = click.option(
        "--yes",
        "-y",
        "_override_yes",
        is_flag=True,
        default=False,
        hidden=True,
        help="Same as the group --yes flag; allowed at leaf position too.",
    )(wrapper)
    wrapper = click.option(
        "--debug",
        "_override_debug",
        is_flag=True,
        default=False,
        hidden=True,
        help="Same as the group --debug flag; allowed at leaf position too.",
    )(wrapper)
    wrapper = click.option(
        "--json",
        "_override_json",
        is_flag=True,
        default=False,
        hidden=True,
        help="Same as the group --json flag; allowed at leaf position too.",
    )(wrapper)
    wrapper = click.option(
        "--portal-url",
        "_override_portal_url",
        default=None,
        envvar="LIUM_PORTAL_URL",
        hidden=True,
        help="Same as the group --portal-url flag.",
    )(wrapper)
    wrapper = click.option(
        "--hotkey",
        "-k",
        "_override_hotkey",
        default=None,
        envvar="LIUM_PROVIDER_HOTKEY",
        hidden=True,
        help="Same as the group --hotkey flag.",
    )(wrapper)
    wrapper = click.option(
        "--coldkey",
        "-w",
        "_override_coldkey",
        default=None,
        envvar="LIUM_PROVIDER_COLDKEY",
        hidden=True,
        help="Same as the group --coldkey flag.",
    )(wrapper)
    return wrapper  # type: ignore[return-value]


__all__ = ["with_provider_overrides"]
