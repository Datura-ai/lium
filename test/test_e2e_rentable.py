"""`e2e/conftest.py`'s `rentable()` — the guard that picks the node the live suite rents (DAH-3151).

The e2e suite only ever calls it on live listings, so the unit suite carries the negative controls: a node in an
excluded country, an excluded executor (by id or by huid), no GPU, over the price cap — and the accept case.
"""

import importlib.util
import json
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

CONFTEST = Path(__file__).resolve().parent.parent / "e2e" / "conftest.py"


def _load(monkeypatch, **env):
    """A fresh import of e2e/conftest.py with the given E2E_* variables (it reads them at import)."""
    for name in ("E2E_EXCLUDE_COUNTRIES", "E2E_EXCLUDE_EXECUTORS", "E2E_MAX_PRICE", "E2E_KEEP_POD", "E2E_MIN_RELIABILITY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    spec = importlib.util.spec_from_file_location("e2e_conftest_under_test", CONFTEST)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)   # its dataclasses resolve string annotations via sys.modules
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "gpu_count, price, country, executor_id, huid, expected",
    [
        (1, 0.30, "Ukraine", "abc-123", "eager-comet-56", True),
        (1, 0.30, "Belarus", "abc-123", "eager-comet-56", False),      # the default exclusion
        (2, 0.30, "belarus", "abc-123", "eager-comet-56", False),      # case-insensitive
        (1, 0.30, "Russia", "abc-123", "eager-comet-56", False),
        (1, 0.30, "RU", "abc-123", "eager-comet-56", False),           # the CLI shows the ISO code when the listing has no country name
        (1, 0.30, "BY", "abc-123", "eager-comet-56", False),
        (1, 0.30, "Ukraine", "abc-123", "Brave-Shark-FF", False),      # excluded by huid, case-insensitive
        (1, 0.30, "Ukraine", "DEF-456", "eager-comet-56", False),      # excluded by id
        (0, 0.30, "Ukraine", "abc-123", "eager-comet-56", False),      # no GPU
        (1, 0.51, "Ukraine", "abc-123", "eager-comet-56", False),      # over E2E_MAX_PRICE
        (1, None, "Ukraine", "abc-123", "eager-comet-56", False),      # no price → never the cheapest
    ],
)
def test_rentable_with_the_defaults_and_an_executor_exclusion(monkeypatch, gpu_count, price, country, executor_id, huid, expected):
    conftest = _load(monkeypatch, E2E_EXCLUDE_EXECUTORS="brave-shark-ff,def-456")
    assert conftest.rentable(gpu_count, price, country, executor_id, huid) is expected


def test_the_country_exclusion_is_on_by_default_and_an_explicit_empty_value_lifts_it(monkeypatch):
    assert _load(monkeypatch).EXCLUDE_COUNTRIES == {"russia", "belarus", "ru", "by"}
    conftest = _load(monkeypatch, E2E_EXCLUDE_COUNTRIES="")
    assert conftest.EXCLUDE_COUNTRIES == set()
    assert conftest.rentable(1, 0.30, "Belarus", "abc-123", "eager-comet-56") is True


@pytest.mark.parametrize(
    "env, reliability, expected",
    [
        ({}, 72.2, False),          # DAH-3383: the 209.137.138.108 node — listed, cheapest, dead port map, score 72
        ({}, 89.99, False),
        ({}, 90, True),             # the floor itself rents
        ({}, "98.1", True),         # a string from a JSON payload is read as a number
        ({}, None, True),           # no score yet = a new node, not a bad one: kept
        ({"E2E_MIN_RELIABILITY": "0"}, 1.0, True),   # an explicit 0 lifts the floor
        ({"E2E_MIN_RELIABILITY": "99"}, 98.9, False),
    ],
)
def test_rentable_applies_the_reliability_floor_only_when_the_listing_has_a_score(monkeypatch, env, reliability, expected):
    conftest = _load(monkeypatch, **env)
    assert conftest.rentable(1, 0.30, "Ukraine", "abc-123", "eager-comet-56", reliability) is expected


def test_rentable_without_a_reliability_argument_behaves_as_before(monkeypatch):
    """Both journeys pass the score; a caller that does not (the old five-argument form) is not filtered by it."""
    conftest = _load(monkeypatch)
    assert conftest.rentable(1, 0.30, "Ukraine", "abc-123", "eager-comet-56") is True


class _FakeListing:
    """What `urllib.request.urlopen` yields for GET /executors: a context manager whose body `json.load` reads."""

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        import io
        import json

        return io.StringIO(json.dumps(self.payload))

    def __exit__(self, *exc):
        return False


def test_reliability_scores_reads_the_public_listing_by_executor_id(monkeypatch):
    conftest = _load(monkeypatch, E2E_API_URL="https://lium.io/api")
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"], seen["timeout"] = req.full_url, timeout
        return _FakeListing([
            {"id": "4a6b251c-1d30", "reliability_score": 72.2, "price_per_gpu": 0.16},
            {"id": "1117c0f6-aaaa", "reliability_score": None},           # not enough data yet
            {"id": "7fd1ff4d-bbbb", "reliability_score": 98.1},
            {"no_id": True},                                              # an unreadable row is skipped, not fatal
            "not-a-dict",
        ])

    monkeypatch.setattr(conftest.urllib.request, "urlopen", fake_urlopen)
    scores = conftest.reliability_scores()
    assert seen["url"] == "https://lium.io/api/executors?size=1000" and seen["timeout"] == 60   # the SDK's own ls() request
    assert scores == {"4a6b251c-1d30": 72.2, "1117c0f6-aaaa": None, "7fd1ff4d-bbbb": 98.1}
    # and a payload that is not the listing is an assertion, not a silent empty map that would lift the floor
    monkeypatch.setattr(conftest.urllib.request, "urlopen", lambda req, timeout: _FakeListing({"detail": "rate limited"}))
    with pytest.raises(AssertionError, match="did not return a list"):
        conftest.reliability_scores(sleep=lambda s: None)


