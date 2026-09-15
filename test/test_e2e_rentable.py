"""`e2e/conftest.py`'s `rentable()` — the guard that picks the node the live suite rents (DAH-3151).

The e2e suite only ever calls it on live listings, so the unit suite carries the negative controls: a node in an
excluded country, an excluded executor (by id or by huid), no GPU, over the price cap — and the accept case.
"""

import importlib.util
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

CONFTEST = Path(__file__).resolve().parent.parent / "e2e" / "conftest.py"


def _load(monkeypatch, **env):
    """A fresh import of e2e/conftest.py with the given E2E_* variables (it reads them at import)."""
    for name in ("E2E_EXCLUDE_COUNTRIES", "E2E_EXCLUDE_EXECUTORS", "E2E_MAX_PRICE", "E2E_KEEP_POD"):
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
