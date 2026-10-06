"""The renter's first hour with this checkout's CLI against a live API: balance → ls → up → RUNNING → exec (exit
codes) → scp up/down → billing → rm → gone → final charge. Every step is what PERSONA_TESTS' renter journey did by
hand on staging on 7 Sep 2026, now asserted. Ordered: each test builds on the previous one's state in `rental`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time

import pytest

from conftest import API_URL, MAX_PRICE, MIN_RELIABILITY, Rental, Session, keep_pod, ps, reliability_scores, rentable

pytestmark = pytest.mark.timeout(600)


def _balance(session: Session) -> float:
    r = session.lium("balance", "--json", check=True)
    body = r.json()
    assert isinstance(body.get("balance_usd"), (int, float)), body
    return float(body["balance_usd"])


# ---------------------------------------------------------------- read-only surface -------------------------------


def test_balance_json_is_a_number(session: Session, rental: Rental):
    rental.balance_before = _balance(session)
    assert rental.balance_before >= 0


def test_ls_json_lists_nodes_with_stable_fields(session: Session, rental: Rental):
    r = session.lium("ls", "--format", "json", check=True)
    nodes = r.json()
    assert isinstance(nodes, list), r
    if not nodes:
        pytest.skip("the API lists 0 nodes right now (staging's single node rented or offline) — the rent tests skip; read-only and error-contract tests still ran")
    required = {"id", "huid", "gpu_type", "gpu_count", "price_per_hour"}
    missing = required - set(nodes[0])
    assert not missing, f"ls --format json lost fields agents read: {missing}"
    # a second call keeps the same key set — the contract agents and the docs rely on
    again = session.lium("ls", "--format", "json", check=True).json()
    assert set(again[0]) == set(nodes[0]), "ls --format json key set changed between two calls"
    scores = reliability_scores()   # `ls --format json` has no reliability_score; the public listing does
    candidates = [n for n in nodes if rentable(n.get("gpu_count"), n.get("price_per_hour"), n.get("country"), n.get("id"), n.get("huid"), scores.get(str(n.get("id"))))]
    if not candidates:
        pytest.skip(f"no rentable node with ≥1 GPU at ≤ ${MAX_PRICE}/h and reliability ≥ {MIN_RELIABILITY:g} listed right now (E2E_EXCLUDE_* applied) — nothing to rent")
    cheapest = min(candidates, key=lambda n: float(n["price_per_hour"]))
    rental.executor_id, rental.price_per_hour, rental.gpu_type = cheapest["id"], float(cheapest["price_per_hour"]), str(cheapest.get("gpu_type"))


def test_ls_gpu_filter_narrows(session: Session, rental: Rental):
    if not rental.gpu_type:
        pytest.skip("no candidate node")
    r = session.lium("ls", "--gpu", rental.gpu_type, "--format", "json", check=True)
    nodes = r.json()
    assert nodes and all(rental.gpu_type.lower() in str(n.get("gpu_type", "")).lower() for n in nodes), nodes[:2]


def test_ps_with_no_pods_is_a_json_array(session: Session):
    r = session.lium("ps", "--format", "json", check=True)
    assert isinstance(r.json(), list)


def test_ls_is_fast_enough_for_an_agent_loop(session: Session):
    """§29 budget: `lium ls` warm under 1 s… measured, not enforced yet — recorded so the trend is visible."""
    r = session.lium("ls", "--format", "json", check=True)
    assert r.seconds < 15, f"lium ls took {r.seconds:.1f}s — far past any interactive budget"


# ---------------------------------------------------------------- error contract ----------------------------------


def test_wrong_key_is_exit_3_with_a_json_error(session: Session):
    r = session.lium("balance", "--json", key="lium_e2e_not_a_real_key_0000000000")
    assert r.rc == 3, r
    text = (r.out.strip() or r.err.strip())  # the JSON error goes to stderr today (persona J02); either stream is the contract
    body = json.loads(text) if text.startswith("{") else None
    assert body and body.get("ok") is False and "error" in body, r


def test_no_key_is_exit_2_and_says_what_to_do(session: Session):
    r = session.lium("balance", key=None)
    assert r.rc == 2, r
    text = (r.out + r.err).lower()
    assert "api key" in text and ("lium init" in text or "lium_api_key" in text or "signup" in text), r


def test_unknown_pod_is_exit_5(session: Session):
    assert session.lium("exec", "no-such-pod-e2e", "--", "true").rc == 5
    assert session.lium("describe", "no-such-pod-e2e", "--json").rc == 5
    assert session.lium("rm", "no-such-pod-e2e", "-y").rc == 5


# ---------------------------------------------------------------- the rental --------------------------------------


def test_up_rents_exactly_one_pod(session: Session, rental: Rental):
    if not rental.executor_id:
        pytest.skip("no candidate node")
    if (rental.balance_before or 0) <= 0.01:
        pytest.skip("the e2e account has no balance — fund it before the rent tests can run")
    rental.up_called_at = time.monotonic()
    # `lium up` schedules --ttl right after the rent (DAH-3331) and then blocks until the pod is RUNNING with an
    # ssh_cmd, so this call's timeout is the suite's RUNNING budget: 540 s, under the module's 600 s
    # pytest-timeout (timeout_method = thread exits the process with no finalizer). A killed `up` may still leave
    # a pod without a TTL (killed between the two calls); the rental fixture removes it by name.
    try:
        r = session.lium("up", rental.executor_id, "--name", rental.name, "--ttl", "30m", "-y", "--no-ssh", timeout=540)
    except subprocess.TimeoutExpired:
        pytest.fail(f"lium up {rental.name} did not return within 540 s (the fixture removes the rented pod by name)")
    assert r.rc == 0, r
    mine = [p for p in ps(session) if p.get("name") == rental.name]
    assert len(mine) == 1, f"pods named {rental.name}: {len(mine)} (a retried POST must never rent twice)"
    rental.pod = mine[0]
    for key in ("id", "huid", "status"):
        assert key in rental.pod, rental.pod


def test_pod_reaches_running_with_ssh(session: Session, rental: Rental):
    if not rental.pod:
        pytest.skip("no pod")
    # `up` has already waited for RUNNING + ssh_cmd (see above), so this is the `ps` consistency check: the listing
    # must show what `up` saw; 60 s covers a lagging listing, not the boot.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        mine = [p for p in ps(session) if p.get("name") == rental.name]
        assert mine, "the pod vanished while pending"
        rental.pod = mine[0]
        if rental.pod.get("status") == "RUNNING" and rental.pod.get("ssh_cmd"):
            rental.running_at = time.monotonic()
            break
        assert rental.pod.get("status") not in ("FAILED", "STOPPED"), rental.pod
        time.sleep(5)
    assert rental.running_at, f"`up` returned 0 but ps does not list {rental.name} as RUNNING with an ssh_cmd after 60s: {rental.pod}"
    assert re.match(r"^ssh\s+\S+@\S+\s+-p\s+\d+", rental.pod["ssh_cmd"]), rental.pod["ssh_cmd"]


def test_describe_json_has_the_sections_the_docs_promise(session: Session, rental: Rental):
    if not rental.pod:
        pytest.skip("no pod")
    r = session.lium("describe", rental.name, "--json", check=True)
    body = r.json()
    assert {"pod", "machine", "gpu", "access", "billing"} <= set(body), sorted(body)


def test_exec_propagates_exit_codes_and_streams(session: Session, rental: Rental):
    if not rental.running_at:
        pytest.skip("pod not running")
    r = session.lium("exec", rental.name, "--json", "--", "echo out; echo err 1>&2; exit 7", timeout=240)
    assert r.rc == 7, r
    body = r.json()  # {"ok": false, "results": [{"pod": …, "exit_code": 7, "stdout": …, "stderr": …, "error": null}]}
    assert body.get("ok") is False and body.get("results"), body
    one = body["results"][0]
    assert one.get("exit_code") == 7 and "out" in (one.get("stdout") or "") and "err" in (one.get("stderr") or ""), one
    r = session.lium("exec", rental.name, "--", "echo -n hello-e2e", timeout=240)
    assert r.rc == 0 and "hello-e2e" in r.out, r


def test_exec_sees_the_gpu_paid_for(session: Session, rental: Rental):
    if not rental.running_at:
        pytest.skip("pod not running")
    r = session.lium("exec", rental.name, "--", "command -v nvidia-smi >/dev/null && nvidia-smi -L || echo NO-NVIDIA-SMI", timeout=240)
    assert r.rc == 0, r
    if "NO-NVIDIA-SMI" in r.out:
        if "localhost" in API_URL or "127.0.0.1" in API_URL:
            pytest.skip("stub executor (compose stack): no GPU to see")
        pytest.fail(f"nvidia-smi missing on a rented pod: {r}")
    gpus = [line for line in r.out.splitlines() if line.startswith("GPU ")]
    assert len(gpus) >= 1, r.out
    billed = int(rental.pod.get("gpu_count") or 1)
    assert len(gpus) >= billed, f"billed {billed} GPU(s), the pod exposes {len(gpus)}"


def test_scp_round_trip_is_byte_exact(session: Session, rental: Rental, tmp_path):
    if not rental.running_at:
        pytest.skip("pod not running")
    payload = os.urandom(256 * 1024)
    src = tmp_path / "up.bin"
    src.write_bytes(payload)
    assert session.lium("exec", rental.name, "--", "mkdir -p /workspace", timeout=240).rc == 0
    up = session.lium("scp", rental.name, str(src), "/workspace/e2e-up.bin", timeout=300)
    assert up.rc == 0, up
    r = session.lium("exec", rental.name, "--", "cp /workspace/e2e-up.bin /workspace/e2e-dl.bin && sha256sum /workspace/e2e-dl.bin", timeout=240)
    assert r.rc == 0 and hashlib.sha256(payload).hexdigest() in r.out, r
    dst = tmp_path / "dl.bin"
    dl = session.lium("scp", rental.name, "/workspace/e2e-dl.bin", str(dst), "-d", timeout=300)
    assert dl.rc == 0, dl
    assert dst.read_bytes() == payload, "downloaded bytes differ"


def test_billing_moves_while_the_pod_runs(session: Session, rental: Rental):
    """Per-second billing settles on a 5-minute accrual tick (docs): once the tick has landed the account balance
    (the server's number) is below what it was before the rent; a pod running past the tick with the balance
    unmoved is a billing hole. `ps`'s spent_usd is computed by the CLI from created_at × price, so it proves nothing
    about the server and is not read here."""
    if not rental.running_at:
        pytest.skip("pod not running")
    if rental.balance_before is None:
        pytest.skip("balance before the rent unknown")
    elapsed = time.monotonic() - rental.up_called_at
    if elapsed < 330:
        # the first accrual tick may not have landed yet; wait for it once
        time.sleep(330 - elapsed)
    bal = _balance(session)
    elapsed = time.monotonic() - rental.up_called_at
    assert bal < rental.balance_before, f"balance {rental.balance_before}→{bal} unchanged after {elapsed:.0f}s of rental"


def test_rm_removes_the_pod_and_the_final_charge_matches_the_clock(session: Session, rental: Rental):
    if not rental.pod:
        pytest.skip("no pod")
    if keep_pod():
        # the ordered rm used to run here whatever had failed before it, so E2E_KEEP_POD=1 kept nothing
        pytest.skip(f"E2E_KEEP_POD=1 and an earlier step failed: {rental.name} is kept for a look (30-min TTL)")
    r = session.lium("rm", rental.name, "-y", timeout=120)
    assert r.rc == 0, r
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and any(p.get("name") == rental.name for p in ps(session)):
        time.sleep(5)
    assert not any(p.get("name") == rental.name for p in ps(session)), "pod still listed 120s after rm"
    rental.pod = {}
    if rental.balance_before is not None and rental.price_per_hour:
        time.sleep(20)
        charged = rental.balance_before - _balance(session)
        seconds = time.monotonic() - rental.up_called_at
        expected = rental.price_per_hour * seconds / 3600
        # per-second billing: within 25 % + a cent of the wall-clock rental, and never a 15-minute floor
        assert charged <= expected * 1.25 + 0.01, f"charged ${charged:.4f} for {seconds:.0f}s at ${rental.price_per_hour}/h (expected ≈ ${expected:.4f})"
        assert charged >= 0, charged