def _http_error(code):
    import urllib.error

    return urllib.error.HTTPError("https://lium.io/api/executors", code, "err", {}, None)


def test_reliability_scores_retries_like_the_sdk_and_raises_the_rest_at_once(monkeypatch):
    """The SDK's ls() retries a 429, a 5xx or a network error three times; this one GET does the same, and a 404 or
    a 401 comes straight back."""
    import urllib.error

    conftest = _load(monkeypatch)
    slept = []

    def flaky(errors):
        errors = list(errors)

        def urlopen(req, timeout):
            if errors:
                raise errors.pop(0)
            return _FakeListing([{"id": "abc", "reliability_score": 99.0}])

        return urlopen

    monkeypatch.setattr(conftest.urllib.request, "urlopen", flaky([_http_error(502), urllib.error.URLError("reset")]))
    assert conftest.reliability_scores(sleep=slept.append) == {"abc": 99.0} and slept == [3.0, 3.0]
    monkeypatch.setattr(conftest.urllib.request, "urlopen", flaky([_http_error(429), _http_error(429), _http_error(429)]))
    with pytest.raises(urllib.error.HTTPError):   # the third failure is the last: raised, not swallowed
        conftest.reliability_scores(sleep=lambda s: None)
    monkeypatch.setattr(conftest.urllib.request, "urlopen", flaky([_http_error(404)]))
    slept.clear()
    with pytest.raises(urllib.error.HTTPError):
        conftest.reliability_scores(sleep=slept.append)
    assert slept == []


def _load_sdk_journey(monkeypatch, conftest):
    monkeypatch.setitem(sys.modules, "conftest", conftest)   # the journey module does `from conftest import …`
    spec = importlib.util.spec_from_file_location("e2e_sdk_journey_under_test", CONFTEST.parent / "test_sdk_journey.py")
    journey = importlib.util.module_from_spec(spec)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", pytest.PytestUnknownMarkWarning)   # its `timeout` mark is pytest-timeout's, an e2e-only dependency
        spec.loader.exec_module(journey)
    return journey


class _FlakySsh:
    """An SDK whose `exec` raises the given errors in order, then answers."""

    def __init__(self, errors):
        self.errors, self.calls = list(errors), []

    def exec(self, pod, *, command):
        self.calls.append(command)
        if self.errors:
            raise self.errors.pop(0)
        return {"success": True, "exit_code": 0, "stdout": "sdk-e2e\n", "stderr": ""}


def _no_valid_connections():
    import paramiko

    return paramiko.ssh_exception.NoValidConnectionsError({("209.137.138.108", 30299): ConnectionRefusedError(111, "refused")})


def test_first_exec_retries_connection_errors_within_the_budget(monkeypatch):
    """wait_ready reports the pod RUNNING, not sshd listening: the first connect may be refused for a few seconds."""
    import paramiko

    journey = _load_sdk_journey(monkeypatch, _load(monkeypatch))
    clock = {"t": 100.0}
    slept = []

    def sleep(s):
        slept.append(s)
        clock["t"] += s

    sdk = _FlakySsh([_no_valid_connections(), paramiko.SSHException("Error reading SSH protocol banner"), TimeoutError()])
    out = journey.first_exec(sdk, SimpleNamespace(id="pod-1"), "echo sdk-e2e", budget_s=30, pause_s=5, sleep=sleep, clock=lambda: clock["t"])
    assert out["success"] is True and len(sdk.calls) == 4 and slept == [5, 5, 5]


def test_first_exec_gives_up_with_the_last_error_once_the_budget_is_spent(monkeypatch):
    """A port map that never comes (the 10 Sep node) still fails — later, and with the same exception."""
    import paramiko

    journey = _load_sdk_journey(monkeypatch, _load(monkeypatch))
    clock = {"t": 0.0}

    def sleep(s):
        clock["t"] += s

    sdk = _FlakySsh([_no_valid_connections() for _ in range(100)])
    with pytest.raises(paramiko.ssh_exception.NoValidConnectionsError):
        journey.first_exec(sdk, SimpleNamespace(id="pod-1"), "true", budget_s=30, pause_s=5, sleep=sleep, clock=lambda: clock["t"])
    assert len(sdk.calls) == 7   # t = 0, 5, …, 30: the attempt at the deadline is the last one
    assert clock["t"] == 30


def test_first_exec_does_not_retry_what_is_not_a_connection_failure(monkeypatch):
    """A host-key mismatch, a missing key file or a bug in the caller is not sshd being late: raised at once, no sleep."""
    from lium.sdk.exceptions import LiumHostKeyError

    journey = _load_sdk_journey(monkeypatch, _load(monkeypatch))
    for err in (LiumHostKeyError("Host key for pod x changed"), FileNotFoundError("id_ed25519"), PermissionError("id_ed25519"),
                ValueError("No SSH for pod x"), KeyError("id")):
        sdk = _FlakySsh([err])
        with pytest.raises(type(err)):
            journey.first_exec(sdk, SimpleNamespace(id="pod-1"), "true", sleep=lambda s: pytest.fail("slept"), clock=lambda: 0.0)
        assert len(sdk.calls) == 1


