"""`lium mine --branch` takes only a branch of the repository, and git gets it as one argument."""

from pathlib import Path

import click
import pytest

from lium.cli.commands import mine


def test_run_quotes_each_list_item_for_the_shell() -> None:
    out, _ = mine._run(["printf", "%s|", "a b", "c;d"])

    assert out == "a b|c;d|"


def test_a_name_that_is_not_a_branch_of_the_repository_is_refused_before_any_change(monkeypatch, tmp_path: Path) -> None:
    ran = []
    # ls-remote matches the pattern as a path suffix, so it can list a longer branch only
    monkeypatch.setattr(mine, "_run", lambda cmd, **kw: ran.append(cmd) or ("abc\trefs/heads/x/v1.0\n", ""))

    with pytest.raises(click.BadParameter):
        mine._clone_or_update_repo(tmp_path / "x", "v1.0")

    assert [cmd[1] for cmd in ran] == ["ls-remote"]


def _origin(tmp_path: Path) -> Path:
    import subprocess

    def git(*args: str, cwd: Path) -> None:
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, check=True,
                       capture_output=True)

    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-q", "-b", "main", cwd=origin)
    for n in range(3):
        (origin / "f.txt").write_text(str(n))
        git("add", "f.txt", cwd=origin)
        git("commit", "-q", "-m", f"c{n}", cwd=origin)
    return origin


def _commits(repo: Path) -> int:
    return int(mine._run(["git", "rev-list", "--count", "HEAD"], cwd=str(repo))[0].strip())


def test_the_node_repository_is_cloned_and_updated_with_the_latest_commit_only(monkeypatch, tmp_path: Path) -> None:
    # a full-history clone (~550 MB) stalled step 1 for 11-12 min on a slow link
    import subprocess

    origin = _origin(tmp_path)
    real_run = mine._run
    monkeypatch.setattr(
        mine, "_run",
        lambda cmd, **kw: real_run([origin.as_uri() if c == "https://github.com/Datura-ai/lium-io.git" else c
                                    for c in cmd] if isinstance(cmd, list) else cmd, **kw),
    )
    target = tmp_path / "node"

    mine._clone_or_update_repo(target, "main")
    assert _commits(target) == 1 and (target / "f.txt").read_text() == "2"

    (origin / "f.txt").write_text("new")
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "c3"],
                   cwd=origin, check=True)
    mine._clone_or_update_repo(target, "main")

    assert _commits(target) == 1 and (target / "f.txt").read_text() == "new"
    assert mine._run(["git", "branch", "--show-current"], cwd=str(target))[0].strip() == "main"


def test_a_full_history_clone_from_an_older_release_still_updates(monkeypatch, tmp_path: Path) -> None:
    import subprocess

    origin = _origin(tmp_path)
    target = tmp_path / "node"
    subprocess.run(["git", "clone", "-q", origin.as_uri(), str(target)], check=True)
    (origin / "f.txt").write_text("new")
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "c3"],
                   cwd=origin, check=True)
    real_run = mine._run
    monkeypatch.setattr(
        mine, "_run",
        lambda cmd, **kw: real_run([origin.as_uri() if c == "https://github.com/Datura-ai/lium-io.git" else c
                                    for c in cmd] if isinstance(cmd, list) else cmd, **kw),
    )

    mine._clone_or_update_repo(target, "main")

    assert (target / "f.txt").read_text() == "new"
