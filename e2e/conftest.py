"""The real `lium` CLI of this checkout against a real Lium API, from a throwaway HOME.

Target (env):
  E2E_API_URL   the API the CLI talks to — default https://staging.lium.io/api; the lium-platform e2e stack works too
                (http://localhost:8000/api with its seeded key).
  E2E_API_KEY   an API key of a FUNDED account there (the rent tests skip, loudly, on a zero balance).
  E2E_LIUM      the lium executable (default: `lium` on PATH — CI installs this checkout into a venv first).
  E2E_MAX_PRICE the most the suite will rent per hour (default 0.50 $/h); the cheapest listed node is chosen.
  E2E_EXCLUDE_COUNTRIES  comma-separated countries never rented; default Russia,Belarus,RU,BY (the loop's own rule; the
                CLI shows the ISO code when the listing has no country name). An explicit empty value lifts it.
  E2E_EXCLUDE_EXECUTORS  comma-separated executor ids or huids never rented (a node known to be defective — B-119's
                brave-shark-ff billed 2 GPUs and exposed 1 — would otherwise be the cheapest pick on every run).
  E2E_KEEP_POD  =1 leaves the pod up on failure for a human to look at (never in CI): once a step has failed, no
                removal this run would do runs — the `rental`/`sdk_pod` finalizers, the renter journey's `rm` step
                (skipped) and the stale-pod sweep at the start; the pod's own 30-min TTL still applies — or, when
                `up` itself never returned (killed between the rent and its --ttl call, or before DAH-3331 any time
                before RUNNING), the fixture schedules a 30-min removal instead. With no failure the journey removes its pod as usual.

Every command runs with HOME set to a temp dir, so `up` mints its SSH key there and the first-run shell-completion
hook edits a shell rc nobody uses (L-65: never the runner's ~/.lium). The key travels only as LIUM_API_KEY.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

API_URL = os.environ.get("E2E_API_URL", "https://staging.lium.io/api").rstrip("/")
API_KEY = os.environ.get("E2E_API_KEY", "")
LIUM = os.environ.get("E2E_LIUM", "lium")
MAX_PRICE = float(os.environ.get("E2E_MAX_PRICE", "0.50"))
EXCLUDE_COUNTRIES = {c.strip().lower() for c in os.environ.get("E2E_EXCLUDE_COUNTRIES", "Russia,Belarus,RU,BY").split(",") if c.strip()}
EXCLUDE_EXECUTORS = {e.strip().lower() for e in os.environ.get("E2E_EXCLUDE_EXECUTORS", "").split(",") if e.strip()}
KEEP_POD = os.environ.get("E2E_KEEP_POD", "") == "1"
ARTIFACTS = Path(os.environ.get("E2E_ARTIFACTS", Path(__file__).parent / "artifacts"))

FAILED_STEPS: list[str] = []   # node ids of the steps that failed so far in this process (setup, call or teardown)


def pytest_runtest_logreport(report) -> None:
    if report.failed:
        FAILED_STEPS.append(report.nodeid)


def keep_pod() -> bool:
    """E2E_KEEP_POD=1 and a step has failed: every removal this run would do is skipped."""
    return KEEP_POD and bool(FAILED_STEPS)


def rentable(gpu_count: int | str | None, price_per_hour: float | str | None, country: str | None,
             executor_id: str | None, huid: str | None) -> bool:
    """≥ 1 GPU, within the price cap, not in an excluded country, not an excluded executor (id or huid)."""
    if int(gpu_count or 0) < 1 or float(price_per_hour or 9e9) > MAX_PRICE:
        return False
    if str(country or "").strip().lower() in EXCLUDE_COUNTRIES:
        return False
    return not ({str(executor_id or "").lower(), str(huid or "").lower()} & EXCLUDE_EXECUTORS)


@dataclass
class Result:
    argv: list[str]
    rc: int
    out: str
    err: str
    seconds: float

    def json(self):
        return json.loads(self.out)

    def __repr__(self) -> str:  # short, key-free
        return f"<lium {' '.join(self.argv)} rc={self.rc} {self.seconds:.1f}s out={self.out[:200]!r} err={self.err[:200]!r}>"


@dataclass
class Session:
    home: Path
    log: list[dict] = field(default_factory=list)

    def lium(self, *args: str, key: str | None = API_KEY, timeout: int = 180, check: bool = False) -> Result:
        env = {
            "HOME": str(self.home),
            "PATH": os.environ["PATH"],
            "LIUM_BASE_URL": API_URL,
            "TERM": "dumb",
            "NO_COLOR": "1",
            "SHELL": "/bin/sh",
        }
        if key is not None:
            env["LIUM_API_KEY"] = key
        for passthrough in ("VIRTUAL_ENV", "PYTHONPATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
            if passthrough in os.environ:
                env[passthrough] = os.environ[passthrough]
        t0 = time.monotonic()
        p = subprocess.run([LIUM, *args], env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        r = Result(list(args), p.returncode, p.stdout, p.stderr, time.monotonic() - t0)
        self.log.append({"argv": r.argv, "rc": r.rc, "seconds": round(r.seconds, 2), "stdout_head": r.out[:400], "stderr_head": r.err[:400]})
        if check and r.rc != 0:
            raise AssertionError(f"expected exit 0: {r!r}")
        return r


def _write_artifacts(session: Session) -> None:
    """Append this process's CLI calls to commands.json. run.sh runs one pytest process per suite and clears the
    file first; a suite that makes no CLI call (the SDK journey) must not replace the renter journey's log with []."""
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    path = ARTIFACTS / "commands.json"
    previous = json.loads(path.read_text()) if path.exists() else []
    path.write_text(json.dumps(previous + session.log, indent=1))


