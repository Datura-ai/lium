"""The unified exit map for namespaced provider codes, and docs/exit-codes.md in step with it."""

import re
from pathlib import Path

import pytest

from lium.cli.provider._render import exit_code_for
from lium.provider import errors
from lium.provider.errors import ProviderError, unified_exit_code

DOC = Path(__file__).resolve().parents[2] / "docs" / "exit-codes.md"


@pytest.mark.parametrize(
    ("code", "status", "expected"),
    [
        ("input.confirmation_required", None, 2),
        ("input.input_required", None, 2),
        ("net.unreachable", None, 4),
        ("ssh.unreachable", None, 4),
        ("host.port_in_use", None, 1),
        ("auth.expired", None, 6),
        ("human.browser_step", None, 12),
        ("node.not_found", None, 5),
        ("node.not_listed_yet", None, 11),
        ("node.blocked.tier_locked", None, 10),
        ("portal.not_supported", 404, 3),
        ("portal.executor_not_found", None, 5),
        ("portal.anything", 404, 5),
        ("portal.session_revoked", 401, 6),
        ("portal.forbidden_for_hotkey", 403, 6),
        ("portal.slow_down", 429, 7),
        ("portal.bad_price", 400, 3),
        ("portal.bad_gateway", 502, 3),
        ("something.else", None, 1),
    ],
)
def test_unified_exit_code(code, status, expected):
    assert unified_exit_code(code, status) == expected


def test_namespaced_codes_exit_by_the_unified_map_and_legacy_codes_keep_theirs():
    assert exit_code_for(ProviderError("x", code=errors.NET_UNREACHABLE)) == 4
    assert exit_code_for(ProviderError("x", code="portal.slow_down", context={"status": 429})) == 7
    assert exit_code_for(ProviderError("x", code=errors.PORTAL_SERVER_ERROR)) == 3
    assert exit_code_for(ProviderError("x", code=errors.PORTAL_AUTH_INVALID)) == 2
    assert exit_code_for(ProviderError("x", code=errors.SSH_UNREACHABLE)) == 5


@pytest.mark.parametrize(
    "name", ["EXIT_OK", "EXIT_GENERAL", "EXIT_INPUT", "EXIT_API", "EXIT_NETWORK", "EXIT_NOT_FOUND",
             "EXIT_AUTH", "EXIT_RETRYABLE", "EXIT_BLOCKED", "EXIT_NOT_LISTED", "EXIT_HUMAN"],
)
def test_every_unified_exit_is_documented(name):
    value = getattr(errors, name)
    assert re.search(rf"^\|\s*{value}\s*\|\s*`{name}`", DOC.read_text(), re.M), f"{name}={value} missing"


@pytest.mark.parametrize(
    "code", [errors.INPUT_REQUIRED, errors.CONFIRMATION_REQUIRED, errors.NET_UNREACHABLE,
             errors.PORTAL_NOT_SUPPORTED, errors.NODE_NOT_LISTED, "host.port_in_use", "input.register_token_invalid"],
)
def test_every_namespaced_code_is_documented(code):
    assert f"`{code}`" in DOC.read_text()


def test_the_old_to_new_code_table_names_each_rename():
    doc = DOC.read_text()
    table = doc[doc.index("### Old codes and their new names"):]
    assert "| `PORTAL_SERVER_ERROR` (3) | `net.unreachable` (4)" in table
    assert "`input.confirmation_required` (2)" in table