def _failed(nodeid="e2e/test_renter_journey.py::test_exec_propagates_exit_codes_and_streams"):
    return SimpleNamespace(failed=True, nodeid=nodeid)


def test_keep_pod_needs_both_the_flag_and_a_failed_step(monkeypatch):
    conftest = _load(monkeypatch)                       # E2E_KEEP_POD unset: a failure changes nothing
    conftest.pytest_runtest_logreport(_failed())
    assert conftest.FAILED_STEPS == [_failed().nodeid]
    assert conftest.keep_pod() is False

    conftest = _load(monkeypatch, E2E_KEEP_POD="1")     # the flag alone: the journey still removes its pod
    conftest.pytest_runtest_logreport(SimpleNamespace(failed=False, nodeid="e2e/test_renter_journey.py::test_up_rents_exactly_one_pod"))
    assert conftest.keep_pod() is False
    conftest.pytest_runtest_logreport(_failed())
    assert conftest.keep_pod() is True


class _RecordingSession:
    """conftest.Session's surface the `rental` fixture uses: `lium(...)` (rm / ps) and `log`. `ps` answers with the
    pods in `listed` (by name) until an `rm` for one of them arrives."""

    def __init__(self, listed: tuple[str, ...] = ()):
        self.calls: list[tuple[str, ...]] = []
        self.log: list[dict] = []
        self.listed = set(listed)

    def lium(self, *args, **_):
        self.calls.append(args)
        if args[0] == "rm":
            self.listed.discard(args[1])
        return SimpleNamespace(rc=0, out="[]", err="", json=lambda: [{"name": n} for n in self.listed])


def _run_rental_fixture(conftest, fail_before_teardown: bool, recorded: bool = True) -> _RecordingSession:
    """Drive the `rental` fixture's generator as pytest would: set up, rent a pod, (fail a step,) tear down.
    `recorded=False` is the `up` that rented but never returned (killed by its timeout, or exited non-zero)."""
    fixture_fn = getattr(conftest.rental, "__wrapped__", conftest.rental)
    session = _RecordingSession()
    gen = fixture_fn(session)
    rental = next(gen)
    session.setup_calls = list(session.calls)   # what ran before the tests: the stale sweep, or nothing
    rental.up_called_at = 1.0
    session.listed.add(rental.name)   # the API lists the pod whether or not the test recorded it
    if recorded:
        rental.pod = {"id": "pod-1", "name": rental.name}
    if fail_before_teardown:
        conftest.pytest_runtest_logreport(_failed())
    with pytest.raises(StopIteration):
        next(gen)
    return session


@pytest.mark.parametrize(
    "keep_flag, failed, rm_expected",
    [
        ({}, True, True),                    # default: a failed run still removes its pod (3c47ae4's behaviour)
        ({"E2E_KEEP_POD": "1"}, False, True),   # the flag with a green run: nothing to look at, the pod goes
        ({"E2E_KEEP_POD": "1"}, True, False),   # the flag and a failed step: no rm, a note in the log
    ],
)
def test_the_rental_finalizer_keeps_the_pod_only_for_a_failed_run_under_keep_pod(monkeypatch, keep_flag, failed, rm_expected):
    conftest = _load(monkeypatch, **keep_flag)
    session = _run_rental_fixture(conftest, fail_before_teardown=failed)
    rm_calls = [c for c in session.calls if c[0] == "rm"]
    assert bool(rm_calls) is rm_expected, session.calls
    if not rm_expected:
        assert any("kept after a failed step" in note.get("note", "") for note in session.log), session.log


def test_a_pod_the_up_step_never_recorded_is_still_removed(monkeypatch):
    """`lium up` killed by the step's 300 s timeout, or exiting non-zero after renting (a GPU-count mismatch), leaves a
    pod `rental.pod` never saw; the finalizer removes by name whenever an `up` was attempted."""
    session = _run_rental_fixture(_load(monkeypatch), fail_before_teardown=True, recorded=False)
    assert [c for c in session.calls if c[0] == "rm"], session.calls
    # and it sends no rm when the API no longer lists the pod (the rm step already removed it)
    conftest = _load(monkeypatch)
    fixture_fn = getattr(conftest.rental, "__wrapped__", conftest.rental)
    session = _RecordingSession()
    gen = fixture_fn(session)
    rental = next(gen)
    rental.up_called_at = 1.0
    with pytest.raises(StopIteration):
        next(gen)
    assert [c for c in session.calls if c[0] == "rm"] == [], session.calls


def test_under_keep_pod_a_pod_whose_up_never_returned_gets_a_scheduled_removal(monkeypatch):
    """An `up` killed by its timeout may leave a rented pod without its --ttl (killed between the rent and the
    schedule call). Kept for a look under E2E_KEEP_POD=1, it gets the 30 minutes: `lium rm <name> --in 30m -y`."""
    session = _run_rental_fixture(_load(monkeypatch, E2E_KEEP_POD="1"), fail_before_teardown=True, recorded=False)
    rm_calls = [c for c in session.calls if c[0] == "rm"]
    assert len(rm_calls) == 1 and rm_calls[0][2:] == ("--in", "30m", "-y"), session.calls
    assert any("removal scheduled in 30m" in n.get("note", "") for n in session.log), session.log
    # a recorded pod (its `up` returned, the TTL is set) is left alone
    session = _run_rental_fixture(_load(monkeypatch, E2E_KEEP_POD="1"), fail_before_teardown=True, recorded=True)
    assert [c for c in session.calls if c[0] == "rm"] == [], session.calls