@pytest.fixture(scope="session")
def session(tmp_path_factory) -> Session:
    if not API_KEY:
        pytest.skip("E2E_API_KEY is not set — nothing to run against (set E2E_API_URL/E2E_API_KEY, see e2e/README.md)")
    if shutil.which(LIUM) is None and not Path(LIUM).exists():
        pytest.fail(f"lium executable not found: {LIUM} (install this checkout, or set E2E_LIUM)")
    home = tmp_path_factory.mktemp("home")
    (home / ".ssh").mkdir(mode=0o700)
    s = Session(home=home)
    yield s
    _write_artifacts(s)


@dataclass
class Rental:
    """One pod rented for the suite, removed no matter what (fixture finalizer + a sweep of stale e2e- pods)."""

    name: str
    executor_id: str = ""
    price_per_hour: float = 0.0
    gpu_type: str = ""
    pod: dict = field(default_factory=dict)
    up_called_at: float = 0.0
    running_at: float = 0.0
    balance_before: float | None = None


def ps(session: Session) -> list[dict]:
    r = session.lium("ps", "--format", "json", check=True)
    return r.json()


def rm_pods_named(session: Session, prefix: str, older_than_s: float = 0) -> int:
    n = 0
    now = time.time()
    for p in ps(session):
        name = p.get("name") or ""
        if not name.startswith(prefix):
            continue
        created = p.get("created_at")
        if older_than_s and created:
            try:
                from datetime import datetime, timezone

                age = now - datetime.fromisoformat(str(created).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
                if age < older_than_s:
                    continue
            except ValueError:
                # unreadable created_at: the age is unknown, so the pod is swept like an old one
                pass
        session.lium("rm", name, "-y", timeout=120)
        n += 1
    return n


@pytest.fixture(scope="session")
def rental(session: Session) -> Rental:
    # a previous run that died mid-way may have left a pod; anything of ours older than 30 min goes first —
    # unless a human asked to keep pods: the one they are looking at may be exactly that old
    if KEEP_POD:
        session.log.append({"note": "E2E_KEEP_POD=1: stale e2e- pods not swept"})
    else:
        swept = rm_pods_named(session, "e2e-", older_than_s=30 * 60)
        if swept:
            session.log.append({"note": f"swept {swept} stale e2e- pod(s)"})
    r = Rental(name=f"e2e-{time.strftime('%H%M%S')}-{os.getpid() % 1000:03d}")
    yield r
    if not (r.pod or r.up_called_at):
        return   # nothing was rented (the read-only steps skipped or failed before `up`)
    if keep_pod():
        if r.pod:
            session.log.append({"note": f"E2E_KEEP_POD=1: {r.name} kept after a failed step (its 30-min TTL still applies)"})
        elif any(p.get("name") == r.name for p in ps(session)):
            # `up` never returned (killed by its timeout, or exited non-zero after renting). Since DAH-3331 `up`
            # schedules --ttl right after the rent, so the pod usually has one already; a kill between the two
            # calls leaves none, and setting the same 30 minutes again is harmless — do it either way
            session.lium("rm", r.name, "--in", "30m", "-y", timeout=120)
            session.log.append({"note": f"E2E_KEEP_POD=1: {r.name} kept after `up` did not return; removal scheduled in 30m"})
        return
    # keyed on the name, not on `r.pod`: an `up` killed by its 300 s timeout, or one that exited non-zero after
    # renting (a GPU-count mismatch since #120), leaves a pod the test never recorded — and, killed between the
    # rent and the --ttl call, one without its --ttl
    for _ in range(3):
        if not any(p.get("name") == r.name for p in ps(session)):
            break
        if session.lium("rm", r.name, "-y", timeout=120).rc == 0:
            break
        time.sleep(5)
