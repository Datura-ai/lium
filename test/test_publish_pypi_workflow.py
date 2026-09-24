"""PyPI gets only code that is on main (RELEASING.md "Who can publish").

publish-pypi.yml is the one upload path for `lium.io`: it runs from main's copy (workflow_run), checks that the release
tag is on main, and holds the upload token in a job that checks nothing out. The stubs run by hand in the main-only
`pypi` environment. No reviewer gate or guard step is needed on top of that.
"""

import subprocess
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PUBLISH = WORKFLOWS / "publish-pypi.yml"
RELEASE = WORKFLOWS / "release.yml"
STUBS = [WORKFLOWS / "release-lium-alias.yml", WORKFLOWS / "release-deprecate-lium-cli.yml"]
PYPI_ACTION = "pypa/gh-action-pypi-publish"
TAG_CHECK_STEP = "Release tag must point at this commit, and the commit must be on main"


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def triggers(workflow: dict) -> dict:
    # PyYAML reads the bare key `on` as True
    return workflow.get("on", workflow.get(True))


def steps_using(job: dict, action: str) -> list[dict]:
    return [s for s in job.get("steps", []) if str(s.get("uses", "")).startswith(action)]


def jobs_uploading(workflow: dict) -> dict:
    return {name: job for name, job in workflow["jobs"].items() if steps_using(job, PYPI_ACTION)}


def environment_name(job: dict) -> str | None:
    env = job.get("environment")
    return env.get("name") if isinstance(env, dict) else env


def test_publish_runs_main_copy_after_a_release_build():
    workflow = load(PUBLISH)
    on = triggers(workflow)
    assert set(on) == {"workflow_run"}
    assert on["workflow_run"]["workflows"] == [load(RELEASE)["name"]]
    assert on["workflow_run"]["types"] == ["completed"]
    assert workflow["permissions"] == {}
    build = workflow["jobs"]["build"]
    assert "github.event.workflow_run.event == 'release'" in build["if"]
    assert "github.event.workflow_run.conclusion == 'success'" in build["if"]


def test_only_the_publish_job_holds_the_upload_token():
    jobs = load(PUBLISH)["jobs"]
    assert set(jobs_uploading({"jobs": jobs})) == {"publish"}
    publish = jobs["publish"]
    assert publish["needs"] == "build"
    assert environment_name(publish) == "pypi"
    assert publish["permissions"] == {"id-token": "write"}
    assert not steps_using(publish, "actions/checkout")
    assert all("run" not in step for step in publish["steps"])
    assert "id-token" not in jobs["build"]["permissions"]


def test_release_workflow_no_longer_uploads_to_pypi():
    workflow = load(RELEASE)
    assert not jobs_uploading(workflow)
    for job in workflow["jobs"].values():
        assert (job.get("permissions") or {}).get("id-token") != "write"
        assert environment_name(job) != "pypi"


@pytest.mark.parametrize("path", STUBS, ids=lambda p: p.name)
def test_stubs_run_by_hand_in_the_pypi_environment_without_a_guard(path):
    workflow = load(path)
    assert set(triggers(workflow)) == {"workflow_dispatch"}
    (job,) = workflow["jobs"].values()
    assert environment_name(job) == "pypi"
    assert "actions" not in job["permissions"]
    assert [s.get("name") for s in job["steps"] if "guard" in str(s.get("name", "")).lower()] == []


@pytest.mark.parametrize("path", [PUBLISH, RELEASE, *STUBS, REPO_ROOT / "RELEASING.md"], ids=lambda p: p.name)
def test_no_dated_paragraphs_or_account_ids(path):
    text = path.read_text()
    assert "Today (" not in text
    assert "surcyf123" not in text


def tag_check_script() -> str:
    (step,) = [s for s in load(PUBLISH)["jobs"]["build"]["steps"] if s.get("name") == TAG_CHECK_STEP]
    return step["run"]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def checkout(tmp_path):
    """A clone of an origin whose main is base -> `on_main` (v1.0.0) -> `merge` (v1.0.3), a --no-ff merge of `merged_in`
    (v1.0.2, an ancestor of main but not on its first-parent history), plus a side branch at `off_main` (v1.0.1)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.email", "t@example.com")
    git(origin, "config", "user.name", "t")
    git(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
    git(origin, "commit", "-q", "--allow-empty", "-m", "base")
    git(origin, "commit", "-q", "--allow-empty", "-m", "on main")
    git(origin, "tag", "v1.0.0")
    shas = {"on_main": git(origin, "rev-parse", "HEAD")}
    git(origin, "checkout", "-q", "-b", "side", "HEAD~1")
    git(origin, "commit", "-q", "--allow-empty", "-m", "off main")
    git(origin, "tag", "v1.0.1")
    git(origin, "tag", "later")
    shas["off_main"] = git(origin, "rev-parse", "HEAD")
    git(origin, "checkout", "-q", "-b", "feature", "main")
    git(origin, "commit", "-q", "--allow-empty", "-m", "merged in")
    git(origin, "tag", "v1.0.2")
    shas["merged_in"] = git(origin, "rev-parse", "HEAD")
    git(origin, "checkout", "-q", "main")
    git(origin, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    git(origin, "tag", "v1.0.3")
    shas["merge"] = git(origin, "rev-parse", "HEAD")
    git(origin, "branch", "-q", "-D", "side", "feature")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--no-tags", str(origin), str(clone))
    return clone, shas


def run_tag_check(clone: Path, tag: str, sha: str) -> subprocess.CompletedProcess[str]:
    # the job runs the step after actions/checkout has put the clone on the release commit
    git(clone, "fetch", "-q", "origin", sha)
    git(clone, "checkout", "-q", "--detach", sha)
    return subprocess.run(
        ["bash", "-e", "-c", tag_check_script()],
        cwd=clone,
        env={"PATH": "/usr/bin:/bin", "HOME": str(clone), "RELEASE_TAG": tag, "RELEASE_SHA": sha},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("tag, which", [("v1.0.0", "on_main"), ("v1.0.3", "merge")])
def test_tag_check_passes_a_release_tag_on_mains_first_parent_history(checkout, tag, which):
    clone, shas = checkout
    result = run_tag_check(clone, tag, shas[which])
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "tag, which, message",
    [
        ("v1.0.1", "off_main", "is not on main's first-parent history"),
        ("v1.0.2", "merged_in", "is not on main's first-parent history"),
        ("v1.0.0", "off_main", "points at"),
        ("later", "off_main", "not a release tag"),
    ],
)
def test_tag_check_refuses(checkout, tag, which, message):
    clone, shas = checkout
    result = run_tag_check(clone, tag, shas[which])
    assert result.returncode != 0
    assert message in result.stderr
