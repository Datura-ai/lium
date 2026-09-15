"""The API's error code, hint and request_id reach the caller (DAH-3057): on the SDK exception,
under the CLI's error line, and in the --json envelope. Absent on an older server → nothing added."""

import json
from types import SimpleNamespace

import click
import pytest

from lium.cli.utils import EXIT_API_ERROR, CliFailure, _api_error_data, handle_errors
from lium.sdk import Config, Lium, LiumError, LiumNotFoundError, LiumPermissionError

ENVELOPE = {
    "success": False,
    "error": {
        "code": "insufficient_balance",
        "message": "Insufficient balance",
        "hint": "Top up at https://lium.io/billing; renting needs a positive balance covering 15 minutes of the node.",
        "request_id": "4f1c9d2e8a7b4c3d9e0f1a2b3c4d5e6f",
    },
    "message": "Insufficient balance",
    "status_code": 403,
}


class _Response:
    ok = False
    text = ""

    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _client(monkeypatch, response):
    monkeypatch.setattr("lium.sdk.client.requests.request", lambda *a, **kw: response)
    return Lium(Config(api_key="test"))


def test_envelope_fields_land_on_the_exception(monkeypatch):
    client = _client(monkeypatch, _Response(403, ENVELOPE))

    with pytest.raises(LiumPermissionError) as raised:
        client._request("POST", "/executors/x/rent")

    e = raised.value
    # the text callers match on is unchanged; main (#217) appends which key the server refused
    assert str(e).startswith("Permission denied: Insufficient balance")
    assert str(e).endswith("from explicit)")  # the key's fingerprint and source (main, #217)
    assert (e.code, e.hint, e.request_id) == (
        "insufficient_balance",
        ENVELOPE["error"]["hint"],
        "4f1c9d2e8a7b4c3d9e0f1a2b3c4d5e6f",
    )


def test_request_id_falls_back_to_the_header_when_the_body_has_none(monkeypatch):
    client = _client(monkeypatch, _Response(500, None, {"X-Request-Id": "hdr-0123456789"}))

    with pytest.raises(LiumError) as raised:
        client._request("GET", "/pods", retry=False)  # one send: a retried 5xx would sleep between attempts

    assert (raised.value.code, raised.value.hint, raised.value.request_id) == (None, None, "hdr-0123456789")


def test_non_string_envelope_fields_are_dropped(monkeypatch):
    """A proxy or a future server sending a number or an object where text is expected
    must not reach the printing code; the header id still counts."""
    body = {"error": {"code": 400, "hint": {"docs": "https://lium.io"}, "request_id": 7}}
    client = _client(monkeypatch, _Response(400, body, {"X-Request-Id": "hdr-0123456789"}))

    with pytest.raises(LiumError) as raised:
        client._request("POST", "/executors/x/rent")

    assert (raised.value.code, raised.value.hint, raised.value.request_id) == (None, None, "hdr-0123456789")


def test_logs_of_a_missing_pod_keeps_the_server_context(monkeypatch):
    """``logs()`` renames the 404 for the caller; the code, hint and id must survive the rename."""
    envelope = {"error": {"code": "pod_not_found", "message": "Pod not found",
                          "hint": "Run 'lium ps' for the pods you have.", "request_id": "req-404-0001"}}
    client = _client(monkeypatch, _Response(404, envelope))

    with pytest.raises(LiumNotFoundError) as raised:
        list(client.logs("pod-x"))

    e = raised.value
    assert str(e) == "Pod not found: pod-x"
    assert (e.code, e.hint, e.request_id) == ("pod_not_found", envelope["error"]["hint"], "req-404-0001")


def test_an_old_server_body_leaves_the_fields_empty(monkeypatch):
    client = _client(monkeypatch, _Response(400, {"success": False, "error": "HTTP error", "message": "Node is not available."}))

    with pytest.raises(LiumError) as raised:
        client._request("POST", "/executors/x/rent")

    assert str(raised.value) == "API error 400: Node is not available."
    assert (raised.value.code, raised.value.hint, raised.value.request_id) == (None, None, None)


def _failing_command(exc):
    @click.command()
    @click.option("--json", "json_output", is_flag=True)
    @handle_errors
    def cmd(json_output):
        raise exc

    return cmd


def test_cli_prints_the_hint_and_the_request_id_under_the_error(capsys):
    exc = LiumError("API error 400: Node is not available.", code="node_unavailable",
                    hint="Pick another node (lium ls) or retry in a minute.", request_id="abc123def456")

    with pytest.raises(SystemExit) as exit_info:
        _failing_command(exc).main([], standalone_mode=False)

    out = capsys.readouterr()
    text = out.out + out.err
    assert exit_info.value.code != 0
    assert "Node is not available." in text
    assert "Pick another node" in text
    assert "request_id: abc123def456" in text


