"""Small CLI fixes from the 7 Oct 2026 sweep: `up` flags, `ls` empty results, `reboot` output."""

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.ls import command as ls_module
from lium.cli.reboot import command as reboot_module
from lium.cli.up import command as up_module
from lium.cli.up import validation as up_validation
from lium.cli.up.actions import CreateEphemeralTemplateAction
from lium.cli.utils import EXIT_CONFIGURATION_ERROR, EXIT_GENERAL_ERROR


@pytest.mark.parametrize(("ports", "accepted"), [(5, True), (0, False), (-1, False)])
def test_up_takes_ports_as_the_only_filter_when_at_least_one(ports, accepted):
    valid, error = up_validation.validate(None, None, None, None, None, None, ports=ports)

    assert valid is accepted, error


def test_up_accepts_an_image_digest():
    digest = "sha256:" + "a" * 64

    valid, error = up_validation.validate(
        None, "A4000", None, None, None, None, image=f"registry:5000/team/img@{digest}"
    )

    assert valid, error


@pytest.mark.parametrize("flags", [["-e", "HF_TOKEN=x"], ["--cmd", "python serve.py"], ["--entrypoint", "/bin/sh"],
                                   ["--internal-ports", "8000"]])
def test_up_refuses_image_only_flags_without_image(monkeypatch, flags):
    monkeypatch.setattr(up_module, "ensure_config", lambda: None)

    def _no_lium(*a, **k):
        raise AssertionError("refused flags must stop the command before any request")

    monkeypatch.setattr(up_module, "Lium", _no_lium)

    result = CliRunner().invoke(cli, ["up", "brave-fox-3a", "-y", *flags])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR, result.output
    assert "apply only with --image" in result.output


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("pytorch/pytorch:2.0", ("pytorch/pytorch", "2.0")),
        ("python", ("python", "latest")),
        ("localhost:5000/team/img", ("localhost:5000/team/img", "latest")),
        ("localhost:5000/team/img:1.2", ("localhost:5000/team/img", "1.2")),
        ("registry:5000/team/img@sha256:" + "a" * 64, ("registry:5000/team/img@sha256:" + "a" * 64, "")),
    ],
)
def test_ephemeral_template_splits_the_tag_not_a_registry_port(image, expected):
    seen = {}

    class _Lium:
        def create_template(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(id="t-1")

    CreateEphemeralTemplateAction().execute({"lium": _Lium(), "image": image})

    assert (seen["docker_image"], seen["docker_image_tag"]) == expected


@pytest.fixture
def empty_ls(monkeypatch):
    class _Lium:
        workspaces = SimpleNamespace(current=lambda: None)

        def __init__(self, *a, **k):
            pass

        def ls(self, **kwargs):
            return []

        def unknown_gpu_type(self, gpu_type):
            return None

    monkeypatch.setattr(ls_module, "Lium", _Lium)
    monkeypatch.setattr(ls_module, "store_executor_selection", lambda executors: None)


@pytest.mark.parametrize("flags", [["--count", "3"], ["--min-cuda", "99"], ["--min-cpus", "100000"]])
def test_ls_with_a_server_filter_that_matches_nothing_does_not_say_rented_out(empty_ls, flags):
    result = CliRunner().invoke(cli, ["ls", *flags])

    assert result.exit_code == 0, result.output
    assert "No available node matches" in result.output
    assert "rented out" not in result.output


@pytest.mark.parametrize("count", ["0", "-1"])
def test_ls_refuses_a_count_below_one(empty_ls, count):
    result = CliRunner().invoke(cli, ["ls", "--count", count])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert "--count must be at least 1" in result.output


def _pod(huid):
    return SimpleNamespace(id=f"id-{huid}", name=huid, huid=huid, status="RUNNING")


def _volume_not_found(pod):
    raise RuntimeError("API error 400: volume not found")


def _run_reboot_all(monkeypatch, outcomes):
    class _Lium:
        def __init__(self, *a, **k):
            pass

        def ps(self):
            return [_pod(huid) for huid in outcomes]

        def reboot(self, pod, volume_id=None):
            return outcomes[pod.huid](pod)

    monkeypatch.setattr(reboot_module, "Lium", _Lium)
    return CliRunner().invoke(cli, ["reboot", "--all"])


@pytest.mark.parametrize(
    ("outcomes", "exit_code", "lines"),
    [
        ({"pod-a": lambda pod: {}, "pod-b": lambda pod: {}}, 0, ["Reboot requested for pod-a, pod-b"]),
        (
            {"pod-a": lambda pod: {}, "pod-b": _volume_not_found},
            EXIT_GENERAL_ERROR,
            ["Reboot requested for pod-a", "pod-b (API error 400: volume not found)"],
        ),
        ({"pod-a": _volume_not_found}, EXIT_GENERAL_ERROR, ["pod-a (API error 400: volume not found)"]),
    ],
    ids=["all-success", "mixed", "all-failure"],
)
def test_reboot_names_what_was_rebooted_and_why_each_failure_failed(monkeypatch, outcomes, exit_code, lines):
    result = _run_reboot_all(monkeypatch, outcomes)

    output = " ".join(result.output.split())  # the error box wraps long lines
    assert result.exit_code == exit_code, result.output
    for line in lines:
        assert line in output
