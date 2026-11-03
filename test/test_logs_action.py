from types import SimpleNamespace

import pytest

from lium.cli.logs.actions import StreamLogsAction
from lium.cli.utils import CliFailure, EXIT_API_ERROR


def test_an_error_frame_fails_the_command_with_the_server_reason():
    frames = ['data: {"log": "hello"}', b'data: {"error": "Node is not reachable right now, try again shortly"}']
    ctx = {"lium": SimpleNamespace(logs=lambda *a, **k: iter(frames)), "pod": SimpleNamespace(id="p"), "tail": 10,
           "follow": False}
    lines = StreamLogsAction().execute(ctx)
    assert next(lines) == "hello"
    with pytest.raises(CliFailure) as failed:
        next(lines)
    assert failed.value.exit_code == EXIT_API_ERROR
    assert failed.value.message == "Node is not reachable right now, try again shortly"


def test_lium_up_image_names_the_billing_pod_when_the_stream_errors(monkeypatch):
    """`lium up --image` streams logs after the rent: the failure names the pod and does not say Retry."""
    from click.testing import CliRunner

    from lium.cli.actions import ActionResult
    from lium.cli.up import command as up_command

    class _Lium:
        workspaces = SimpleNamespace(current=lambda: None)

        def get_deployment_estimate(self, *a, **k):
            return {}

        def logs(self, *a, **k):
            return iter([b'data: {"error": "Node is not reachable right now"}'])

    def ok(data):
        return lambda: SimpleNamespace(execute=lambda ctx: ActionResult(ok=True, data=data))

    executor = SimpleNamespace(id="exec-1", huid="brave-fox-3a", gpu_count=1, gpu_type="A6000",
                               price_per_hour=0.24, available_port_count=10, download_speed=1000)
    pod = SimpleNamespace(id="pod-1", name="train", huid="eager-wolf-aa", status="RUNNING", ssh_cmd=None,
                          ports={}, jupyter_url=None)
    monkeypatch.setattr(up_command, "ensure_config", lambda: None)
    monkeypatch.setattr(up_command, "Lium", lambda **kwargs: _Lium())
    monkeypatch.setattr(up_command, "ResolveExecutorAction", ok({"executor": executor}))
    monkeypatch.setattr(up_command, "CreateEphemeralTemplateAction", ok({"template": SimpleNamespace(id="t")}))
    monkeypatch.setattr(up_command, "RentPodAction", ok({"pod_info": {}, "pod_id": "pod-1", "pod_name": "train"}))
    monkeypatch.setattr(up_command, "WaitReadyAction", ok({"pod": pod}))

    result = CliRunner().invoke(up_command.up_command, ["brave-fox-3a", "--yes", "--image", "nginx"])
    out = " ".join(result.output.split())
    assert result.exit_code == EXIT_API_ERROR, result.output
    assert "Pod train (id: pod-1) is rented and billing" in out
    assert "Node is not reachable right now" in out
    assert "Do not run 'lium up' again" in out
