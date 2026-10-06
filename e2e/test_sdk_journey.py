"""The SDK of this checkout, the way the docs' pod-lifecycle example uses it, against the live API: ls → up →
wait_ready → exec (success and a non-zero exit) → upload/download → ps → rm. PERSONA_TESTS' renter-sdk-notebook
journey (S01–S12) as assertions. Runs in-process (the SDK, not the CLI); the same funded account.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import paramiko
import pytest

from conftest import API_KEY, API_URL, MAX_PRICE, MIN_RELIABILITY, keep_pod, reliability_scores, rentable

pytestmark = pytest.mark.timeout(900)

FIRST_SSH_BUDGET_S = 30.0   # how long the first connect to a fresh pod may keep failing before the journey does
FIRST_SSH_PAUSE_S = 5.0


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
    node = min(nodes, key=lambda n: float(n.price_per_hour))
    t0 = time.monotonic()
    created = lium.up(executor_id=node.id, name=sdk_pod["name"])
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
