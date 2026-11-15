"""The miner repo clone passes --branch and the target dir as single arguments, and refuses a non-ref branch."""

from pathlib import Path

import click
import pytest

from lium.cli.commands import mine


def test_the_clone_sends_branch_and_dir_as_single_quoted_arguments(monkeypatch, tmp_path: Path) -> None:
    ran = []
    monkeypatch.setattr(mine, "_run", lambda cmd, **kw: ran.append(cmd) or ("", ""))
    target = tmp_path / "dir with space"

    mine._clone_or_update_repo(target, "release/v1.2")

    assert ran == [["git", "clone", "--branch", "release/v1.2", "https://github.com/Datura-ai/lium-io.git", str(target)]]


@pytest.mark.parametrize("branch", ["main;id", "-x", "a b", "$(x)", ".", "..", "a/../b", "a/.b", "a.lock", "a/", "a//b", "a."])
def test_a_branch_that_is_not_a_ref_name_is_refused(monkeypatch, tmp_path: Path, branch: str) -> None:
    monkeypatch.setattr(mine, "_run", lambda *a, **kw: pytest.fail("ran a command"))

    with pytest.raises(click.BadParameter):
        mine._clone_or_update_repo(tmp_path / "x", branch)


def test_run_quotes_each_list_item_for_the_shell() -> None:
    out, _ = mine._run(["printf", "%s|", "a b", "c;d"])

    assert out == "a b|c;d|"


def test_the_executor_install_script_path_with_a_space_is_one_argument(monkeypatch, tmp_path: Path) -> None:
    ran = []
    monkeypatch.setattr(mine, "_run", lambda cmd, **kw: ran.append(cmd) or ("", ""))
    compute_dir = tmp_path / "node dir"
    (compute_dir / "scripts").mkdir(parents=True)
    (compute_dir / "scripts" / "install_executor_on_ubuntu.sh").touch()

    mine._install_executor_tools(compute_dir)

    assert ran == [["bash", str(compute_dir / "scripts" / "install_executor_on_ubuntu.sh")]]


def test_an_existing_checkout_is_updated_with_switch_so_the_branch_is_never_a_pathspec(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    ran = []
    monkeypatch.setattr(mine, "_run", lambda cmd, **kw: ran.append(cmd) or ("", ""))

    mine._clone_or_update_repo(tmp_path, "main")

    assert ["git", "switch", "main"] in ran
    assert not any(isinstance(cmd, list) and "checkout" in cmd for cmd in ran)
