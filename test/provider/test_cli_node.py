"""CLI tests for ``lium provider node …`` (node lifecycle).

Mutating commands pass ``-y`` (the global ``--yes`` flag) so the persona
gate short-circuits silently. Read-only commands (list/get/pods/...) skip
the gate entirely.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from lium.cli.provider.command import provider_command
from lium.provider.auth import LocalKeypairSigner
from lium.provider.client import ProviderClient
from lium.provider.errors import ProviderError, ProviderNotFoundError
from lium.provider.token_store import TokenStore


class _Portal:
    def __init__(
        self,
        *,
        get_body: dict | None = None,
        post_body: dict | None = None,
        delete_body: dict | None = None,
        get_raises: BaseException | None = None,
        post_raises: BaseException | None = None,
        delete_raises: BaseException | None = None,
    ):
        self._get_body = get_body
        self._post_body = post_body
        self._delete_body = delete_body
        self._get_raises = get_raises
        self._post_raises = post_raises
        self._delete_raises = delete_raises
        self.posts: list[Any] = []
        self.gets: list[Any] = []
        self.deletes: list[Any] = []

    def get(self, path, *, params=None, auth=True):
        self.gets.append((path, params, auth))
        if self._get_raises:
            raise self._get_raises
        return self._get_body or {}

    def post(self, path, *, json_body=None, auth=True):
        self.posts.append((path, json_body, auth))
        if self._post_raises:
            raise self._post_raises
        return self._post_body or {}

    def put(self, *a, **k):  # pragma: no cover
        return {}

    def delete(self, path, *, auth=True):
        self.deletes.append((path, auth))
        if self._delete_raises:
            raise self._delete_raises
        return self._delete_body or {}


@pytest.fixture
def patched_build_client(
    monkeypatch, fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore
):
    def _factory(portal: _Portal):
        def _builder(ctx):
            return ProviderClient(
                signer=fake_signer,
                token_store=tmp_token_store,
                http=portal,  # type: ignore[arg-type]
            )

        monkeypatch.setattr("lium.cli.provider.node.build_client", _builder)
        return portal

    return _factory


def test_node_list_renders_summary(patched_build_client) -> None:
    portal = _Portal(get_body={"data": [{"id": "e-1"}, {"id": "e-2"}], "total": 2})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "node", "list"],
    )
    assert result.exit_code == 0, result.output
    assert portal.gets[0][0] == "/executors"


def _node_row(node_id: str, ip: str, status: str | None) -> dict:
    # a price with four decimals and a two-figure revenue: values a ratio column cuts to `$1.2…` / `$23.…` at 80
    row = {
        "id": node_id,
        "executor_ip_address": ip,
        "executor_ip_port": "8080",
        "price_per_gpu": 1.2345,
        "gpu_count": 8,
        "rented_gpu_count": 3,
        "revenue_per_hour": 23.75,
        "gpu_type": "RTX 4090",
    }
    if status:
        row["computed_status"] = {"status": status, "message": "…"}
    return row


def test_node_list_shows_computed_status_column(patched_build_client) -> None:
    # The portal returns computed_status on every row; the human table must show it whole, or a
    # VALIDATION_FAILED node looks identical to an AVAILABLE one — and, at the 80 columns a provider's
    # terminal has by default, to a VALIDATION_PENDING one (e4e3a85 rendered both as `VALIDATI…`).
    rows = [
        _node_row("e-1", "203.0.113.4", "AVAILABLE"),
        _node_row("e-2", "203.0.113.5", "VALIDATION_PENDING"),
        _node_row("e-3", "203.0.113.6", "VALIDATION_FAILED"),
        _node_row("e-4", "203.0.113.7", None),
    ]
    portal = _Portal(get_body={"data": rows, "total": 4})
    patched_build_client(portal)
    runner = CliRunner()
    # Rich sizes the table from COLUMNS (ignored under TERM=dumb): 80 is the width that used to cut the words
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "node", "list"],
        env={"COLUMNS": "80", "TERM": "xterm-256color"},
    )
    assert result.exit_code == 0, result.output
    assert "Status" in result.output
    lines = result.output.splitlines()
    assert any("AVAILABLE" in line and "e-1" in line for line in lines), result.output
    assert any("VALIDATION_PENDING" in line and "e-2" in line for line in lines), result.output
    assert any("VALIDATION_FAILED" in line and "e-3" in line for line in lines), result.output
    assert "VALIDATI…" not in result.output
    # the figures on the right are content-sized too: on e4e3a85 the ratio layout rendered `$23.…` at 80
    for line in lines:
        if line.strip().startswith(("1 ", "2 ", "3 ", "4 ")) and "e-" in line:
            assert "$1.2345" in line and "$23.75" in line and "3/8" in line, line
    assert "$1.2…" not in result.output and "$23.…" not in result.output
    # A row without computed_status renders a dash, not a crash.
    assert any("e-4" in line and "—" in line for line in lines), result.output


@pytest.mark.parametrize("columns", ["40", "60", "66"])
def test_node_list_still_fits_a_terminal_too_narrow_for_the_fixed_columns(patched_build_client, columns) -> None:
    """Below 67 columns the content-sized columns plus three characters per flexible one no longer fit; the table
    goes back to the ratio layout (ellipses everywhere, as on e4e3a85) instead of running past the right edge."""
    rows = [_node_row("e-1", "203.0.113.4", "VALIDATION_PENDING"), _node_row("e-2", "203.0.113.5", "AVAILABLE")]
    patched_build_client(_Portal(get_body={"data": rows, "total": 2}))
    result = CliRunner().invoke(provider_command, ["--hotkey", "hk1", "node", "list"], env={"COLUMNS": columns, "TERM": "xterm-256color"})
    assert result.exit_code == 0, result.output
    width = int(columns)
    lines = result.output.splitlines()
    for line in lines:
        assert len(line) <= width, f"{len(line)} > {width}: {line!r}"
    # all eight columns are still on screen (ellipsised down to `…` at 40), where a fixed layout that does not fit
    # is cropped by Rich at the right edge and the money columns vanish
    header = next(line for line in lines if line.split()[:1] == ["#"])   # `node list: …` summary and a blank line come first
    assert len(header.split()) == 8, header


def test_node_list_status_column_shrinks_to_its_content(patched_build_client) -> None:
    """Content-sized means no wasted width either: a listing of short statuses leaves the room to the ID."""
    rows = [_node_row("8f3c2a1e-9b7d-4c6a-a1f2-0123456789ab", "203.0.113.4", "AVAILABLE")]
    patched_build_client(_Portal(get_body={"data": rows, "total": 1}))
    result = CliRunner().invoke(provider_command, ["--hotkey", "hk1", "node", "list"], env={"COLUMNS": "80", "TERM": "xterm-256color"})
    assert result.exit_code == 0, result.output
    header = next(line for line in result.output.splitlines() if "Status" in line and "ID" in line)
    # the Status column is as wide as its longest cell, not 18: the ID header starts right after `AVAILABLE `
    assert header.index("ID") - header.index("Status") <= len("AVAILABLE") + 3, header


def test_node_list_defaults_to_own_hotkey(
    patched_build_client, fake_signer: LocalKeypairSigner
) -> None:
    """The portal's ``GET /executors`` is a global listing; without a
    ``miner_hotkey`` filter a provider with zero nodes sees other providers'
    machines listed as its own. Default the filter to the active hotkey."""
    portal = _Portal(get_body={"data": [], "total": 0})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(provider_command, ["--hotkey", "hk1", "node", "list"])
    assert result.exit_code == 0, result.output
    path, params, _ = portal.gets[0]
    assert path == "/executors"
    assert params == {"miner_hotkey": fake_signer.ss58_address}


def test_node_list_all_drops_hotkey_filter(patched_build_client) -> None:
    portal = _Portal(get_body={"data": [{"id": "e-1"}], "total": 1})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "--json", "node", "list", "--all", "--limit", "5"],
    )
    assert result.exit_code == 0, result.output
    path, params, _ = portal.gets[0]
    assert path == "/executors"
    assert params == {"limit": 5}


def test_node_list_all_and_miner_hotkey_are_exclusive(patched_build_client) -> None:
    portal = _Portal(get_body={"data": [], "total": 0})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        [
            "--hotkey",
            "hk1",
            "--json",
            "node",
            "list",
            "--all",
            "--miner-hotkey",
            "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        ],
    )
    assert result.exit_code != 0
    payload = json.loads(result.output.strip())
    assert payload["ok"] is False
    assert payload["error"]["code"] == "ARG_INVALID"
    assert portal.gets == []


def test_node_list_json(patched_build_client) -> None:
    portal = _Portal(get_body={"data": [{"id": "e-1"}], "total": 1})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "--json", "node", "list"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    assert payload["ok"] is True
    assert payload["data"]["total"] == 1


def test_node_list_filters(patched_build_client) -> None:
    portal = _Portal(get_body={"data": [], "total": 0})
    patched_build_client(portal)
    runner = CliRunner()
    miner_hotkey = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
    result = runner.invoke(
        provider_command,
        [
            "--hotkey",
            "hk1",
            "node",
            "list",
            "--miner-hotkey",
            miner_hotkey,
            "--page",
            "2",
            "--limit",
            "10",
        ],
    )
    assert result.exit_code == 0, result.output
    path, params, _ = portal.gets[0]
    assert path == "/executors"
    assert params == {"miner_hotkey": miner_hotkey, "page": 2, "limit": 10}


def test_node_get_renders(patched_build_client) -> None:
    portal = _Portal(get_body={"id": "e-1", "gpu_type": "H100"})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "node", "get", "e-1"],
    )
    assert result.exit_code == 0, result.output


def test_node_get_prints_status_message_and_last_error(patched_build_client) -> None:
    portal = _Portal(
        get_body={
            "id": "e-1",
            "gpu_type": "H100",
            "computed_status": {
                "status": "VALIDATION_FAILED",
                "message": "VerifyX validation failed (network speed too slow) (last success: 3 hours ago)",
                "last_error": {
                    "title": "VerifyX validation failed (network speed too slow)",
                    "message": "VerifyX validation failed (network speed too slow)",
                    "source": "Validator",
                    "reason_code": "VERIFYX_FAILED_NETWORK_SPEED_TOO_SLOW",
                    "impact": "Score set to 0",
                    "remediation": "EMA download speed is below the minimum threshold.",
                    "what_we_saw": {"ema_verifyx_download_speed": 61.2},
                },
                "last_successful_validation": None,
            },
        }
    )
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "node", "get", "e-1"],
        terminal_width=200,
    )
    assert result.exit_code == 0, result.output
    assert "VALIDATION_FAILED" in result.output
    assert "network speed too slow" in result.output
    assert "Impact: Score set to 0" in result.output
    assert "Fix: EMA download speed is below the minimum threshold." in result.output
    # The dict is rendered once, as Status / Last Error, not also as a collapsed blob.
    assert "{4 fields}" not in result.output
    assert "Computed Status" not in result.output


def test_node_get_prints_bracketed_validator_text_verbatim(patched_build_client) -> None:
    """The validator's free text goes through Rich markup: `[/var/log/x]` used to be parsed as a closing tag and
    `node get` exited 1 with MarkupError; `[word]` was eaten as a style."""
    portal = _Portal(
        get_body={
            "id": "e-1",
            "gpu_type": "H100",
            "computed_status": {
                "status": "VALIDATION_FAILED",
                "message": "validator log [truncated]",
                "last_error": {
                    "title": "docker inspect failed [exit 1]",
                    "impact": "Score set to 0",
                    "remediation": "see [/var/log/executor.log] and [bold] lines",
                },
            },
        }
    )
    patched_build_client(portal)
    result = CliRunner().invoke(provider_command, ["--hotkey", "hk1", "node", "get", "e-1"], terminal_width=200)
    assert result.exit_code == 0, result.output
    for verbatim in ("validator log [truncated]", "docker inspect failed [exit 1]", "see [/var/log/executor.log] and [bold] lines"):
        assert verbatim in result.output, result.output


def test_node_get_status_without_last_error(patched_build_client) -> None:
    portal = _Portal(
        get_body={
            "id": "e-1",
            "computed_status": {
                "status": "AVAILABLE",
                "message": "8 of 8 GPUs ready for rent",
                "last_error": None,
                "last_successful_validation": "2026-09-06T10:00:00Z",
            },
        }
    )
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "node", "get", "e-1"],
        terminal_width=200,
    )
    assert result.exit_code == 0, result.output
    assert "AVAILABLE" in result.output
    assert "8 of 8 GPUs ready for rent" in result.output
    assert "Last Error" not in result.output


def test_node_get_rejects_path_traversal_in_id(patched_build_client) -> None:
    portal = _Portal(get_body={})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "node", "get", "../auth/me"],
    )
    assert result.exit_code == 1, result.output
    assert "ARG_INVALID" in result.output
    assert portal.gets == []


def test_node_add_posts_payload(patched_build_client) -> None:
    portal = _Portal(post_body={"message": "queued"})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        [
            "-y",
            "--hotkey",
            "hk1",
            "node",
            "add",
            "--gpu-type",
            "H100",
            "--ip",
            "10.0.0.1",
            "--port",
            "8080",
            "--price",
            "1.5",
            "--gpu-count",
            "8",
        ],
    )
    assert result.exit_code == 0, result.output
    assert portal.posts[0][0] == "/executors"
    assert portal.posts[0][1] == {
        "gpu_type": "H100",
        "ip_address": "10.0.0.1",
        "port": 8080,
        "price_per_gpu": 1.5,
        "gpu_count": 8,
    }


def test_node_add_requires_hotkey() -> None:
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        [
            "node",
            "add",
            "--gpu-type",
            "H100",
            "--ip",
            "10.0.0.1",
            "--price",
            "1.5",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "ARG_INVALID" in result.output


def test_node_add_rejects_zero_gpu_count(patched_build_client) -> None:
    portal = _Portal(post_body={})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        [
            "-y",
            "--hotkey",
            "hk1",
            "node",
            "add",
            "--gpu-type",
            "H100",
            "--ip",
            "10.0.0.1",
            "--price",
            "1.5",
            "--gpu-count",
            "0",
        ],
    )
    assert result.exit_code != 0, result.output
    assert portal.posts == []


def test_node_add_auto_fills_price_from_shared_config(
    patched_build_client, monkeypatch
) -> None:
    portal = _Portal(post_body={"message": "queued"})
    patched_build_client(portal)
    from lium.provider._shared_config import SharedConfigSnapshot

    snapshot = SharedConfigSnapshot(
        machine_prices={"NVIDIA H100 80GB HBM3": 1.26},
        machine_min_price_rate=0.5,
        machine_max_price_rate=3.0,
    )
    fetch_calls: list[None] = []

    def _fake_fetch(*, url=None, timeout=10.0, session=None):
        fetch_calls.append(None)
        return snapshot

    monkeypatch.setattr("lium.cli.provider.node.fetch_shared_config", _fake_fetch)

    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        [
            "-y",
            "--hotkey",
            "hk1",
            "node",
            "add",
            "--gpu-type",
            "NVIDIA H100 80GB HBM3",
            "--ip",
            "10.0.0.1",
            "--gpu-count",
            "8",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(fetch_calls) == 1
    assert portal.posts[0][1]["price_per_gpu"] == 1.26
    assert "default price" in result.output.lower()


def test_node_add_unknown_gpu_type_errors_when_price_omitted(
    patched_build_client, monkeypatch
) -> None:
    portal = _Portal(post_body={})
    patched_build_client(portal)
    from lium.provider._shared_config import SharedConfigSnapshot

    snapshot = SharedConfigSnapshot(
        machine_prices={"NVIDIA H100 80GB HBM3": 1.26},
        machine_min_price_rate=0.5,
        machine_max_price_rate=3.0,
    )
    monkeypatch.setattr(
        "lium.cli.provider.node.fetch_shared_config",
        lambda **_: snapshot,
    )

    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        [
            "-y",
            "--hotkey",
            "hk1",
            "node",
            "add",
            "--gpu-type",
            "NVIDIA NONEXISTENT",
            "--ip",
            "10.0.0.1",
        ],
    )
    assert result.exit_code != 0, result.output
    assert "ARG_INVALID" in result.output
    assert portal.posts == []


def test_node_add_explicit_price_skips_shared_config(
    patched_build_client, monkeypatch
) -> None:
    """Passing ``--price`` must not hit the public shared-config endpoint."""
    portal = _Portal(post_body={"message": "queued"})
    patched_build_client(portal)
    fetch_calls: list[None] = []

    def _should_not_be_called(**_kwargs):  # pragma: no cover
        fetch_calls.append(None)
        raise AssertionError("fetch_shared_config must not run when --price is supplied")

    monkeypatch.setattr(
        "lium.cli.provider.node.fetch_shared_config", _should_not_be_called
    )

    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        [
            "-y",
            "--hotkey",
            "hk1",
            "node",
            "add",
            "--gpu-type",
            "H100",
            "--ip",
            "10.0.0.1",
            "--price",
            "1.5",
        ],
    )
    assert result.exit_code == 0, result.output
    assert fetch_calls == []
    assert portal.posts[0][1]["price_per_gpu"] == 1.5


def test_node_rm_calls_delete(patched_build_client) -> None:
    portal = _Portal(delete_body={"message": "ok"})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["-y", "--hotkey", "hk1", "node", "rm", "e-1"],
    )
    assert result.exit_code == 0, result.output
    assert portal.deletes == [("/executors/e-1", True)]


def test_node_update_price(patched_build_client) -> None:
    portal = _Portal(post_body={"message": "ok"})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        [
            "-y",
            "--hotkey",
            "hk1",
            "node",
            "update-price",
            "e-1",
            "--price",
            "2.5",
        ],
    )
    assert result.exit_code == 0, result.output
    assert portal.posts[0][0] == "/executors/e-1/update-price"
    assert portal.posts[0][1] == {"price_per_gpu": 2.5}


def test_node_update_gpu(patched_build_client) -> None:
    portal = _Portal(post_body={"data": {}})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        [
            "-y",
            "--hotkey",
            "hk1",
            "node",
            "update-gpu",
            "e-1",
            "--gpu-type",
            "H100",
            "--gpu-count",
            "8",
        ],
    )
    assert result.exit_code == 0, result.output
    assert portal.posts[0][0] == "/executors/e-1/update-gpu"
    assert portal.posts[0][1] == {"gpu_type": "H100", "gpu_count": 8}


def test_node_min_gpu_set(patched_build_client) -> None:
    portal = _Portal(post_body={"data": {}})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["-y", "--hotkey", "hk1", "node", "min-gpu", "set", "e-1", "4"],
    )
    assert result.exit_code == 0, result.output
    assert portal.posts[0][0] == "/executors/e-1/min-gpu-count-for-rental"
    assert portal.posts[0][1] == {"min_gpu_count_for_rental": 4}


def test_node_min_gpu_unset(patched_build_client) -> None:
    portal = _Portal(delete_body={"data": {}})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["-y", "--hotkey", "hk1", "node", "min-gpu", "unset", "e-1"],
    )
    assert result.exit_code == 0, result.output
    assert portal.deletes == [("/executors/e-1/min-gpu-count-for-rental", True)]


def test_node_pods(patched_build_client) -> None:
    portal = _Portal(get_body={"data": [{"id": "p-1"}, {"id": "p-2"}]})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "node", "pods", "e-1"],
    )
    assert result.exit_code == 0, result.output


def test_node_machine_requests(patched_build_client) -> None:
    portal = _Portal(get_body={"data": []})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "node", "machine-requests", "e-1"],
    )
    assert result.exit_code == 0, result.output


def test_node_notice_period_set_and_unset(patched_build_client) -> None:
    portal = _Portal(post_body={}, delete_body={})
    patched_build_client(portal)
    runner = CliRunner()

    result = runner.invoke(
        provider_command,
        ["-y", "--hotkey", "hk1", "node", "notice-period", "set", "e-1"],
    )
    assert result.exit_code == 0, result.output
    assert portal.posts[0][0] == "/executors/e-1/notice-period"

    result = runner.invoke(
        provider_command,
        ["-y", "--hotkey", "hk1", "node", "notice-period", "unset", "e-1"],
    )
    assert result.exit_code == 0, result.output
    assert portal.deletes == [("/executors/e-1/notice-period", True)]


def test_node_notify_added(patched_build_client) -> None:
    portal = _Portal(post_body={"message": "ok"})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        [
            "-y",
            "--hotkey",
            "hk1",
            "node",
            "notify-added",
            "e-1",
            "--request-id",
            "r-9",
        ],
    )
    assert result.exit_code == 0, result.output
    assert portal.posts[0][0] == "/executors/e-1/machine-added"
    assert portal.posts[0][1] == {"machine_request_id": "r-9"}


def test_node_get_404_returns_exit_3(patched_build_client) -> None:
    portal = _Portal(get_raises=ProviderNotFoundError("not found"))
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "node", "get", "e-missing"],
    )
    assert result.exit_code == 3, result.output


def test_node_list_generic_provider_error_returns_1(patched_build_client) -> None:
    portal = _Portal(get_raises=ProviderError("boom", code="ARG_INVALID"))
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "node", "list"],
    )
    assert result.exit_code == 1, result.output
