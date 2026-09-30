"""The three switches that make a run agent mode, and reading the error a run under one of them printed.

``--json``, ``LIUM_OUTPUT=json`` and ``LIUM_NONINTERACTIVE=1`` select the same exit map; only the first two print
the JSON envelope. ``LIUM_NONINTERACTIVE=1`` alone prints ``[<code>] <message>`` and ``  hint: …`` on stderr.
"""

from __future__ import annotations

import json

import pytest

AGENT_SWITCHES = [
    pytest.param((("--json",), {}), id="--json"),
    pytest.param(((), {"LIUM_OUTPUT": "json"}), id="LIUM_OUTPUT=json"),
    pytest.param(((), {"LIUM_NONINTERACTIVE": "1"}), id="LIUM_NONINTERACTIVE=1"),
]
PLAIN_TEXT = {"LIUM_OUTPUT": "", "LIUM_NONINTERACTIVE": ""}


def prints_json(switch) -> bool:
    flags, env = switch
    return bool(flags) or env.get("LIUM_OUTPUT") == "json"


def read_error(result, switch) -> tuple[str, str, str]:
    """``(code, message, hint)`` of the failure ``result`` printed under ``switch``; the envelope's
    ``exit_code`` is checked against the process exit on the way."""
    if prints_json(switch):
        error = json.loads(result.stdout)["error"]
        assert error["exit_code"] == result.exit_code
        return error["code"], error["message"], error["hint"] or ""
    lines = result.stderr.splitlines()
    assert lines and lines[0].startswith("["), result.output
    code, _, message = lines[0][1:].partition("] ")
    hint = lines[1].removeprefix("  hint: ") if len(lines) > 1 and lines[1].startswith("  hint: ") else ""
    return code, message, hint