def test_under_keep_pod_the_stale_sweep_does_not_run(monkeypatch):
    # a human re-running with E2E_KEEP_POD=1 may be looking at a pod older than 30 min: it is not swept
    session = _run_rental_fixture(_load(monkeypatch, E2E_KEEP_POD="1"), fail_before_teardown=False)
    assert session.setup_calls == []
    swept_by_default = _run_rental_fixture(_load(monkeypatch), fail_before_teardown=False)
    assert swept_by_default.setup_calls == [("ps", "--format", "json")]


def test_the_rm_step_is_skipped_under_keep_pod_after_a_failure(monkeypatch):
    """The ordered `rm` test used to run whatever had failed before it — the reason E2E_KEEP_POD=1 kept nothing."""
    conftest = _load(monkeypatch, E2E_KEEP_POD="1")
    monkeypatch.setitem(sys.modules, "conftest", conftest)   # the journey module does `from conftest import …`
    spec = importlib.util.spec_from_file_location("e2e_renter_journey_under_test", CONFTEST.parent / "test_renter_journey.py")
    journey = importlib.util.module_from_spec(spec)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", pytest.PytestUnknownMarkWarning)   # its `timeout` mark is pytest-timeout's, an e2e-only dependency
        spec.loader.exec_module(journey)
    session = _RecordingSession()
    rental = conftest.Rental(name="e2e-000000-001", pod={"id": "pod-1"})
    conftest.pytest_runtest_logreport(_failed())
    with pytest.raises(pytest.skip.Exception, match="E2E_KEEP_POD=1 and an earlier step failed"):
        journey.test_rm_removes_the_pod_and_the_final_charge_matches_the_clock(session, rental)
    assert session.calls == []   # no rm was sent


def test_the_ci_job_states_the_same_exclusion_as_the_default(monkeypatch):
    """ci.yml carries the list so the job says what it does; it must not drift from conftest's default."""
    ci = (CONFTEST.parent.parent / ".github" / "workflows" / "ci.yml").read_text()
    conftest = _load(monkeypatch)
    line = next(line for line in ci.splitlines() if "E2E_EXCLUDE_COUNTRIES:" in line)
    stated = {c.strip().lower() for c in line.split('"')[1].split(",")}
    assert stated == conftest.EXCLUDE_COUNTRIES


# ---------------------------------------------------------------- a node that refuses the rent (DAH-3488) ---------
# 14 Sep 2026, lium#152 and lium#164: the one candidate answered `400 Can't rent node. Try again later.` and the whole
# run went red although other rentable nodes were listed. The rent step now moves to the next node, ≤ 3 distinct
# executors; these tests drive the REAL journey step against a fake `lium` executable (the subprocess path
# `Session.lium` takes), never a live API.

FAKE_LIUM = r'''#!%s
"""A `lium` that rents or refuses by executor id. State in $HOME/fake-lium.json (Session.lium passes HOME only):
  refuse[]: executor ids answered with the CLI's rent_rejected text, exit 3
  pod_despite_refusal[]: refused ids that still leave a pod named --name behind (a retried POST that did rent)
  no_answer[]: ids answered with the api_timeout text, exit 3  ·  crash[]: ids answered with exit 1  ·  ps_fails: `ps` exits 3
  huids{}: id → huid  ·  pods[]: names `ps` lists  ·  ups[]: every `up` argv seen"""
import json, os, sys
path = os.path.join(os.environ["HOME"], "fake-lium.json")
st = json.load(open(path))
args = sys.argv[1:]
if args[0] == "ps":
    if st.get("ps_fails"):
        print("API error 502: bad gateway", file=sys.stderr)
        sys.exit(3)
    print(json.dumps([{"name": n, "id": "pod-%%d" %% i, "huid": "pod-huid-%%d" %% i, "status": "RUNNING"} for i, n in enumerate(st["pods"])]))
    sys.exit(0)
assert args[0] == "up", args
ex, name = args[1], args[args.index("--name") + 1]
huid = st.get("huids", {}).get(ex, ex)
st.setdefault("ups", []).append(args)
if ex in st.get("refuse", []):
    if ex in st.get("pod_despite_refusal", []):
        st["pods"].append(name)
    json.dump(st, open(path, "w"))
    print("Est. deploy time: ~1m 0s (image: ~3.5 GB, download: 172 Mbps)\nrenting %%s…" %% huid)
    print("Node %%s could not be rented: API error 400: Can't rent node. Try \nagain later.. Run 'lium ps' to check whether a pod was created. Run 'lium ls --format json' for the nodes rentable now." %% huid)
    sys.exit(3)
json.dump(st, open(path, "w"))
if ex in st.get("no_answer", []):
    print("The rent request for %%s got no answer from the API (ConnectTimeout). Run 'lium ps' before retrying: a pod named %%s may exist and be billing." %% (huid, name))
    sys.exit(3)
if ex in st.get("crash", []):
    print("Traceback (most recent call last): boom", file=sys.stderr)
    sys.exit(1)
st["pods"].append(name)
json.dump(st, open(path, "w"))
print("pod %%s (id: pod-%%d) created; waiting for it to become ready" %% (name, len(st["pods"]) - 1))
sys.exit(0)
''' % sys.executable   # noqa: UP031 — the script body is full of braces; %-format keeps it a plain raw string


