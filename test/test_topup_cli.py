"""DAH-2110: `lium topup` stablecoin commands and JSON error handling.

`topup` lets a user (or an autonomous agent) fund their balance with a
stablecoin. Because the `--json` paths are meant for machine consumers, an
error under `--json` must be emitted as a parseable JSON envelope on stderr
with a non-zero exit code — never Rich-formatted text on stdout with a success
exit code. The human (non-`--json`) rendering is preserved.
"""

import json

from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.topup import command as topup_module
from lium.sdk import LiumError


def test_topup_currencies_json_success(monkeypatch):
    class FakeLium:
        def topup_currencies(self, refresh=False):
            return [
                {
                    "code": "USDT",
                    "network": "tron",
                    "decimals": 6,
                    "display_decimals": 2,
                }
            ]

    monkeypatch.setattr(topup_module, "Lium", FakeLium)

    result = CliRunner().invoke(cli, ["topup", "currencies", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "currencies": [
            {
                "code": "USDT",
                "network": "tron",
                "decimals": 6,
                "display_decimals": 2,
            }
        ]
    }


def test_topup_currencies_empty_json_emits_error_envelope(monkeypatch):
    # An empty list must not look like success under --json: the agent would
    # otherwise read `{"currencies": []}` and proceed with nothing to pay in.
    class FakeLium:
        def topup_currencies(self, refresh=False):
            return []

    monkeypatch.setattr(topup_module, "Lium", FakeLium)

    result = CliRunner().invoke(cli, ["topup", "currencies", "--json"])

    assert result.exit_code != 0
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert "No supported currencies" in payload["error"]["message"]


def test_topup_currencies_empty_human_errors(monkeypatch):
    class FakeLium:
        def topup_currencies(self, refresh=False):
            return []

    monkeypatch.setattr(topup_module, "Lium", FakeLium)

    result = CliRunner().invoke(cli, ["topup", "currencies"])

    assert "Error: No supported currencies returned" in result.output


def test_topup_create_json_error_is_json_envelope_on_stderr(monkeypatch):
    class FakeLium:
        def topup_create_invoice(self, amount, crypto_currency, crypto_network):
            raise LiumError("Server error: 503")

    monkeypatch.setattr(topup_module, "Lium", FakeLium)

    result = CliRunner().invoke(
        cli, ["topup", "create", "-a", "1", "-c", "USDT", "-n", "tron", "--json"]
    )

    # Non-zero exit so machine callers detect the failure.
    assert result.exit_code != 0
    # stdout stays clean — no Rich text leaks into the JSON stream.
    assert result.stdout == ""
    # Error is a parseable JSON envelope on stderr.
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "lium_error"
    assert "503" in payload["error"]["message"]


def test_topup_create_human_error_is_unchanged(monkeypatch):
    class FakeLium:
        def topup_create_invoice(self, amount, crypto_currency, crypto_network):
            raise LiumError("Server error: 503")

    monkeypatch.setattr(topup_module, "Lium", FakeLium)

    result = CliRunner().invoke(
        cli, ["topup", "create", "-a", "1", "-c", "USDT", "-n", "tron"]
    )

    # Human path keeps the readable, non-JSON message.
    assert "Error: Server error: 503" in result.output
