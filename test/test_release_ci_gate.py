"""release.yml `ci-green`: a release ships only a commit whose manual (workflow_dispatch) ci.yml run went green.

v0.41.7 (6 Nov 2026) was cut 2 min after its PR merged, before anyone had run ci.yml on the commit, and the gate
failed. The gate now starts ci.yml on the release tag when no run is green or running and waits for it. These tests
run the step's own script against a stub `gh` that answers each poll from a list, and a no-op `sleep`.
"""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

RELEASE = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
FAIL = "FAIL"  # a poll the API refuses


def gate_job() -> dict:
    return yaml.safe_load(RELEASE.read_text())["jobs"]["ci-green"]


def run_gate(
    tmp_path: Path, polls: list, dispatch_rc: int = 0
) -> tuple[int, str, list[str]]:
    """polls: one entry per `gh api` call, each a list of "<status>:<conclusion>" runs or FAIL; the last repeats."""
    (tmp_path / "polls").write_text(
        "\n".join(FAIL if p == FAIL else ",".join(p) for p in polls) + "\n"
    )
    gh = tmp_path / "gh"
    gh.write_text(
        f"""#!/bin/bash
d={tmp_path}
if [ "$1" = workflow ]; then echo "$*" >> $d/dispatched; exit {dispatch_rc}; fi
i=$(cat $d/i 2>/dev/null || echo 1); echo $((i+1)) > $d/i
n=$(wc -l < $d/polls); [ "$i" -le "$n" ] || i=$n
line=$(sed -n "${{i}}p" $d/polls)
[ "$line" = {FAIL} ] && {{ echo "HTTP 502" >&2; exit 1; }}
[ -n "$line" ] && printf '%s\\n' "$line" | tr ',' '\\n'
exit 0
"""
    )
    (tmp_path / "sleep").write_text("#!/bin/bash\nexit 0\n")
    for f in ("gh", "sleep"):
        (tmp_path / f).chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "SHA": "abc123",
        "RELEASE_TAG": "v9.9.9",
        "GITHUB_REPOSITORY": "Datura-ai/lium",
    }
    proc = subprocess.run(
        ["bash", "-c", gate_job()["steps"][0]["run"]],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    dispatched = (
        (tmp_path / "dispatched").read_text().splitlines()
        if (tmp_path / "dispatched").exists()
        else []
    )
    return proc.returncode, proc.stdout + proc.stderr, dispatched


def test_gate_can_start_ci_and_has_time_to_wait_for_it():
    job = gate_job()
    assert job["permissions"] == {"actions": "write"}
    assert job["timeout-minutes"] >= 45
    assert (
        'gh workflow run ci.yml -R "$GITHUB_REPOSITORY" --ref "$RELEASE_TAG"'
        in job["steps"][0]["run"]
    )


def test_already_green_passes_without_starting_a_run(tmp_path):
    rc, _, dispatched = run_gate(tmp_path, [["completed:failure", "completed:success"]])
    assert rc == 0 and dispatched == []


def test_no_run_yet_starts_one_on_the_tag_and_passes_when_it_goes_green(tmp_path):
    rc, _, dispatched = run_gate(
        tmp_path, [[], [], ["queued:"], ["in_progress:"], ["completed:success"]]
    )
    assert rc == 0
    assert dispatched == [
        "workflow run ci.yml -R Datura-ai/lium --ref v9.9.9"
    ]  # once, though the run showed up late


def test_the_started_run_failing_fails_the_gate_despite_an_older_failure(tmp_path):
    rc, out, dispatched = run_gate(
        tmp_path,
        [
            ["completed:failure"],
            ["completed:failure", "in_progress:"],
            ["completed:failure"] * 2,
        ],
    )
    assert rc == 1 and "finished without success" in out and len(dispatched) == 1


def test_a_run_already_in_progress_is_watched_and_its_failure_fails_the_gate(tmp_path):
    rc, out, dispatched = run_gate(tmp_path, [["in_progress:"], ["completed:failure"]])
    assert rc == 1 and "finished without success" in out
    assert (
        dispatched == []
    )  # no replacement run that could ship the commit after a red one


def test_a_run_already_in_progress_going_green_passes(tmp_path):
    rc, _, dispatched = run_gate(tmp_path, [["in_progress:"], ["completed:success"]])
    assert rc == 0 and dispatched == []


def test_a_failed_poll_changes_nothing_and_is_retried(tmp_path):
    rc, out, dispatched = run_gate(tmp_path, [FAIL, FAIL, ["completed:success"]])
    assert rc == 0 and dispatched == [] and "could not read" in out


def test_a_refused_dispatch_fails_at_once(tmp_path):
    rc, out, _ = run_gate(tmp_path, [[]], dispatch_rc=1)
    assert rc == 1 and "could not start ci.yml" in out


@pytest.mark.parametrize("polls", [[["in_progress:"]], [[]]])
def test_a_run_that_never_finishes_times_out(tmp_path, polls):
    rc, out, _ = run_gate(tmp_path, polls)
    assert rc == 1 and "did not finish in 45 min" in out