def _fake_lium(tmp_path, **state) -> tuple[Path, Path]:
    """The fake executable and a HOME holding its state file. Returns (executable, home)."""
    home = tmp_path / "home"
    home.mkdir()
    exe = tmp_path / "lium"
    exe.write_text(FAKE_LIUM)
    exe.chmod(0o755)
    state.setdefault("pods", [])
    (home / "fake-lium.json").write_text(json.dumps(state))
    return exe, home


def _fake_state(home: Path) -> dict:
    return json.loads((home / "fake-lium.json").read_text())


def _node(i: int, price: float, node_id: str | None = None) -> dict:
    return {"id": node_id or f"exec-{i}", "huid": f"huid-{i}", "gpu_type": "RTX 4090", "gpu_count": 1, "price_per_hour": price}


def _load_renter_journey(monkeypatch, conftest):
    monkeypatch.setitem(sys.modules, "conftest", conftest)
    spec = importlib.util.spec_from_file_location("e2e_renter_journey_under_test", CONFTEST.parent / "test_renter_journey.py")
    journey = importlib.util.module_from_spec(spec)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", pytest.PytestUnknownMarkWarning)
        spec.loader.exec_module(journey)
    return journey


def test_the_rent_step_moves_to_the_next_node_when_the_cheapest_refuses(monkeypatch, tmp_path):
    """The 14 Sep failure: the cheapest node answers 400. Two refusals, the third node rents; the step passes and
    records which nodes refused, in the shape the fleet team greps for."""
    exe, home = _fake_lium(tmp_path, refuse=["exec-1", "exec-2"], huids={"exec-1": "brave-eagle-b8", "exec-2": "huid-2"})
    conftest = _load(monkeypatch, E2E_LIUM=str(exe))
    journey = _load_renter_journey(monkeypatch, conftest)
    session = conftest.Session(home=home)
    # `ls` listed them out of price order and with the cheapest id twice; the journey orders and dedupes
    eagle = {**_node(1, 0.30), "huid": "brave-eagle-b8"}   # the node that answered 400 on lium#164
    rental = conftest.Rental(name="e2e-000000-001", executor_id="exec-1", balance_before=5.0,
                             candidates=conftest.cheapest_first([_node(3, 0.35), eagle, _node(2, 0.32), eagle]))
    with pytest.warns(UserWarning, match="rent refused") as caught:
        journey.test_up_rents_exactly_one_pod(session, rental)
    ups = [u[1] for u in _fake_state(home)["ups"]]
    assert ups == ["exec-1", "exec-2", "exec-3"], ups                     # cheapest first, one `up` per node, three distinct
    assert rental.pod["name"] == "e2e-000000-001" and rental.executor_id == "exec-3" and rental.price_per_hour == 0.35
    tail = ("could not be rented: API error 400: Can't rent node. Try again later.. Run 'lium ps' to check whether a pod was "
            "created. Run 'lium ls --format json' for the nodes rentable now.")
    assert rental.refused == [
        f"rent refused: brave-eagle-b8 (exec-1) at $0.30/h: Est. deploy time: ~1m 0s (image: ~3.5 GB, download: 172 Mbps) renting brave-eagle-b8… Node brave-eagle-b8 {tail}",
        f"rent refused: huid-2 (exec-2) at $0.32/h: Est. deploy time: ~1m 0s (image: ~3.5 GB, download: 172 Mbps) renting huid-2… Node huid-2 {tail}",
    ]
    assert [n["note"] for n in session.log if "note" in n] == rental.refused   # commands.json carries the same lines
    assert [str(w.message) for w in caught] == rental.refused                  # and the job log's warnings summary
    # a refusal checks that nothing was rented before moving on: one `ps` after each refused `up`
    argv = [tuple(e["argv"]) for e in session.log if "argv" in e]
    assert argv[:4] == [("up", "exec-1", "--name", "e2e-000000-001", "--ttl", "30m", "-y", "--no-ssh"), ("ps", "--format", "json"),
                        ("up", "exec-2", "--name", "e2e-000000-001", "--ttl", "30m", "-y", "--no-ssh"), ("ps", "--format", "json")]


def test_the_rent_step_stops_after_three_distinct_refusals_and_names_them(monkeypatch, tmp_path):
    exe, home = _fake_lium(tmp_path, refuse=["exec-1", "exec-2", "exec-3", "exec-4"])
    conftest = _load(monkeypatch, E2E_LIUM=str(exe))
    session = conftest.Session(home=home)
    rental = conftest.Rental(name="e2e-000000-002", candidates=conftest.cheapest_first([_node(i, 0.30 + i / 100) for i in (1, 2, 3, 4)]))
    with pytest.warns(UserWarning), pytest.raises(pytest.fail.Exception) as failed:
        conftest.up_first_accepting(session, rental)
    assert [u[1] for u in _fake_state(home)["ups"]] == ["exec-1", "exec-2", "exec-3"]   # the fourth is never tried
    text = str(failed.value)
    assert text.startswith("3 node(s) refused the rent, none accepted (MAX_UP_TRIES=3, 1 rentable candidate(s) left untried):")
    assert text.count("rent refused: ") == 3 and "huid-1 (exec-1)" in text and "huid-3 (exec-3)" in text and "huid-4" not in text
    assert _fake_state(home)["pods"] == []


