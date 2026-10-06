"""DAH-2875 follow-up: `lium up --json`, one JSON document on stdout, the lium#217 envelope on failure.

The command an agent needs first (rent) had no machine-readable output: the pod id came back in
Rich prose on stdout and the command then opened SSH. Under `--json` stdout is now exactly one
JSON document (`{"pod": {...}}`, the bare payload `ps`, `describe` and `rm --format json` print),
the narration moves to stderr, and a failure is the `{"ok": false, "error": {...}}` envelope on
stderr with the billing pod in `data`. The teardown half, `lium rm --format json`, shipped in
lium#218 (0.2.0) and is tested in test_rm_final_spend.py.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests
from click.testing import CliRunner

from lium.cli import ui
from lium.cli.actions import ActionResult
from lium.cli.cli import cli
from lium.cli.up import command as up_module
from lium.cli.utils import EXIT_API_ERROR, EXIT_CONFIGURATION_ERROR, EXIT_GENERAL_ERROR
from lium.sdk import LiumError, LiumServerError, PodInfo, PodStartError, VolumeInfo


def _pod(pod_id="pod-1", huid="eager-wolf-aa", name="train") -> PodInfo:
    return PodInfo(
        id=pod_id, name=name, status="RUNNING", huid=huid,
        ssh_cmd="ssh root@203.0.113.10 -p 20022", ports={"22": 20022}, created_at="2026-09-05T10:00:00Z",
        updated_at="2026-09-05T10:00:00Z", executor=None, template={}, removal_scheduled_at=None,
        jupyter_installation_status=None, jupyter_url=None, gpu_count=1,
    )


def _volume(volume_id="vol-1", huid="calm-lake-01", name="my-data") -> VolumeInfo:
    return VolumeInfo(id=volume_id, huid=huid, name=name, description="", created_at="2026-09-05T10:00:00Z")


def _one_document(stdout: str) -> dict:
    """The whole of stdout parses as one JSON object: nothing before it, nothing after it."""
    assert stdout.strip(), "stdout is empty"
    return json.loads(stdout)


# --- lium up --json ---------------------------------------------------------------------------

def _run_up(monkeypatch, args=(), *, wait="ready", rent_error=None, gpus="match", jupyter="ok", rm_error=None,
            volume_takes=0, schedule_error=None, yes=True, input=None):
    """`lium up brave-fox-3a --yes --json <args>` against fakes; returns (result, calls).

    ``wait``: ``"ready"`` | ``"timeout"`` (still starting) | ``"failed"`` (dead pod) | ``"api_error"`` (503 while
    polling) | ``"network_error"`` (``Lium.ps()`` re-raised the transport error after its retries).
    ``gpus``: ``"match"`` | ``"mismatch"`` (nvidia-smi saw fewer) | ``"unchecked"`` (SSH check could not run).
    ``jupyter``: ``"ok"`` (installed, URL returned) | ``"failed"`` | ``"network_error"``; only read with ``--jupyter``.
    ``rm_error``: raised by ``Lium.rm`` (the ``--strict-gpus`` removal); None = the pod is removed.
    ``volume_takes``: seconds the ``--volume new:`` creation advances a fake clock by (``vol-1`` is created either way).
    ``schedule_error``: raised by every ``Lium.schedule_termination`` call (the one at the rent and the retry).
    ``yes=False`` drops ``--yes`` so the price prompt is asked; ``input`` is what the terminal answers.
    """
    calls: list = []
    clock = {"now": 1000.0}
    monkeypatch.setattr(up_module.time, "monotonic", lambda: clock["now"])
    executor = SimpleNamespace(
        id="exec-1", huid="brave-fox-3a", gpu_count=1, gpu_type="A6000",
        price_per_hour=0.24, available_port_count=10, download_speed=1000,
    )

    class _Lium:
        workspaces = SimpleNamespace(current=lambda: None)

        def get_deployment_estimate(self, *a, **k):
            return {}

        def schedule_termination(self, pod, *, termination_time):
            calls.append(("schedule", pod, termination_time))
            if schedule_error is not None:
                raise schedule_error
            return {"removal_scheduled_at": termination_time}

        def rm(self, pod):
            calls.append(("rm", pod.id))
            if rm_error is not None:
                raise rm_error

    class _Resolve:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"executor": executor})

    class _Template:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"template": SimpleNamespace(id="tmpl-1")})

    class _Volume:
        def execute(self, ctx):
            calls.append(("volume", ctx["volume_create_params"]["name"]))
            clock["now"] += volume_takes
            return ActionResult(ok=True, data={"volume": _volume(), "volume_id": "vol-1"})

    class _Rent:
        def execute(self, ctx):
            calls.append(("rent",))
            if rent_error is not None:
                raise rent_error
            return ActionResult(ok=True, data={"pod_info": {"id": "pod-1"}, "pod_id": "pod-1", "pod_name": "train"})

    class _Wait:
        def execute(self, ctx):
            calls.append(("wait", ctx["pod_id"]))
            if wait == "timeout":
                return ActionResult(ok=False, data={}, error="still starting")
            if wait == "api_error":
                raise LiumServerError("API error 503: Service Unavailable")
            if wait == "network_error":
                raise requests.ConnectionError("connection reset")
            if wait == "failed":
                raise PodStartError(
                    "Pod eager-wolf-aa (pod-1) will not start: status FAILED",
                    pod_id="pod-1", pod=_pod(), status="FAILED", history=["PENDING", "FAILED"],
                )
            return ActionResult(ok=True, data={"pod": _pod()})

    class _Verify:
        def execute(self, ctx):
            if gpus == "match":
                return ActionResult(ok=True, data={})
            data = {"expected": 1, "billed": 1, "visible": 0, "executor_id": "exec-1", "mismatch": gpus == "mismatch"}
            return ActionResult(ok=False, data=data, error="GPU count mismatch" if gpus == "mismatch" else "could not check")

    class _Jupyter:
        def execute(self, ctx):
            if jupyter == "ok":
                return ActionResult(ok=True, data={"jupyter_url": "http://203.0.113.10:40001/?token=t", "jupyter_port": 40001})
            if jupyter == "network_error":
                raise requests.ConnectionError("connection reset")
            return ActionResult(ok=False, data={}, error="Jupyter installation failed")

    class _SSH:
        def execute(self, ctx):
            calls.append(("ssh",))
            raise AssertionError("--json must not open an SSH session")

    monkeypatch.setattr(up_module, "ensure_config", lambda: None)
    monkeypatch.setattr(up_module, "Lium", lambda **kwargs: _Lium())
    monkeypatch.setattr(up_module, "ResolveExecutorAction", _Resolve)
    monkeypatch.setattr(up_module, "ResolveTemplateAction", _Template)
    monkeypatch.setattr(up_module, "CreateVolumeAction", _Volume)
    monkeypatch.setattr(up_module, "RentPodAction", _Rent)
    monkeypatch.setattr(up_module, "WaitReadyAction", _Wait)
    monkeypatch.setattr(up_module, "VerifyGpuCountAction", _Verify)
    monkeypatch.setattr(up_module, "InstallJupyterAction", _Jupyter)
    monkeypatch.setattr(up_module, "PrepareSSHAction", _SSH)
    result = CliRunner().invoke(cli, ["up", "brave-fox-3a", *(["--yes"] if yes else []), "--json", *args], input=input)
    return result, calls


def test_up_json_is_one_document_with_the_pod_and_the_narration_on_stderr(monkeypatch):
    """Before: the pod id was in Rich prose on stdout; a parser got `pod train (id: pod-1) created…`."""
    result, _ = _run_up(monkeypatch)

    assert result.exit_code == 0, result.output
    payload = _one_document(result.stdout)
    # The bare payload, as `ps`, `describe` and `rm --format json` print theirs: no `ok` wrapper
    # (exit 0 says it worked; a failure is the envelope on stderr), no key for a time nobody set.
    assert set(payload) == {"pod"}
    assert payload["pod"]["id"] == "pod-1"
    assert payload["pod"]["huid"] == "eager-wolf-aa"
    assert payload["pod"]["name"] == "train"
    assert payload["pod"]["status"] == "RUNNING"
    assert payload["pod"]["ssh_cmd"] == "ssh root@203.0.113.10 -p 20022"
    # The same keys as one row of `lium ps --format json`: one parser serves both.
    assert {"id", "huid", "name", "status", "ports", "ssh_cmd", "ssh_command", "gpu_type"} <= payload["pod"].keys()
    # Moved, not dropped: a person tailing stderr still sees what happened.
    assert "created; waiting for it to become ready" in result.stderr
    assert "created; waiting" not in result.stdout


def test_up_json_ends_after_the_pod_is_ready_without_an_ssh_session(monkeypatch):
    """`--json` without `--no-ssh` used to fall through to the interactive SSH session."""
    result, calls = _run_up(monkeypatch)

    assert result.exit_code == 0, result.output
    assert ("ssh",) not in calls


def test_up_json_carries_the_ttl_as_termination_time(monkeypatch):
    before = datetime.now(timezone.utc)
    result, calls = _run_up(monkeypatch, ["--ttl", "2h"])
    after = datetime.now(timezone.utc)

    assert result.exit_code == 0, result.output
    payload = _one_document(result.stdout)
    scheduled = datetime.fromisoformat(payload["termination_time"])
    assert before + timedelta(hours=2) <= scheduled <= after + timedelta(hours=2)
    # The time the backend was given, not a second reading of the clock.
    assert payload["termination_time"] == next(c[2] for c in calls if c[0] == "schedule")


@pytest.mark.parametrize(
    "outcome, args, code, exit_code",
    [
        (dict(wait="timeout"), [], "pod_not_ready", EXIT_GENERAL_ERROR),
        (dict(wait="failed"), [], "pod_start_failed", EXIT_API_ERROR),
        (dict(gpus="mismatch"), ["--verify-gpus"], "gpu_count_mismatch", EXIT_GENERAL_ERROR),
        (dict(gpus="unchecked"), ["--verify-gpus"], "gpu_verification_failed", EXIT_GENERAL_ERROR),
        (dict(jupyter="failed"), ["--jupyter"], "jupyter_install_failed", EXIT_GENERAL_ERROR),
    ],
)
def test_up_json_failure_after_the_rent_names_the_billing_pod_in_data(monkeypatch, outcome, args, code, exit_code):
    """The pod exists and bills; a program must get its id as a field, not parse it out of the message."""
    result, _ = _run_up(monkeypatch, args, **outcome)

    assert result.exit_code == exit_code
    assert result.stdout == ""
    envelope = json.loads(result.stderr.strip().splitlines()[-1])
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == code
    assert envelope["error"]["exit_code"] == exit_code
    assert envelope["data"]["pod_id"] == "pod-1"
    assert envelope["data"]["pod_name"] == "train"
    # The console redirect is undone on the failure path too, not only on a clean return.
    assert ui.console.stderr is False


@pytest.mark.parametrize(
    "rm_error, pod_removed",
    [
        (None, True),
        (LiumServerError("API error 502"), False),
        # the DELETE is sent once and its answer was lost: `Lium.rm` re-raises the transport error, the pod may
        # still be there, and until this round the envelope was `unexpected_error` with no `data`
        (requests.ConnectionError("connection reset"), False),
    ],
)
def test_up_json_strict_gpus_says_whether_the_mismatched_pod_is_gone(monkeypatch, rm_error, pod_removed):
    """`--strict-gpus` removes the pod; when that removal fails the pod still bills and the envelope must say so."""
    result, calls = _run_up(monkeypatch, ["--verify-gpus", "--strict-gpus"], gpus="mismatch", rm_error=rm_error)

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert ("rm", "pod-1") in calls
    envelope = json.loads(result.stderr.strip().splitlines()[-1])
    assert envelope["error"]["code"] == "gpu_count_mismatch"
    assert envelope["data"]["pod_id"] == "pod-1"
    assert envelope["data"]["pod_removed"] is pod_removed
    assert envelope["data"]["mismatch"] is True


def test_up_json_api_error_during_the_wait_still_names_the_pod(monkeypatch):
    """A 5xx while polling used to reach the handler as a bare `server_error` with no `data`, pod billing."""
    result, _ = _run_up(monkeypatch, wait="api_error")

    assert result.exit_code == EXIT_API_ERROR
    envelope = json.loads(result.stderr.strip().splitlines()[-1])
    assert envelope["error"]["code"] == "server_error"
    assert envelope["data"] == {"pod_id": "pod-1", "pod_name": "train"}


@pytest.mark.parametrize(
    "outcome, args, doing",
    [
        (dict(wait="network_error"), [], "waiting for it to become ready"),
        (dict(schedule_error=requests.ConnectionError("connection reset")), ["--ttl", "2h"], "scheduling its auto-termination"),
        (dict(jupyter="network_error"), ["--jupyter"], "installing Jupyter"),
    ],
)
def test_up_json_network_failure_after_the_rent_names_the_pod_in_data(monkeypatch, outcome, args, doing):
    """`Lium.ps()` and `schedule_termination` re-raise `requests.RequestException` once the SDK's retries run
    out, `install_jupyter` (a POST, sent once) on the first lost connection. The three sites after the rent
    caught only `LiumError`, so a network drop there reached the handler as `unexpected_error` with no `data`:
    the pod bills and the caller has no id to remove (arhangel66's review of lium#252)."""
    result, calls = _run_up(monkeypatch, args, **outcome)

    assert result.exit_code == EXIT_API_ERROR
    assert result.stdout == ""
    envelope = json.loads(result.stderr.strip().splitlines()[-1])
    assert envelope["error"]["code"] == "api_timeout"
    assert envelope["data"] == {"pod_id": "pod-1", "pod_name": "train"}
    assert doing in envelope["error"]["message"]
    assert "ConnectionError" in envelope["error"]["message"]
    if doing != "waiting for it to become ready":
        # The step's own next step, for the text reader too: the ttl or the Jupyter port is still to be set.
        assert "NOT " in envelope["error"]["message"] and "eager-wolf-aa" in envelope["error"]["message"]
    # The generic exit-3 hint is "Retry"; here a retry of `lium up` rents a second pod.
    assert "Do not run 'lium up' again" in envelope["error"]["hint"]
    assert "lium rm train" in envelope["error"]["hint"]
    if "schedule_error" in outcome:
        # The retry once the pod is ready, not the attempt at the rent (which only warns), is what failed.
        assert [c[0] for c in calls].count("schedule") == 2


def test_up_json_with_jupyter_shows_the_url_the_install_produced(monkeypatch):
    """`pod` was fetched before the install; the document must not say `jupyter_url: null` after a success."""
    result, _ = _run_up(monkeypatch, ["--jupyter"])

    assert result.exit_code == 0, result.output
    assert _one_document(result.stdout)["pod"]["jupyter_url"] == "http://203.0.113.10:40001/?token=t"


RENT_FAILURES = [
    (requests.exceptions.ConnectTimeout("connect timed out"), "api_timeout"),
    (LiumError("node is no longer rentable"), "rent_rejected"),
    (LiumServerError("API error 503: Service Unavailable"), "server_error"),
]


@pytest.mark.parametrize("rent_error, code", RENT_FAILURES)
def test_up_json_failure_at_the_rent_without_a_volume_carries_no_data(monkeypatch, rent_error, code):
    """Nothing was created before the rent, so there is nothing to name: `data` is absent, not an empty object."""
    result, _ = _run_up(monkeypatch, rent_error=rent_error)

    assert result.exit_code == EXIT_API_ERROR
    assert result.stdout == ""
    envelope = json.loads(result.stderr.strip().splitlines()[-1])
    assert envelope["error"]["code"] == code
    assert "data" not in envelope
    assert "volume" not in envelope["error"]["message"]


@pytest.mark.parametrize("rent_error, code", RENT_FAILURES)
def test_up_json_failure_at_the_rent_names_the_volume_it_created(monkeypatch, rent_error, code):
    """`--volume new:` creates the volume before the rent and no failure removes it. The envelope used to
    carry no `data` here (lium-platform#355 review), so a program had nothing to retry with or remove."""
    result, calls = _run_up(monkeypatch, ["--volume", "new:name=my-data"], rent_error=rent_error)

    assert result.exit_code == EXIT_API_ERROR
    assert result.stdout == ""
    assert ("volume", "my-data") in calls
    envelope = json.loads(result.stderr.strip().splitlines()[-1])
    assert envelope["error"]["code"] == code
    assert envelope["error"]["message"].endswith(" The volume my-data was created and is kept.")
    assert envelope["data"] == {"volume_id": "vol-1", "volume_huid": "calm-lake-01"}


def test_up_json_refusal_at_the_rent_keeps_the_servers_request_id_next_to_the_volume(monkeypatch):
    """After lium#190 the API's code, hint and request_id ride on the error; the volume the rent leaves
    behind must not push them out of the envelope (nor the other way round)."""
    refusal = LiumError("insufficient balance", code="insufficient_balance", hint="Run 'lium fund'.",
                        request_id="req-abc")
    result, _ = _run_up(monkeypatch, ["--volume", "new:name=my-data"], rent_error=refusal)

    assert result.exit_code == EXIT_API_ERROR
    envelope = json.loads(result.stderr.strip().splitlines()[-1])
    assert envelope["error"]["code"] == "insufficient_balance"
    assert envelope["error"]["hint"] == "Run 'lium fund'."
    assert envelope["error"]["message"].endswith(" The volume my-data was created and is kept.")
    assert envelope["data"] == {"volume_id": "vol-1", "volume_huid": "calm-lake-01", "request_id": "req-abc"}


def test_up_json_ttl_retry_refused_by_the_api_still_says_the_ttl_is_not_set(monkeypatch):
    """A 5xx at the `--ttl` retry took the `LiumError` branch of `_post_rent_failure`, which dropped the
    note: the text reader was told to 'Retry' and not that auto-termination is still to be scheduled."""
    result, _ = _run_up(monkeypatch, ["--ttl", "2h"], schedule_error=LiumServerError("API error 502"))

    assert result.exit_code == EXIT_API_ERROR
    envelope = json.loads(result.stderr.strip().splitlines()[-1])
    assert envelope["error"]["code"] == "server_error"
    assert envelope["data"] == {"pod_id": "pod-1", "pod_name": "train"}
    assert "Auto-termination is NOT scheduled" in envelope["error"]["message"]


def test_up_json_timeout_before_rent_names_the_volume_it_created(monkeypatch):
    """The volume request ate the whole `--timeout`; the volume exists and is kept, and `error.message` was the
    only place that said so (by name, not id)."""
    result, calls = _run_up(monkeypatch, ["--timeout", "120", "--volume", "new:name=my-data"], volume_takes=121)

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert result.stdout == ""
    assert ("rent",) not in calls
    envelope = json.loads(result.stderr.strip().splitlines()[-1])
    assert envelope["error"]["code"] == "timeout_before_rent"
    assert "The volume my-data was created and is kept." in envelope["error"]["message"]
    assert envelope["data"] == {"volume_id": "vol-1", "volume_huid": "calm-lake-01"}


def test_up_json_without_yes_behind_a_pipe_is_the_confirmation_envelope(monkeypatch):
    """A `--json` caller behind a pipe cannot answer the price prompt; it must get the envelope, not a hang."""
    monkeypatch.setattr(ui, "is_interactive", lambda: False)
    calls: list = []

    class _Resolve:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"executor": SimpleNamespace(
                id="exec-1", huid="brave-fox-3a", gpu_count=1, gpu_type="A6000", price_per_hour=0.24,
                available_port_count=10, download_speed=1000, location=None,
            )})

    class _Template:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"template": SimpleNamespace(id="tmpl-1")})

    class _Rent:
        def execute(self, ctx):
            calls.append("rent")

    monkeypatch.setattr(up_module, "ensure_config", lambda: None)
    monkeypatch.setattr(up_module, "Lium", lambda **kwargs: SimpleNamespace(
        workspaces=SimpleNamespace(current=lambda: None), get_deployment_estimate=lambda *a, **k: {},
    ))
    monkeypatch.setattr(up_module, "ResolveExecutorAction", _Resolve)
    monkeypatch.setattr(up_module, "ResolveTemplateAction", _Template)
    monkeypatch.setattr(up_module, "RentPodAction", _Rent)

    result = CliRunner().invoke(cli, ["up", "brave-fox-3a", "--json"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert result.stdout == ""
    assert json.loads(result.stderr.strip().splitlines()[-1])["error"]["code"] == "confirmation_required"
    assert calls == [], "nothing is rented on the strength of a prompt nobody answered"


def test_up_json_confirmation_prompt_goes_to_stderr(monkeypatch):
    """`Confirm.ask` on Rich's global console wrote the price question into stdout, ahead of the JSON."""
    monkeypatch.setattr(ui, "is_interactive", lambda: True)  # a terminal that answers "y"
    result, calls = _run_up(monkeypatch, yes=False, input="y\n")

    assert result.exit_code == 0, result.output
    assert _one_document(result.stdout)["pod"]["huid"] == "eager-wolf-aa"
    assert ("rent",) in calls
    assert "Acquire pod on brave-fox-3a" in result.stderr
    assert "Acquire pod" not in result.stdout


def test_json_narration_redirect_is_undone_after_the_command(monkeypatch):
    """The console is a process-wide object; a `--json` run must not leave the next command's text on stderr."""
    assert ui.console.stderr is False
    _run_up(monkeypatch)
    assert ui.console.stderr is False
    _run_up(monkeypatch, wait="timeout")  # the failure path leaves through `finally` too
    assert ui.console.stderr is False
