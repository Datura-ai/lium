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