def test_a_refusal_that_left_a_pod_behind_stops_the_retry(monkeypatch, tmp_path):
    """`lium up` says "Run 'lium ps' to check whether a pod was created" for a reason: a retried POST can rent and
    still report a refusal. Renting elsewhere then would make two pods; the step fails and names the pod instead."""
    exe, home = _fake_lium(tmp_path, refuse=["exec-1"], pod_despite_refusal=["exec-1"])
    conftest = _load(monkeypatch, E2E_LIUM=str(exe))
    session = conftest.Session(home=home)
    rental = conftest.Rental(name="e2e-000000-003", candidates=[_node(1, 0.30), _node(2, 0.32)])
    with pytest.warns(UserWarning), pytest.raises(pytest.fail.Exception, match="huid-1 refused the rent but a pod named e2e-000000-003 exists"):
        conftest.up_first_accepting(session, rental)
    assert [u[1] for u in _fake_state(home)["ups"]] == ["exec-1"]
    assert rental.executor_id == "exec-1" and rental.refused and len(rental.refused) == 1


@pytest.mark.parametrize("state, rc", [({"no_answer": ["exec-1"]}, 3), ({"crash": ["exec-1"]}, 1)])
def test_only_a_refusal_is_retried(monkeypatch, tmp_path, state, rc):
    """Exit 3 with the api_timeout text (a pod MAY exist) and exit 1 come back to the caller's assertion unchanged,
    with no second `up`: those are not "try another node"."""
    exe, home = _fake_lium(tmp_path, **state)
    conftest = _load(monkeypatch, E2E_LIUM=str(exe))
    session = conftest.Session(home=home)
    rental = conftest.Rental(name="e2e-000000-004", candidates=[_node(1, 0.30), _node(2, 0.32)])
    with warnings.catch_warnings():
        warnings.simplefilter("error")   # a warning here would be a refusal wrongly recorded
        r = conftest.up_first_accepting(session, rental)
    assert r.rc == rc and [u[1] for u in _fake_state(home)["ups"]] == ["exec-1"]
    assert rental.refused == [] and conftest.rent_refused(r) is False


def test_a_failing_ps_after_a_refusal_fails_the_step_instead_of_renting_elsewhere(monkeypatch, tmp_path):
    """The `ps` that proves the refusal rented nothing is check=True: when it fails, nothing is known, so no second `up`."""
    exe, home = _fake_lium(tmp_path, refuse=["exec-1"], ps_fails=True)
    conftest = _load(monkeypatch, E2E_LIUM=str(exe))
    session = conftest.Session(home=home)
    rental = conftest.Rental(name="e2e-000000-006", candidates=[_node(1, 0.30), _node(2, 0.32)])
    with pytest.warns(UserWarning), pytest.raises(AssertionError, match=r"expected exit 0: <lium ps --format json rc=3"):
        conftest.up_first_accepting(session, rental)
    assert [u[1] for u in _fake_state(home)["ups"]] == ["exec-1"]


class _SlowSession:
    """conftest.Session's surface `up_first_accepting` uses, with a clock the test drives: every `up` is a refusal that
    takes `up_seconds`, every `ps` lists nothing. Records the `timeout` each call was given."""

    def __init__(self, clock: dict, up_seconds: float):
        self.clock, self.up_seconds, self.calls, self.log = clock, up_seconds, [], []

    def lium(self, *args, timeout=180, check=False, **_):
        self.calls.append((args[0], args[1] if args[0] == "up" else None, timeout))
        if args[0] == "up":
            self.clock["t"] += self.up_seconds
            return SimpleNamespace(rc=3, out=f"Node {args[1]} could not be rented: API error 400: Can't rent node.", err="", json=lambda: None)
        self.clock["t"] += 1
        return SimpleNamespace(rc=0, out="[]", err="", json=list)


def test_the_retries_share_one_step_budget(monkeypatch):
    """Three `up` calls of 540 s each would run 27 min inside the module's 600 s pytest-timeout, which kills the process
    with no finalizer. Every call gets the time left of ONE budget, and a further node is tried only with 60 s left."""
    conftest = _load(monkeypatch)
    clock = {"t": 1000.0}
    session = _SlowSession(clock, up_seconds=250)   # a refusal that took 250 s (a slow API), twice
    rental = conftest.Rental(name="e2e-000000-007", candidates=[_node(i, 0.30 + i / 100) for i in (1, 2, 3, 4)])
    with pytest.warns(UserWarning), pytest.raises(pytest.fail.Exception) as failed:
        conftest.up_first_accepting(session, rental, budget_s=540, clock=lambda: clock["t"])
    # up #1 at t=0 with 540 s (250 s + a 1 s ps); up #2 at t=251 with 289 s; its ps gets min(180, 39) = 39; then 38 s
    # left < MIN_RETRY_BUDGET_S: stop — the third and fourth nodes are never tried
    assert session.calls == [("up", "exec-1", 540), ("ps", None, 180), ("up", "exec-2", 289), ("ps", None, 39)]
    assert "2 node(s) refused the rent, none accepted (MAX_UP_TRIES=3, 2 rentable candidate(s) left untried; 38 s of the 540 s step budget left, no further node tried):" in str(failed.value)
    assert len(rental.refused) == 2
    # with time to spare the same session gets all three tries
    clock["t"], session.calls = 1000.0, []
    rental = conftest.Rental(name="e2e-000000-008", candidates=[_node(i, 0.30 + i / 100) for i in (1, 2, 3, 4)])
    with pytest.warns(UserWarning), pytest.raises(pytest.fail.Exception, match=r"^3 node\(s\) refused"):
        conftest.up_first_accepting(session, rental, budget_s=5400, clock=lambda: clock["t"])
    assert [c[0] for c in session.calls] == ["up", "ps", "up", "ps", "up", "ps"]


