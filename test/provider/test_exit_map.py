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
        ("human.handoff_required", None, 12),
        ("human.handoff_expired", None, 12),
        ("portal.api_token_needs_session", None, 6),
        ("portal.overview_not_for_custodied_account", None, 6),
        ("input.interrupted", None, 130),
        ("auth.refresh_race", None, 7),
        ("portal.api_token_scope_missing", None, 6),
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


def test_text_mode_keeps_the_old_exits_and_json_uses_the_unified_map():
    coded = ProviderError("x", code="portal.slow_down", context={"status": 429}, legacy_code=errors.PORTAL_RATE_LIMIT)
    assert (exit_code_for(coded), exit_code_for(coded, json_mode=True)) == (3, 7)
    unreachable = ProviderError("x", code=errors.NET_UNREACHABLE, legacy_code=errors.PORTAL_SERVER_ERROR)
    assert (exit_code_for(unreachable), exit_code_for(unreachable, json_mode=True)) == (3, 4)
    for old, text_exit, json_exit in ((errors.PORTAL_SERVER_ERROR, 3, 3), (errors.PORTAL_AUTH_INVALID, 2, 6),
                                      (errors.SSH_UNREACHABLE, 5, 4), (errors.ARG_INVALID, 1, 2)):
        err = ProviderError("x", code=old)
        assert (exit_code_for(err), exit_code_for(err, json_mode=True)) == (text_exit, json_exit), old
    new = ProviderError("x", code=errors.HANDOFF_REQUIRED)
    assert (exit_code_for(new), exit_code_for(new, json_mode=True)) == (12, 12)


def test_the_which_map_table_matches_both_modes():
    rows = re.findall(r"^\|\s*`([A-Z_]+)`\s*\|\s*(\d+)\s*\|[^|]*\|\s*(\d+)\s*\|$", _section("### Which exit map applies"), re.M)
    assert len(rows) >= 8
    for old, text_exit, json_exit in rows:
        err = ProviderError("x", code=old)
        assert (exit_code_for(err), exit_code_for(err, json_mode=True)) == (int(text_exit), int(json_exit)), old


def test_every_legacy_code_has_a_row_in_the_which_map_table():
    from lium.cli.provider._render import _EXIT_CODES, ERROR_CODES

    documented = set(re.findall(r"^\|\s*`([A-Z_]+)`\s*\|\s*\d+\s*\|", _section("### Which exit map applies"), re.M))
    assert set(_EXIT_CODES) | set(ERROR_CODES) <= documented, sorted((set(_EXIT_CODES) | set(ERROR_CODES)) - documented)


def test_a_token_cache_race_is_retryable_under_json():
    err = ProviderError("x", code=errors.PORTAL_AUTH_REFRESH_RACE)
    assert (exit_code_for(err), exit_code_for(err, json_mode=True)) == (7, 7)
    assert unified_exit_code(errors.AUTH_REFRESH_RACE) == 7


@pytest.mark.parametrize(
    "name", ["EXIT_OK", "EXIT_GENERAL", "EXIT_INPUT", "EXIT_API", "EXIT_NETWORK", "EXIT_NOT_FOUND",
             "EXIT_AUTH", "EXIT_RETRYABLE", "EXIT_BLOCKED", "EXIT_NOT_LISTED", "EXIT_HUMAN", "EXIT_INTERRUPTED"],
)
def test_every_unified_exit_is_documented(name):
    value = getattr(errors, name)
    assert re.search(rf"^\|\s*{value}\s*\|\s*`{name}`", DOC.read_text(), re.M), f"{name}={value} missing"


@pytest.mark.parametrize(
    "code", [errors.INPUT_REQUIRED, errors.CONFIRMATION_REQUIRED, errors.NET_UNREACHABLE,
             errors.PORTAL_NOT_SUPPORTED, errors.NODE_NOT_LISTED, "host.port_in_use", "input.register_token_invalid",
             errors.HANDOFF_REQUIRED, errors.HANDOFF_EXPIRED, errors.API_TOKEN_NEEDS_SESSION, errors.API_TOKEN_SCOPE_MISSING,
             errors.INTERRUPTED, errors.OVERVIEW_NOT_FOR_CUSTODIED_ACCOUNT],
)
def test_every_namespaced_code_is_documented(code):
    assert f"`{code}`" in DOC.read_text()


def _section(title: str) -> str:
    doc = DOC.read_text()
    start = doc.index(title)
    return doc[start:doc.index("\n#", start + len(title))]


def test_each_documented_code_exits_as_the_doc_says():
    rows = re.findall(r"^\|\s*`([a-z]+\.[a-z_]+)`\s*\|\s*(\d+)\s*\|", _section("### The unified exit map"), re.M)
    assert len(rows) >= 8
    wrong = {code: (int(doc_exit), unified_exit_code(code)) for code, doc_exit in rows
             if unified_exit_code(code) != int(doc_exit)}
    assert wrong == {}


def test_the_old_to_new_table_matches_both_maps():
    """Each `OLD (n)` must be the legacy map's exit and each `new (m)` the unified map's."""
    rows = re.findall(r"^\|\s*`([A-Z_]+)` \((\d+)\)\s*\|\s*`([a-z]+\.[a-z_]+)` \((\d+)\)", _section("### Old codes and their new names"), re.M)
    assert rows
    for old, old_exit, new, new_exit in rows:
        assert exit_code_for(ProviderError("x", code=old)) == int(old_exit), old
        assert unified_exit_code(new) == int(new_exit), new