def test_cli_json_envelope_carries_the_server_code_hint_and_request_id(capsys):
    exc = LiumError("API error 400: Node is not available.", code="node_unavailable", hint="Pick another node.",
                    request_id="abc123def456")

    with pytest.raises(SystemExit):
        _failing_command(exc).main(["--json"], standalone_mode=False)

    envelope = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert envelope["error"]["code"] == "node_unavailable"
    assert envelope["error"]["hint"] == "Pick another node."  # the server's hint, not the default for the code
    assert envelope["data"] == {"request_id": "abc123def456"}


def test_cli_without_server_context_prints_only_the_error(capsys):
    with pytest.raises(SystemExit):
        _failing_command(LiumError("API error 400: Node is not available.")).main([], standalone_mode=False)

    out = capsys.readouterr()
    assert "request_id" not in out.out + out.err


def test_a_request_id_with_rich_markup_is_printed_not_parsed(capsys):
    """The id comes from the server or a proxy; ``[/]`` in it must not raise inside the error handler."""
    exc = LiumError("API error 500: Server error", hint="Retry [soon].", request_id="abc[/]def")

    with pytest.raises(SystemExit) as exit_info:
        _failing_command(exc).main([], standalone_mode=False)

    out = capsys.readouterr()
    text = out.out + out.err
    assert exit_info.value.code != 0
    assert "Retry [soon]." in text
    assert "request_id: abc[/]def" in text


def test_cli_failure_carrying_api_context_prints_and_exports_it(capsys):
    """A command that wraps an API refusal in a CliFailure hands the id over in ``data`` and the
    server's hint as its own (it replaces the default hint for the code)."""
    api = LiumError("API error 400: Node is not available.", code="node_unavailable",
                    hint="Pick another node (lium ls).", request_id="abc123def456")
    failure = CliFailure(api.code, f"Node x could not be rented: {api}.", EXIT_API_ERROR,
                         data=_api_error_data(api), hint=api.hint)

    with pytest.raises(SystemExit):
        _failing_command(failure).main([], standalone_mode=False)
    out = capsys.readouterr()
    assert "Pick another node (lium ls)." in out.out + out.err
    assert "request_id: abc123def456" in out.out + out.err

    with pytest.raises(SystemExit):
        _failing_command(failure).main(["--json"], standalone_mode=False)
    envelope = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert envelope["error"]["code"] == "node_unavailable"
    assert envelope["error"]["hint"] == "Pick another node (lium ls)."
    assert envelope["data"] == {"request_id": "abc123def456"}


def test_up_on_a_refused_rent_prints_the_hint_and_the_request_id(monkeypatch):
    """The headline case: `lium up <taken node>` — the 400 goes through the rent step's
    CliFailure, and the server's hint and request_id must still reach the screen."""
    from click.testing import CliRunner

    from lium.cli.cli import cli
    from lium.cli.up import command as up_module

    class _RefusingLium:
        # a server without workspaces: `up` reads it for its workspace line (DAH-3033)
        workspaces = SimpleNamespace(current=lambda: None)

        def __init__(self, *args, **kwargs):
            pass

        def get_executor(self, executor_id):
            return SimpleNamespace(
                id="id-brave-orbit-b9", huid="brave-orbit-b9", gpu_type="RTX4090", gpu_count=1,
                price_per_hour=1.0, price_per_gpu=1.0, location={"country": "Ukraine", "country_code": "UA"},
                download_speed=100, upload_speed=100,
                specs={"network": {"download_speed": 100, "upload_speed": 100}},
                docker_in_docker=False, max_cuda_version=12.4, tier="secure",
            )

        def default_docker_template(self, executor_id):
            return SimpleNamespace(id="tpl-1", name="pytorch")

        def get_deployment_estimate(self, executor_id, template_id):
            return {}

        def ps(self):
            return []

        def up(self, **kwargs):
            raise LiumError("API error 400: Node is not available.", code="node_unavailable",
                            hint="Pick another node (lium ls) or retry in a minute.", request_id="abc123def456")

    monkeypatch.setattr(up_module, "Lium", _RefusingLium)
    monkeypatch.setattr(up_module, "ensure_config", lambda: None)

    result = CliRunner().invoke(cli, ["up", "some-node-id", "-y", "--no-ssh"])

    assert result.exit_code == EXIT_API_ERROR
    assert "brave-orbit-b9 could not be rented" in result.output
    assert "Pick another node (lium ls) or retry in a minute." in result.output
    assert "request_id: abc123def456" in result.output
