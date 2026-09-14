"""The SDK of this checkout, the way the docs' pod-lifecycle example uses it, against the live API: ls → up →
wait_ready → exec (success and a non-zero exit) → upload/download → ps → rm. PERSONA_TESTS' renter-sdk-notebook
journey (S01–S12) as assertions. Runs in-process (the SDK, not the CLI); the same funded account.
"""

from __future__ import annotations

import os
import time
import warnings
from types import SimpleNamespace

import paramiko
import pytest

from conftest import API_KEY, API_URL, MAX_PRICE, MAX_UP_TRIES, MIN_RELIABILITY, keep_pod, refusal_note, reliability_scores, rentable
from lium.sdk.exceptions import (
    LiumAuthError,
    LiumError,
    LiumHostKeyError,
    LiumPermissionError,
    LiumRateLimitError,
    LiumServerError,
)

pytestmark = pytest.mark.timeout(900)

FIRST_SSH_BUDGET_S = 30.0   # how long the first connect to a fresh pod may keep failing before the journey does
FIRST_SSH_PAUSE_S = 5.0

# `Lium.up()` errors that moving to another node cannot fix: a bad key, no balance/permission (LiumInsufficientBalanceError
# is a LiumPermissionError), the API down or throttling, a host-key mismatch. Every other LiumError is the API answering
# the rent with a no (400 "Can't rent node") and is a refusal of that node. So are the two ValueErrors `up()` raises before
# the POST when the node left the listing between `ls` and `up` (`Lium.get_executor`, `Lium.default_docker_template`).
NOT_A_REFUSAL = (LiumAuthError, LiumPermissionError, LiumServerError, LiumRateLimitError, LiumHostKeyError)
NODE_GONE = ("not found", "No node found")   # `Lium.up()`'s two ValueErrors for a node no longer listed (`get_executor`,
                                              # `default_docker_template`); its other ValueErrors (no SSH key, template) are ours


def up_first_accepting(lium, nodes: list, name: str, state: dict, max_tries: int = MAX_UP_TRIES) -> dict:
    """`lium.up()` on the cheapest node; a node that refuses the rent (a `LiumError` outside NOT_A_REFUSAL, or the
    `ValueError` for a node gone from the listing) gives way to the next one, a different executor id each time, at most
    `max_tries` `up()` calls. Returns the dict `up()` returned for the node that took it. Every refusal is a
    `rent refused: …` warning and a line of `state["refused"]`. When a refusal left a pod named `name` behind, that pod
    is recorded in `state["pod"]` (the fixture removes it) and the step fails; when every try refused, the last refusal
    is re-raised as a `LiumError` with every refused node in its message. No clock of its own: an `up()` is the SDK's
    30 s POST plus its `ls` lookups and the `ps` after a refusal, so three tries stay under ~3 min of the module's 900 s
    pytest-timeout next to `wait_ready(600)`; the CLI journey needs the shared budget because each `lium up` waits for
    the pod to be ready."""
    tried: set[str] = set()
    last: Exception | None = None
    state.setdefault("refused", [])
    for node in sorted(nodes, key=lambda n: float(n.price_per_hour)):
        if str(node.id) in tried:
            continue
        if len(tried) >= max_tries:
            break
        tried.add(str(node.id))
        try:
            return lium.up(executor_id=node.id, name=name)
        except NOT_A_REFUSAL:
            raise
        except (LiumError, ValueError) as exc:
            if isinstance(exc, ValueError) and not any(t in str(exc) for t in NODE_GONE):
                raise   # "No SSH keys found", a bad backup argument: ours, and no other node fixes it
            last = exc
            note = refusal_note(str(node.huid), str(node.id), node.price_per_hour, str(exc))
            state["refused"].append(note)
            warnings.warn(note, stacklevel=2)
            left_behind = [p for p in lium.ps() if p.name == name]
            if left_behind:
                state["pod"] = {"id": left_behind[0].id}
                raise AssertionError(f"{node.huid} refused the rent but a pod named {name} exists (id {left_behind[0].id}) "
                                     "— not renting elsewhere; the fixture removes it") from exc
    left = len({str(n.id) for n in nodes} - tried)
    raise LiumError(f"{len(tried)} node(s) refused the rent, none accepted (max_tries={max_tries}"
                    + (f", {left} rentable candidate(s) left untried" if left else "") + "): " + "; ".join(state["refused"])) from last


def first_exec(lium, pod, command: str, budget_s: float = FIRST_SSH_BUDGET_S, pause_s: float = FIRST_SSH_PAUSE_S,
               sleep=time.sleep, clock=time.monotonic) -> dict:
    """`lium.exec` for the FIRST command on a pod, retried while the connection itself fails: new attempts start for
    `budget_s` (each carries the SDK's own 30 s connect timeout, so a port that drops packets can take ~60 s to fail).

    `wait_ready` returns when the platform reports the pod RUNNING with an ssh_cmd — the container's state, not sshd's:
    the port map can be a few seconds behind. Only connection-level errors are retried (paramiko's
    `NoValidConnectionsError`, a refused/reset connection, a timeout, a banner/transport/auth `SSHException` while the
    key lands); a host-key mismatch (`LiumHostKeyError`), a missing key file and any failure inside the command come
    straight back. The last error is re-raised once the budget is spent, so a dead port map still fails — later, and
    with the same exception."""
    deadline = clock() + budget_s
    while True:
        try:
            return lium.exec(pod, command=command)
        except (paramiko.ssh_exception.NoValidConnectionsError, ConnectionError, TimeoutError, paramiko.SSHException):
            if clock() >= deadline:
                raise
            sleep(pause_s)


