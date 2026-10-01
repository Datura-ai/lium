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
  E2E_MIN_RELIABILITY  the lowest `reliability_score` (0–100, the public listing's blended provider score) the suite
                rents; default 90. 10 Sep 2026 (DAH-3383): a listed 2×3090 at $0.32/h scored 72 and had an unreachable
                SSH port map — it was the cheapest pick for three PRs in a row. A node with no score yet (null) is not
                excluded: that is a new node, not a bad one. The SDK's ExecutorInfo and `ls --format json` do not carry the
                score, so `reliability_scores()` reads the same public listing once per journey. The platform's own
                rental check (`rental_check_verified_status`) is not on GET /executors and, on 10 Sep 2026, 452 of 473
                verified executors were still PENDING, so it cannot be the rule here.
  E2E_KEEP_POD  =1 leaves the pod up on failure for a human to look at (never in CI): once a step has failed, no
                removal this run would do runs — the `rental`/`sdk_pod` finalizers, the renter journey's `rm` step
                (skipped) and the stale-pod sweep at the start; the pod's own 30-min TTL still applies — or, when
                `up` itself never returned (killed between the rent and its --ttl call, or before DAH-3331 any time
                before RUNNING), the fixture schedules a 30-min removal instead. With no failure the journey removes its pod as usual.

A node that refuses the rent gives way to the next one (DAH-3488). Both journeys keep every rentable node, cheapest
first, and rent the first that accepts: when `lium up` exits 3 with the CLI's "could not be rented" text (the API
answered the rent with a 400 such as "Can't rent node. Try again later.", or the node was taken meanwhile), or the
SDK's `up()` raises the plain `LiumError` that wraps the same answer, the journey makes sure no pod with its name
exists, records the refusal and tries the next candidate with a different executor id — at most MAX_UP_TRIES (3)
`up` calls per journey, all inside the step's one 540 s budget. A timeout, a pod that did not start, an
auth/permission/server/rate-limit error and every other exit are not retried anywhere. Each refusal is a `rent refused: <huid> (<id>) at $<price>/h: <message>` pytest
warning in the job log (and, in the CLI journey, a note in commands.json) (the fleet team reads which nodes refused); when every try
refused, the failure lists them. On 14 Sep 2026 the single candidate answered 400 on two PRs (lium#152, lium#164);
each cost a human a rerun.

Every command runs with HOME set to a temp dir, so `up` mints its SSH key there and the first-run shell-completion
hook edits a shell rc nobody uses (L-65: never the runner's ~/.lium). The key travels only as LIUM_API_KEY.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import pytest

API_URL = os.environ.get("E2E_API_URL", "https://staging.lium.io/api").rstrip("/")
API_KEY = os.environ.get("E2E_API_KEY", "")
LIUM = os.environ.get("E2E_LIUM", "lium")
MAX_PRICE = float(os.environ.get("E2E_MAX_PRICE", "0.50"))
EXCLUDE_COUNTRIES = {c.strip().lower() for c in os.environ.get("E2E_EXCLUDE_COUNTRIES", "Russia,Belarus,RU,BY").split(",") if c.strip()}
EXCLUDE_EXECUTORS = {e.strip().lower() for e in os.environ.get("E2E_EXCLUDE_EXECUTORS", "").split(",") if e.strip()}
MIN_RELIABILITY = float(os.environ.get("E2E_MIN_RELIABILITY", "90"))
KEEP_POD = os.environ.get("E2E_KEEP_POD", "") == "1"
ARTIFACTS = Path(os.environ.get("E2E_ARTIFACTS", Path(__file__).parent / "artifacts"))

MAX_UP_TRIES = 3   # `up` calls per journey, each on a different executor, before the rent step fails
OUTPUT_HEAD_CHARS = 1500   # of a command's stdout/stderr kept in commands.json and in a Result's repr (the failure line)
EXIT_API_ERROR = 3   # lium/cli/utils.py: "the API refused or failed the call" (test_e2e_rentable.py pins the value)
REFUSED_TEXT = "could not be rented"   # lium/cli/up/command.py's rent_rejected message: the API answered the rent and said no

FAILED_STEPS: list[str] = []   # node ids of the steps that failed so far in this process (setup, call or teardown)


def pytest_runtest_logreport(report) -> None:
    if report.failed:
        FAILED_STEPS.append(report.nodeid)


def keep_pod() -> bool:
    """E2E_KEEP_POD=1 and a step has failed: every removal this run would do is skipped."""
    return KEEP_POD and bool(FAILED_STEPS)


def rentable(gpu_count: int | str | None, price_per_hour: float | str | None, country: str | None,
             executor_id: str | None, huid: str | None, reliability: float | str | None = None) -> bool:
    """≥ 1 GPU, within the price cap, not in an excluded country, not an excluded executor (id or huid), and a
    `reliability_score` at or above E2E_MIN_RELIABILITY when the listing has one (None = no score yet: kept)."""
    if int(gpu_count or 0) < 1 or float(price_per_hour or 9e9) > MAX_PRICE:
        return False
    if str(country or "").strip().lower() in EXCLUDE_COUNTRIES:
        return False
    if reliability is not None and float(reliability) < MIN_RELIABILITY:
        return False
    return not ({str(executor_id or "").lower(), str(huid or "").lower()} & EXCLUDE_EXECUTORS)


def reliability_scores(url: str = API_URL, attempts: int = 3, pause_s: float = 3.0, sleep=time.sleep) -> dict[str, float | None]:
    """`reliability_score` by executor id from the public listing (GET /executors, no key needed) — the field the SDK's
    ExecutorInfo and `ls --format json` do not surface. The same URL the SDK's `ls()` reads, as one unauthenticated
    request retried the way the SDK retries its own (three attempts on a 429, a 5xx or a network error); a 4xx other
    than 429 is raised at once."""
    req = urllib.request.Request(f"{url}/executors?size=1000", headers={"Accept": "application/json"})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 — https URL from E2E_API_URL, the suite's own target
                rows = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            if attempt == attempts or (e.code != 429 and e.code < 500):
                raise
        except (urllib.error.URLError, OSError):
            if attempt == attempts:
                raise
        sleep(pause_s)
    if not isinstance(rows, list):
        raise AssertionError(f"GET /executors did not return a list: {str(rows)[:200]!r}")
    return {str(row.get("id")): row.get("reliability_score") for row in rows if isinstance(row, dict) and row.get("id")}


@dataclass
class Result:
    argv: list[str]
    rc: int
    out: str
    err: str
    seconds: float

    def json(self):
        return json.loads(self.out)

    def __repr__(self) -> str:  # key-free (the key travels in the environment, never in argv or the output)
        head = OUTPUT_HEAD_CHARS   # 200 cut the API's answer out of the failure line on 14 Sep 2026 (r142)
        return f"<lium {' '.join(self.argv)} rc={self.rc} {self.seconds:.1f}s out={self.out[:head]!r} err={self.err[:head]!r}>"


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
        self.log.append({"argv": r.argv, "rc": r.rc, "seconds": round(r.seconds, 2),
                         "stdout_head": r.out[:OUTPUT_HEAD_CHARS], "stderr_head": r.err[:OUTPUT_HEAD_CHARS]})
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
    executor_id: str = ""        # the node `up` is (or was last) sent to; after a rent, the node that took it
    price_per_hour: float = 0.0
    gpu_type: str = ""
    candidates: list[dict] = field(default_factory=list)   # every rentable `ls` row, cheapest first, one per executor id
    refused: list[str] = field(default_factory=list)       # the `rent refused: …` notes of this run, in order
    pod: dict = field(default_factory=dict)
    up_called_at: float = 0.0
    running_at: float = 0.0
    balance_before: float | None = None


def ps(session: Session) -> list[dict]:
    r = session.lium("ps", "--format", "json", check=True)
    return r.json()


def cheapest_first(nodes: list[dict]) -> list[dict]:
    """The rentable `ls --format json` rows by price, one row per executor id (the first seen keeps its place)."""
    seen: set[str] = set()
    ordered = []
    for n in sorted(nodes, key=lambda n: float(n["price_per_hour"])):
        if str(n.get("id")) not in seen:
            seen.add(str(n.get("id")))
            ordered.append(n)
    return ordered


def refusal_note(huid: str, executor_id: str, price_per_hour: float | str | None, message: str) -> str:
    """One refused rent, the way the fleet team reads it in commands.json and the job's warnings summary."""
    price = f"${float(price_per_hour):.2f}/h" if price_per_hour not in (None, "") else "price unknown"
    return f"rent refused: {huid} ({executor_id}) at {price}: {' '.join(message.split())}"


def rent_refused(r: Result) -> bool:
    """`lium up` exit 3 carrying the CLI's rent_rejected text: the API answered the rent and said no (400 "Can't rent
    node", the node taken meanwhile). An api_timeout is exit 3 too but says "got no answer" — a pod may exist, so it is
    not one; nor is pod_start_failed (a pod was created) or any exit 1/2."""
    return r.rc == EXIT_API_ERROR and REFUSED_TEXT in (r.out + r.err)


MIN_RETRY_BUDGET_S = 60   # a further `up` starts only with at least this much of the step's budget left


def up_first_accepting(session: Session, rental: Rental, budget_s: float = 540, clock=time.monotonic) -> Result:
    """`lium up` on the cheapest candidate; a node that refuses the rent gives way to the next one, a different executor
    id each time, at most MAX_UP_TRIES `up` calls — all inside ONE budget of `budget_s` (the caller's 540 s, under the
    module's 600 s pytest-timeout): each `up` gets the time left, the `ps` after a refusal too, and a further try starts
    only with MIN_RETRY_BUDGET_S left. Returns the `up` that was not a refusal, with `rental.executor_id`,
    `price_per_hour` and `gpu_type` set to the node it went to; the caller asserts on its exit code as before, so a
    timeout, a pod that did not start or an exit 1/2 fails there and is never retried. When every try refused, or a
    refusal left a pod named `rental.name` behind (a retried rent must never rent twice), the step fails naming them.
    The `ps` after a refusal fails closed: a `ps` that exits non-zero is an AssertionError (`check=True`), not a rent
    elsewhere. Every refusal is a note in commands.json, a pytest warning and a line of `rental.refused`."""
    deadline = clock() + budget_s
    tried: set[str] = set()
    stopped = ""
    for node in rental.candidates:
        node_id, huid = str(node.get("id")), str(node.get("huid") or node.get("id"))
        if node_id in tried:
            continue
        if len(tried) >= MAX_UP_TRIES:
            break
        left_s = deadline - clock()
        if tried and left_s < MIN_RETRY_BUDGET_S:
            stopped = f"; {left_s:.0f} s of the {budget_s:.0f} s step budget left, no further node tried"
            break
        tried.add(node_id)
        rental.executor_id, rental.price_per_hour, rental.gpu_type = node_id, float(node.get("price_per_hour") or 0), str(node.get("gpu_type"))
        r = session.lium("up", node_id, "--name", rental.name, "--ttl", "30m", "-y", "--no-ssh", timeout=max(1, int(left_s)))
        if not rent_refused(r):
            return r
        note = refusal_note(huid, node_id, node.get("price_per_hour"), (r.out + r.err).strip())
        rental.refused.append(note)
        session.log.append({"note": note})
        warnings.warn(note, stacklevel=2)
        listed = session.lium("ps", "--format", "json", check=True, timeout=max(1, min(180, int(deadline - clock())))).json()
        if any(p.get("name") == rental.name for p in listed):
            pytest.fail(f"{huid} refused the rent but a pod named {rental.name} exists — not renting elsewhere "
                        f"(the rental fixture removes it by name): {r!r}")
    left = len({str(n.get("id")) for n in rental.candidates} - tried)
    pytest.fail(f"{len(tried)} node(s) refused the rent, none accepted (MAX_UP_TRIES={MAX_UP_TRIES}"
                + (f", {left} rentable candidate(s) left untried" if left else "") + stopped + "):\n  " + "\n  ".join(rental.refused))


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
