"""DAH-3047: `lium up --gpu X` lets the backend pick and rent when it advertises rent_by_spec.

One dry run names the pick and price before the confirmation; the rent then re-selects on the
server, capped at the confirmed $/GPU·h, so a node taken meanwhile falls through to the next
candidate instead of failing. Without --gpu, or on an older backend, the Pareto pick of
test_up_auto_select.py is unchanged.
"""

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.up import command as up_module
from lium.cli.up.actions import RentPodAction, ResolveExecutorAction
from lium.sdk import ExecutorInfo, LiumAuthError, LiumError, RentResult


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


def test_resolve_dry_runs_the_spec_instead_of_listing():
    lium = _SpecLium()

    result = ResolveExecutorAction().execute({"lium": lium, "gpu": "RTX4090", "count": 2, "country": "de", "ports": 5})

    assert result.ok and result.data["auto_selected"] is True
    assert result.data["executor"] is CHEAP
    assert result.data["candidates"] == 4 and result.data["price_per_hour"] == 0.30
    assert result.data["template_id"] == "tpl-default"
    assert result.data["spec"] == {"gpu_type": "RTX4090", "gpu_count": 2, "country": "de", "min_ports": 5,
                                   "min_download_mbps": 100.0}
    assert lium.rents == [{**result.data["spec"], "template_id": None, "dockerfile_content": None, "dry_run": True}]


def test_no_match_on_the_spec_path_is_a_selection_failure_not_an_api_error():
    """The server's 409 (or the client-side "No node matches") ends like the Pareto path's empty
    list: ActionResult(ok=False) → node_selection_failed, exit 1 — not lium_error, exit 3."""
    lium = _SpecLium()

    def no_match(**kwargs):
        raise LiumError("API error 409: no_executor_matches_spec: gpu_count=8: none of the 3 node(s) satisfies it")

    lium.rent = no_match

    result = ResolveExecutorAction().execute({"lium": lium, "gpu": "RTX4090", "count": 8})

    assert not result.ok
    assert "no_executor_matches_spec" in result.error


def test_the_servers_hint_and_request_id_ride_along_with_a_spec_refusal():
    """A 409 with an error envelope: the hint and the id reach ``data`` (DAH-3057), so the
    node_selection_failed failure prints them like every other API refusal."""
    lium = _SpecLium()

    def no_match(**kwargs):
        raise LiumError("API error 409: no_executor_matches_spec: gpu_count=8", code="no_executor_matches_spec",
                        hint="Lower gpu_count or drop the country filter.", request_id="req-409-0001")

    lium.rent = no_match

    result = ResolveExecutorAction().execute({"lium": lium, "gpu": "RTX4090", "count": 8})

    assert not result.ok
    assert result.data == {"hint": "Lower gpu_count or drop the country filter.",
                           "request_id": "req-409-0001"}


def test_other_api_errors_on_the_spec_path_keep_their_own_code():
    lium = _SpecLium()

    def unauthorised(**kwargs):
        raise LiumAuthError("Invalid API key")

    lium.rent = unauthorised

    with pytest.raises(LiumAuthError):
        ResolveExecutorAction().execute({"lium": lium, "gpu": "RTX4090"})


def test_without_a_gpu_filter_the_client_side_pick_is_kept(monkeypatch):
    class _Listing(_SpecLium):
        def ls(self, **kwargs):
            return [CHEAP]

    monkeypatch.setattr("lium.cli.ls.command.ls_store_executor", lambda **kwargs: [])
    lium = _Listing()

    result = ResolveExecutorAction().execute({"lium": lium, "country": "DE"})

    assert result.ok and result.data["executor"] is CHEAP
    assert "spec" not in result.data and lium.rents == []


def test_an_explicit_node_id_never_rents_by_spec():
    class _ById(_SpecLium):
        def get_executor(self, executor_id):
            return CHEAP

    lium = _ById()

    result = ResolveExecutorAction().execute({"lium": lium, "executor_id": CHEAP.id, "gpu": "RTX4090"})

    assert result.ok and result.data["executor"] is CHEAP and lium.rents == []