def test_rent_refused_reads_the_cli_text_and_exit_code_the_cli_uses(monkeypatch):
    """The detector is pinned to `lium/cli/up/command.py`'s rent_rejected message and utils.EXIT_API_ERROR; either
    moving without this test would silently turn every refusal back into a red run."""
    from lium.cli.utils import EXIT_API_ERROR

    conftest = _load(monkeypatch)
    assert conftest.EXIT_API_ERROR == EXIT_API_ERROR == 3
    command_py = (CONFTEST.parent.parent / "lium" / "cli" / "up" / "command.py").read_text()
    assert f'"Node {{executor.huid}} {conftest.REFUSED_TEXT}: {{exc}}.' in command_py
    refused = conftest.Result(["up", "x"], 3, "renting a…\nNode a could not be rented: API error 400: Can't rent node. Try again later..", "", 2.8)
    assert conftest.rent_refused(refused) is True
    assert conftest.rent_refused(conftest.Result(["up", "x"], 3, "", "Node a could not be rented: API error 400: taken", 1.0)) is True   # either stream
    assert conftest.rent_refused(conftest.Result(["up", "x"], 0, "Node a could not be rented", "", 1.0)) is False    # exit 0 is a rent
    assert conftest.rent_refused(conftest.Result(["up", "x"], 3, "The rent request for a got no answer from the API", "", 1.0)) is False
    assert conftest.rent_refused(conftest.Result(["up", "x"], 3, "Pod p (id: 1) failed to start: image pull", "", 1.0)) is False


def test_cheapest_first_orders_by_price_and_keeps_one_row_per_executor(monkeypatch):
    conftest = _load(monkeypatch)
    rows = [_node(3, "0.35"), _node(1, 0.30), _node(2, 0.32), _node(1, 0.30), _node(9, 0.29, node_id="exec-1")]   # ids repeat
    assert [n["id"] for n in conftest.cheapest_first(rows)] == ["exec-1", "exec-2", "exec-3"]
    assert conftest.cheapest_first([]) == []


def test_refusal_note_shape(monkeypatch):
    conftest = _load(monkeypatch)
    assert conftest.refusal_note("brave-eagle-b8", "26a74144", 0.3, "API error 400: Can't rent node. Try \nagain later.") == \
        "rent refused: brave-eagle-b8 (26a74144) at $0.30/h: API error 400: Can't rent node. Try again later."
    assert conftest.refusal_note("h", "i", None, "x") == "rent refused: h (i) at price unknown: x"


def test_the_output_heads_keep_the_api_answer(monkeypatch, tmp_path):
    """r142: the 200-char repr cut the failing `up`'s answer at "Try" — a diagnosis round to learn what the API said.
    The failure line and commands.json keep OUTPUT_HEAD_CHARS of each stream, no more."""
    exe, home = _fake_lium(tmp_path, refuse=["exec-1"], huids={"exec-1": "brave-eagle-b8"})
    conftest = _load(monkeypatch, E2E_LIUM=str(exe))
    assert conftest.OUTPUT_HEAD_CHARS == 1500
    out = "x" * 1490 + "THE-ANSWER" + "y" * 200   # the answer ends at character 1,500; the 200 y's are past the head
    r = conftest.Result(["up", "exec-1"], 3, out, "e" * 1600, 2.8)
    assert "THE-ANSWER" in repr(r) and "y" not in repr(r) and "e" * 1500 in repr(r) and "e" * 1501 not in repr(r)
    # the 14 Sep refusal through the real subprocess path: the CLI's whole answer (~330 chars) is in commands.json
    session = conftest.Session(home=home)
    r = session.lium("up", "exec-1", "--name", "e2e-000000-005", "--ttl", "30m", "-y", "--no-ssh")
    entry = session.log[-1]
    assert set(entry) == {"argv", "rc", "seconds", "stdout_head", "stderr_head"} and entry["rc"] == 3
    assert len(entry["stdout_head"]) > 200 and entry["stdout_head"].rstrip().endswith("for the nodes rentable now.")
    assert "for the nodes rentable now." in repr(r)   # the failure line a human reads in the job log


class _RefusingSdk:
    """An SDK whose `up` raises per executor id and whose `ps` lists `pods` (objects with .name/.id)."""

    def __init__(self, refuse: dict, pods=()):
        self.refuse, self.pods, self.ups = refuse, list(pods), []

    def up(self, *, executor_id, name):
        self.ups.append(executor_id)
        if executor_id in self.refuse:
            raise self.refuse[executor_id]
        return {"id": f"pod-for-{executor_id}", "status": "PENDING", "name": name}

    def ps(self):
        return self.pods


def _sdk_node(i: int, price: float):
    return SimpleNamespace(id=f"exec-{i}", huid=f"huid-{i}", price_per_hour=price)


