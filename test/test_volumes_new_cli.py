"""DAH-2902: ``lium volumes new`` takes ``--description`` as the long spelling of ``-d``/``--desc``.

The README used to advertise ``--description`` while the CLI only knew ``--desc``/``-d``
(``No such option: --description``). All three spellings reach the action with the same value.
"""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from lium.cli.volumes.new.command import volumes_new_command


@pytest.mark.parametrize("flag", ["--description", "--desc", "-d"])
def test_volumes_new_accepts_every_description_spelling(monkeypatch, flag: str) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr("lium.cli.volumes.new.command.ensure_config", lambda: None)
    monkeypatch.setattr("lium.cli.volumes.new.command.Lium", lambda: object())
    monkeypatch.setattr("lium.cli.volumes.new.command.ui.load", lambda _msg, fn: fn())

    class _Action:
        def execute(self, ctx):
            seen.update(ctx)

    monkeypatch.setattr("lium.cli.volumes.new.command.CreateVolumeAction", _Action)
    result = CliRunner().invoke(volumes_new_command, ["mydata", flag, "My dataset"])
    assert result.exit_code == 0, result.output
    assert seen["name"] == "mydata"
    assert seen["description"] == "My dataset"