def test_rent_action_re_selects_on_the_server_capped_at_the_confirmed_price():
    lium = _SpecLium()
    spec = {"gpu_type": "RTX4090", "gpu_count": 1, "min_download_mbps": 100.0}

    result = RentPodAction().execute(
        {"lium": lium, "executor": CHEAP, "spec": spec, "template": SimpleNamespace(id="tpl-1"),
         "name": "my-pod", "ports": 5, "enable_volume_encryption": True}
    )

    assert result.ok and result.data["pod_id"] == "pod-uuid-1"
    assert result.data["executor"] is CHEAP and result.data["price_per_hour"] == 0.30
    (rent,) = lium.rents
    assert rent["gpu_type"] == "RTX4090" and rent["gpu_count"] == 1 and rent["min_download_mbps"] == 100.0
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


def test_up_names_the_servers_pick_then_rents_by_spec(monkeypatch):
    result = _run_up(monkeypatch, _SpecLium)

    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())  # the console wraps at 80 columns
    assert "Selected thrifty-node-bb (1×RTX4090, Germany) at $0.30/h — cheapest of 4 matching node(s)" in output
    assert "was taken meanwhile" not in output
    assert output.index("Selected") < output.index("ready")


def test_up_prints_the_servers_hint_under_a_spec_refusal(monkeypatch):
    """The 409's hint and request_id reach the terminal (DAH-3057) even though the failure is
    node_selection_failed, not lium_error."""
    class _Refusing(_SpecLium):
        def rent(self, **kwargs):
            raise LiumError("API error 409: no_executor_matches_spec: gpu_count=1", code="no_executor_matches_spec",
                            hint="Drop the country filter.", request_id="req-409-0001")

    result = _run_up(monkeypatch, _Refusing)

    assert result.exit_code == 1, result.output
    output = " ".join(result.output.split())
    assert "no_executor_matches_spec" in output
    assert "Drop the country filter." in output
    assert "request_id: req-409-0001" in output


def test_up_says_when_the_confirmed_node_was_taken_and_another_rented(monkeypatch):
    monkeypatch.setattr(_SpecLium, "rented_executor", NEXT)

    result = _run_up(monkeypatch, _SpecLium)

    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())
    assert "thrifty-node-bb was taken meanwhile; rented second-node-cc (1×RTX4090) at $0.30/h instead" in output


def test_up_names_the_gpus_rented_not_the_nodes_total_on_a_split(monkeypatch):
    """The server may rent one GPU of an 8-GPU node; the price shown is for that one GPU, so
    the count next to it is the rental's, not the node's."""
    big = _executor("eight-node-dd", 0.30, gpu_count=8)

    class _SplitLium(_SpecLium):
        rented_executor = big

        def rent(self, **kwargs):
            self.rents.append(kwargs)
            if kwargs.get("dry_run"):
                return RentResult(executor=big, price_per_hour=0.30, gpu_count=1, template_id="tpl-default",
                                  candidates=2, dry_run=True, server_side=True)
            return RentResult(executor=big, price_per_hour=0.30, gpu_count=1, template_id="tpl-default",
                              pod={"id": "pod-uuid-1", "name": kwargs["name"]}, attempts=1, server_side=True)

    result = _run_up(monkeypatch, _SplitLium)

    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())
    assert "Selected eight-node-dd (1×RTX4090, Germany) at $0.30/h" in output
    assert "8×" not in output
    # the GPU-count check expects the rent's one GPU, not the node's eight (the pod row says 1)
    assert "GPU count mismatch" not in output


def test_help_states_who_picks():
    result = CliRunner().invoke(up_module.up_command, ["--help"])

    assert result.exit_code == 0
    assert "the backend chooses" in result.output
    assert "one GPU unless -c" in result.output