def test_sdk_up_moves_to_the_next_node_on_a_refusal(monkeypatch):
    from lium.sdk.exceptions import LiumError, LiumNotFoundError

    journey = _load_sdk_journey(monkeypatch, _load(monkeypatch))
    sdk = _RefusingSdk({"exec-1": LiumError("API error 400: Can't rent node. Try again later."), "exec-2": LiumNotFoundError("API error 404: executor not found")})
    state = {"pod": None, "name": "e2e-sdk-000000"}
    with pytest.warns(UserWarning, match="rent refused") as caught:
        created = journey.up_first_accepting(sdk, [_sdk_node(3, 0.35), _sdk_node(1, 0.30), _sdk_node(2, 0.32), _sdk_node(1, 0.30)], "e2e-sdk-000000", state)
    assert sdk.ups == ["exec-1", "exec-2", "exec-3"] and created["id"] == "pod-for-exec-3"
    assert state["refused"] == [
        "rent refused: huid-1 (exec-1) at $0.30/h: API error 400: Can't rent node. Try again later.",
        "rent refused: huid-2 (exec-2) at $0.32/h: API error 404: executor not found",
    ] == [str(w.message) for w in caught]
    assert state["pod"] is None


def test_sdk_up_treats_a_node_gone_from_the_listing_as_a_refusal_and_its_other_value_errors_as_ours(monkeypatch):
    """`Lium.up()` resolves the node from `ls()` before the POST and raises ValueError("Node with ID … not found") when
    it left the listing: that is the node refusing, so the next one is tried. "No SSH keys found" is our problem."""
    journey = _load_sdk_journey(monkeypatch, _load(monkeypatch))
    sdk = _RefusingSdk({"exec-1": ValueError("Node with ID 'exec-1' not found")})
    state = {"pod": None}
    with pytest.warns(UserWarning, match=r"rent refused: huid-1 \(exec-1\) at \$0.30/h: Node with ID 'exec-1' not found"):
        created = journey.up_first_accepting(sdk, [_sdk_node(1, 0.30), _sdk_node(2, 0.32)], "e2e-sdk-000004", state)
    assert sdk.ups == ["exec-1", "exec-2"] and created["id"] == "pod-for-exec-2"
    sdk = _RefusingSdk({"exec-1": ValueError("No node found with id exec-1")})   # `default_docker_template`'s wording
    with pytest.warns(UserWarning, match=r"No node found with id exec-1"):
        created = journey.up_first_accepting(sdk, [_sdk_node(1, 0.30), _sdk_node(2, 0.32)], "e2e-sdk-000006", {"pod": None})
    assert sdk.ups == ["exec-1", "exec-2"] and created["id"] == "pod-for-exec-2"
    sdk = _RefusingSdk({"exec-1": ValueError("No SSH keys found")})
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(ValueError, match="No SSH keys found"):
            journey.up_first_accepting(sdk, [_sdk_node(1, 0.30), _sdk_node(2, 0.32)], "e2e-sdk-000005", {})
    assert sdk.ups == ["exec-1"]


def test_sdk_up_gives_up_after_three_distinct_nodes(monkeypatch):
    from lium.sdk.exceptions import LiumError

    journey = _load_sdk_journey(monkeypatch, _load(monkeypatch))
    sdk = _RefusingSdk({f"exec-{i}": LiumError("API error 400: Can't rent node. Try again later.") for i in range(1, 5)})
    with pytest.warns(UserWarning), pytest.raises(LiumError, match=r"^3 node\(s\) refused the rent, none accepted \(max_tries=3, 1 rentable candidate\(s\) left untried\): rent refused: huid-1") as raised:
        journey.up_first_accepting(sdk, [_sdk_node(i, 0.30 + i / 100) for i in (1, 2, 3, 4)], "e2e-sdk-000001", {})
    assert sdk.ups == ["exec-1", "exec-2", "exec-3"]
    assert isinstance(raised.value.__cause__, LiumError) and "huid-4" not in str(raised.value)


def test_sdk_up_does_not_retry_what_another_node_cannot_fix(monkeypatch):
    from lium.sdk.exceptions import (
        LiumAuthError,
        LiumHostKeyError,
        LiumInsufficientBalanceError,
        LiumPermissionError,
        LiumRateLimitError,
        LiumServerError,
    )

    journey = _load_sdk_journey(monkeypatch, _load(monkeypatch))
    for err in (LiumAuthError("401"), LiumPermissionError("403"), LiumInsufficientBalanceError("no balance"), LiumServerError("502"),
                LiumRateLimitError("429"), LiumHostKeyError("host key changed")):
        sdk = _RefusingSdk({"exec-1": err})
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with pytest.raises(type(err)):
                journey.up_first_accepting(sdk, [_sdk_node(1, 0.30), _sdk_node(2, 0.32)], "e2e-sdk-000002", {})
        assert sdk.ups == ["exec-1"], type(err).__name__


def test_sdk_a_refusal_that_left_a_pod_is_recorded_for_the_fixture(monkeypatch):
    from lium.sdk.exceptions import LiumError

    journey = _load_sdk_journey(monkeypatch, _load(monkeypatch))
    sdk = _RefusingSdk({"exec-1": LiumError("API error 400: Can't rent node.")}, pods=[SimpleNamespace(id="pod-77", name="e2e-sdk-000003")])
    state = {"pod": None, "name": "e2e-sdk-000003"}
    with pytest.warns(UserWarning), pytest.raises(AssertionError, match="huid-1 refused the rent but a pod named e2e-sdk-000003 exists \\(id pod-77\\)"):
        journey.up_first_accepting(sdk, [_sdk_node(1, 0.30), _sdk_node(2, 0.32)], "e2e-sdk-000003", state)
    assert sdk.ups == ["exec-1"] and state["pod"] == {"id": "pod-77"}   # the sdk_pod fixture removes it by this id
