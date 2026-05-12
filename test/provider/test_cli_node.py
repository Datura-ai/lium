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