@pytest.fixture(scope="module")
def lium(session, tmp_path_factory):  # `session` only for the skip when no key is configured
    # the SDK reads ~/.ssh/id_ed25519 for `up` (registers the .pub) and `exec` (the private key): a throwaway HOME
    # with a fresh key, never the runner's
    import subprocess

    home = tmp_path_factory.mktemp("sdk-home")
    (home / ".ssh").mkdir(mode=0o700)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(home / ".ssh" / "id_ed25519")], check=True)
    os.environ["HOME"] = str(home)
    os.environ["LIUM_API_KEY"] = API_KEY
    os.environ["LIUM_BASE_URL"] = API_URL
    from lium.sdk import Lium

    return Lium()


@pytest.fixture(scope="module")
def sdk_pod(lium):
    state = {"pod": None, "name": f"e2e-sdk-{time.strftime('%H%M%S')}"}
    yield state
    if state["pod"] is not None and not keep_pod():   # E2E_KEEP_POD=1 after a failure: the pod stays for a look
        try:
            # state["pod"] is the dict `up` returned or the PodInfo wait_ready returned; down() needs the id either way
            pod_id = state["pod"]["id"] if isinstance(state["pod"], dict) else state["pod"].id
            lium.rm(SimpleNamespace(id=pod_id))
        except Exception:  # noqa: BLE001 — teardown must not mask the test
            pass


def test_sdk_balance_and_ls(lium):
    bal = lium.balance()
    assert isinstance(bal, (int, float)), bal
    nodes = lium.ls()
    assert isinstance(nodes, list)
    if not nodes:
        pytest.skip("the API lists 0 nodes right now — the SDK rent test skips")
    first = nodes[0]
    for attr in ("id", "huid", "gpu_type", "gpu_count", "price_per_hour"):
        assert hasattr(first, attr), f"ExecutorInfo lost .{attr}"


def _country(node) -> str | None:
    loc = node.location or {}
    return loc.get("country") or loc.get("country_code") or loc.get("iso_code")


def test_sdk_up_wait_exec_upload_download_rm(lium, sdk_pod, tmp_path):
    if lium.balance() <= 0.01:
        pytest.skip("the e2e account has no balance")
    # the same country rule as the CLI journey: `ls --format json` falls back to the ISO code when a listing has no
    # country name (lium/cli/ls/display.py), which is why RU/BY are in the default exclusion
    scores = reliability_scores()   # ExecutorInfo has no reliability_score; the public listing does
    nodes = [n for n in lium.ls() if rentable(n.gpu_count, n.price_per_hour, _country(n), n.id, n.huid, scores.get(str(n.id)))]
    if not nodes:
        pytest.skip(f"no rentable node with ≥1 GPU at ≤ ${MAX_PRICE}/h and reliability ≥ {MIN_RELIABILITY:g} listed right now (E2E_EXCLUDE_* applied)")
    t0 = time.monotonic()
    # the cheapest node that accepts: a refusal (400 "Can't rent node") moves to the next one, ≤ MAX_UP_TRIES (DAH-3488)
    created = up_first_accepting(lium, nodes, sdk_pod["name"], sdk_pod)
    assert created.get("id") and created.get("status"), created  # {'executor_id','huid','id','name','ssh_cmd','status'}
    # Recorded before anything can fail, and capped: if wait_ready returns None or raises, an assertion trips, or
    # pytest-timeout kills the process (timeout_method = thread runs no finalizer), the pod still goes — by the
    # fixture, or by the platform at the deadline.
    sdk_pod["pod"] = created
    lium.schedule_termination(
        SimpleNamespace(id=created["id"]),
        termination_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 30 * 60)),
    )
    ready = lium.wait_ready(created["id"], timeout=600)
    # on timeout wait_ready returns None rather than raising: keep the created record so the fixture still removes the pod
    assert ready is not None, f"pod {created['id']} was not RUNNING within 600 s (still recorded; the fixture removes it)"
    sdk_pod["pod"] = ready
    assert ready.status == "RUNNING" and ready.ssh_cmd, ready
    t_ready = time.monotonic() - t0

    # the first connect: RUNNING is the pod's state, sshd's port map may land a few seconds later (DAH-3383)
    ok = first_exec(lium, ready, "echo sdk-e2e && (command -v nvidia-smi >/dev/null && nvidia-smi -L || echo NO-NVIDIA-SMI)")
    assert ok["success"] is True and ok["exit_code"] == 0 and "sdk-e2e" in ok["stdout"], ok
    if "NO-NVIDIA-SMI" not in ok["stdout"] or "localhost" not in API_URL:
        assert "GPU 0" in ok["stdout"], ok
    bad = lium.exec(ready, command="exit 3")
    assert bad["success"] is False and bad["exit_code"] == 3, bad  # a dict, not an exception — the notebook must check it

    src = tmp_path / "sdk-up.txt"
    src.write_text("lium sdk e2e " + str(time.time()))
    lium.upload(ready, local=str(src), remote="/workspace/sdk-up.txt")
    dst = tmp_path / "sdk-dl.txt"
    lium.download(ready, remote="/workspace/sdk-up.txt", local=str(dst))
    assert dst.read_text() == src.read_text()

    listed = [p for p in lium.ps() if p.name == sdk_pod["name"]]
    assert len(listed) == 1, [p.name for p in lium.ps()]

    result = lium.rm(ready)
    sdk_pod["pod"] = None
    assert result.get("success") is True, result
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and any(p.name == sdk_pod["name"] for p in lium.ps()):
        time.sleep(5)
    assert not any(p.name == sdk_pod["name"] for p in lium.ps()), "pod still listed 120s after rm()"
    assert t_ready < 600
