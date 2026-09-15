"""Why the chain stack may be missing, in words a caller can act on.

The chain stack is optional (DAH-2553): renting never needs it, and its build
fails on Python 3.14. A caller that hits this needs to know which of the two
situations it is in, because the fix is different.
"""

import os
import sys
from pathlib import Path

LAST_SUPPORTED_PYTHON = (3, 13)


def install_command() -> str:
    """The command that adds the chain stack to *this* installation of lium.

    ``pip install`` is wrong for the two ways providers actually install: ``mine.sh``
    runs ``uv tool install lium.io`` (a tool venv pip cannot reach) and the docs' curl
    installer ships a frozen binary that carries the stack prebuilt (DAH-2943).
    """
    if getattr(sys, "frozen", False):
        return (
            "this lium binary should ship it prebuilt; reinstall it with "
            "`curl -fsSL https://lium.io/install.sh | bash`"
        )
    # No resolve(): a uv tool venv's bin/python is a symlink to uv's shared interpreter and a
    # pipx venv's to the system one, so the resolved path loses the /uv/tools/ or /pipx/venvs/ mark.
    executable = Path(sys.executable).as_posix()
    uv_tool_dir = os.environ.get("UV_TOOL_DIR")
    if "/uv/tools/" in executable or (uv_tool_dir and executable.startswith(Path(uv_tool_dir).as_posix())):
        return 'uv tool install --force "lium.io[provider]"'
    if "/pipx/venvs/" in executable:
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
