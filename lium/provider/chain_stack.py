"""Why the chain stack may be missing, in words a caller can act on.

The chain stack is optional (DAH-2553): renting never needs it, and its build
fails on Python 3.14. A caller that hits this needs to know which of the two
situations it is in, because the fix is different — and, when the fix is
"install the extra", the command that does it depends on how ``lium`` itself
was installed (DAH-2943: a ``uv tool`` / ``mine.sh`` install told to
``pip install`` puts the extra into a Python the CLI does not run from).
"""

import os
import sys
from pathlib import Path

LAST_SUPPORTED_PYTHON = (3, 13)

# `uv tool install` and pipx each leave a marker at the root of the venv they
# manage; the standalone binary runs frozen. Anything else is a plain pip/venv.
_UV_TOOL_MARKER = "uv-receipt.toml"
_PIPX_MARKER = "pipx_metadata.json"
# The same line as lium/cli/self_update.py's REINSTALL_COMMAND; provider code does not import the CLI, so it is
# spelled here too and test_the_reinstall_command_is_the_self_update_one keeps the two in step.
REINSTALL_COMMAND = "curl -fsSL https://lium.io/install.sh | bash"


def _venv_paths() -> list[str]:
    """Where this venv lives, in every spelling the installer's directory can show up in.

    The venv root (``sys.prefix``) and the interpreter as invoked (``sys.executable``), each as
    given and resolved. Neither is resolved *alone*: a uv tool venv's ``bin/python`` is a symlink
    to uv's shared interpreter and a pipx venv's to the system one, so the resolved interpreter
    path loses the ``/uv/tools/`` or ``/pipx/venvs/`` mark — while on macOS CPython hands out
    ``sys.prefix`` with directory symlinks already resolved (``/tmp`` → ``/private/tmp``), so a
    ``$UV_TOOL_DIR`` typed through a symlink only matches once both sides are resolved.
    """
    paths: list[str] = []
    for raw in (sys.prefix, sys.executable):
        for candidate in (Path(raw), Path(raw).resolve()):
            spelled = candidate.as_posix()
            if spelled not in paths:
                paths.append(spelled)
    return paths


def _under(path: str, directory: str) -> bool:
    return (path + "/").startswith(directory.rstrip("/") + "/")


def install_command() -> str:
    """The command that adds the chain stack to *this* installation of lium.

    ``pip install`` is wrong for the two ways providers actually install: ``mine.sh``
    runs ``uv tool install lium.io`` (a tool venv pip cannot reach) and the docs' curl
    installer ships a frozen binary that carries the stack prebuilt (DAH-2943). The
    installer is recognised by its marker at the venv root (``uv-receipt.toml``,
    ``pipx_metadata.json``) or, failing that, by where the venv lives
    (``…/uv/tools/…``, ``$UV_TOOL_DIR``, ``…/pipx/venvs/…`` — see ``_venv_paths``).
    """
    if getattr(sys, "frozen", False):
        return f"reinstall the standalone binary: {REINSTALL_COMMAND}"
    paths = _venv_paths()
    uv_tool_dir = os.environ.get("UV_TOOL_DIR")
    uv_tool_dirs = [Path(uv_tool_dir).as_posix(), Path(uv_tool_dir).resolve().as_posix()] if uv_tool_dir else []
    if (
        os.path.exists(os.path.join(sys.prefix, _UV_TOOL_MARKER))
        or any("/uv/tools/" in p + "/" for p in paths)
        or any(_under(p, d) for p in paths for d in uv_tool_dirs)
    ):
        return 'uv tool install --force "lium.io[provider]"'
    if os.path.exists(os.path.join(sys.prefix, _PIPX_MARKER)) or any("/pipx/venvs/" in p + "/" for p in paths):
        return 'pipx install --force "lium.io[provider]"'
    return 'pip install "lium.io[provider]"'


def missing_chain_stack_message() -> str:
    """Explain the absence and name the fix for this interpreter."""
    running_python = sys.version_info[:2]
    if running_python > LAST_SUPPORTED_PYTHON:
        running = f"{running_python[0]}.{running_python[1]}"
        return (
            f"the chain stack does not build on Python {running} (bittensor-drand "
            f"needs a Rust toolchain and fails there). Use Python "
            f"{LAST_SUPPORTED_PYTHON[0]}.{LAST_SUPPORTED_PYTHON[1]} or the standalone "
            f"binary from https://lium.io/install.sh, which ships it prebuilt"
        )
    return f"the chain stack is not installed: {install_command()}"
