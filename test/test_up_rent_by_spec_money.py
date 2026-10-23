"""Money guards of `lium up --gpu` (DAH-3047, lium#328), kept from test_up_rent_by_spec.py by DAH-4027: the rent is
capped at the confirmed $/GPU·h, and an unknown rent outcome never prints a "Retry" hint.
"""

from types import SimpleNamespace

from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.up import command as up_module
from lium.cli.up.actions import RentPodAction
from lium.sdk import ExecutorInfo, LiumError, RentResult


def _executor(huid, price, gpu_count=1):
    return ExecutorInfo(
        id=f"id-{huid}", huid=huid, machine_name="NVIDIA GeForce RTX 4090", gpu_type="RTX4090",
        gpu_count=gpu_count, price_per_hour=price * gpu_count, price_per_gpu=price,
        location={"country": "Germany", "country_code": "DE"}, specs={}, status="available",
        docker_in_docker=False, ip="1.2.3.4", effective_download_speed_mbps=900.0,
    )


CHEAP = _executor("thrifty-node-bb", 0.30)
NEXT = _executor("second-node-cc", 0.30)


class _SpecLium:
    """A client against a backend that advertises rent_by_spec; records every rent."""

    rented_executor = CHEAP

    def __init__(self, *args, **kwargs):
        self.rents = []

    def supports(self, feature):
        return feature == "rent_by_spec"

    def ls(self, **kwargs):
        raise AssertionError("the fleet must not be listed when the backend can pick")

    def get_executor(self, executor_id):
        raise AssertionError("the fleet must not be listed when the backend can pick")

    def rent(self, **kwargs):
        self.rents.append(kwargs)
        if kwargs.get("dry_run"):
            return RentResult(executor=CHEAP, price_per_hour=0.30, template_id="tpl-default", candidates=4,
                              alternatives=[{"id": NEXT.id}], dry_run=True, server_side=True)
        return RentResult(executor=self.rented_executor, price_per_hour=0.30, template_id="tpl-default",
                          pod={"id": "pod-uuid-1", "name": kwargs["name"]}, attempts=1, server_side=True)


def test_rent_action_re_selects_on_the_server_capped_at_the_confirmed_price():
    lium = _SpecLium()
    spec = {"gpu_type": "RTX4090", "gpu_count": 1}

    result = RentPodAction().execute(
        {"lium": lium, "executor": CHEAP, "spec": spec, "template": SimpleNamespace(id="tpl-1"),
         "name": "my-pod", "ports": 5, "enable_volume_encryption": True}
    )

    assert result.ok and result.data["pod_id"] == "pod-uuid-1"
    assert result.data["executor"] is CHEAP and result.data["price_per_hour"] == 0.30
    (rent,) = lium.rents
    assert rent["gpu_type"] == "RTX4090" and rent["gpu_count"] == 1
    assert "min_download_mbps" not in rent
    assert rent["max_price_per_gpu_hour"] == 0.30
    assert rent["template_id"] == "tpl-1" and rent["name"] == "my-pod" and rent["ports"] == 5
    assert "dry_run" not in rent


def _run_up(monkeypatch, lium_cls):
    class _Ready(lium_cls):
        # a server without workspaces: `up` reads it for its workspace line (DAH-3033)
        workspaces = SimpleNamespace(current=lambda: None)

        def get_template(self, template_id):
            return SimpleNamespace(id=template_id, name="pytorch")

        def get_deployment_estimate(self, executor_id, template_id):
            return {}

        def ps(self):
            # gpu_count as a real /pods row has it: the GPU-count check compares it with the rent's
            return [SimpleNamespace(id="pod-uuid-1", huid="thrifty-node-bb", name="thrifty-node-bb", gpu_count=1,
                                    status="RUNNING", ssh_cmd="ssh root@pod.example", ports={"22": 10022})]

        def wait_ready(self, pod, *, timeout=None, poll_interval=None, on_poll=None):
            # the CLI waits through Lium.wait_ready (DAH-2558); the pod here is ready on the first look
            return self.ps()[0]

    monkeypatch.setattr(up_module, "Lium", _Ready)
    monkeypatch.setattr(up_module, "ensure_config", lambda: None)
    return CliRunner().invoke(cli, ["up", "--gpu", "RTX4090", "-y", "--no-ssh"])


def test_up_reports_an_unknown_rent_outcome_without_retry(monkeypatch):
    """lium#328 r4285798281: the resend refused and no pod listed, the first rent may have landed, so
    `lium up` must not fall back to the exit-3 "Retry" hint; it points at 'lium ps'."""
    class _Unknown(_SpecLium):
        def rent(self, **kwargs):
            if kwargs.get("dry_run"):
                return super().rent(**kwargs)
            raise LiumError("The rent got no answer and its resend was refused (403); the pod may be rented",
                            code="rent_outcome_unknown")

    result = _run_up(monkeypatch, _Unknown)

    assert result.exit_code == 3, result.output
    output = " ".join(result.output.split())
    assert "Retry" not in output and "could not be rented" not in output
    assert "may be rented and billing" in output and "'lium ps' shows whether it exists" in output
