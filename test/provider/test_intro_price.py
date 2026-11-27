"""DAH-7294: ``lium provider node intro-price`` sets or clears a node's intro price on the portal."""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from lium.cli.provider.command import provider_command
from lium.provider.client import ProviderClient
from lium.provider.errors import ProviderError


class _Portal:
    def __init__(self) -> None:
        self.puts: list[Any] = []

    def put(self, path, *, json_body=None, auth=True):
        self.puts.append((path, json_body))
        return {"success": True}


def _client(fake_signer, tmp_token_store, portal: _Portal) -> ProviderClient:
    return ProviderClient(signer=fake_signer, token_store=tmp_token_store, http=portal)  # type: ignore[arg-type]


def test_set_and_clear_send_the_portal_payload(fake_signer, tmp_token_store) -> None:
    portal = _Portal()
    client = _client(fake_signer, tmp_token_store, portal)

    client.set_node_intro_price("node-1", 1.5, 168)
    client.set_node_intro_price("node-1", None, None)

    assert portal.puts == [
        ("/executors/node-1/intro-price", {"intro_price_per_gpu": 1.5, "intro_hours": 168}),
        ("/executors/node-1/intro-price", {"intro_price_per_gpu": None, "intro_hours": None}),
    ]


@pytest.mark.parametrize("price,hours", [(1.5, None), (None, 48), (0.0, 48), (1.5, 23), (1.5, 721)])
def test_a_half_or_out_of_range_intro_is_refused_before_any_request(
    fake_signer, tmp_token_store, price, hours
) -> None:
    portal = _Portal()
    with pytest.raises(ProviderError):
        _client(fake_signer, tmp_token_store, portal).set_node_intro_price("node-1", price, hours)
    assert portal.puts == []


@pytest.mark.parametrize(
    "extra", [[], ["--clear", "--price", "1"], ["--price", "1"], ["--hours", "48"]]
)
def test_the_command_needs_price_and_hours_or_clear(extra) -> None:
    result = CliRunner().invoke(provider_command, ["--hotkey", "hk1", "node", "intro-price", "node-1", *extra])

    assert result.exit_code == 2
    assert "give --price and --hours, or --clear" in result.output


def test_the_command_sends_the_intro(monkeypatch) -> None:
    calls: list[Any] = []

    class _Client:
        def set_node_intro_price(self, *args):
            calls.append(args)
            return {"success": True}

    monkeypatch.setattr("lium.cli.provider.node.build_client", lambda *_: _Client())
    result = CliRunner().invoke(
        provider_command,
        ["-y", "--hotkey", "hk1", "node", "intro-price", "node-1", "--price", "1.5", "--hours", "168"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("node-1", 1.5, 168)]
