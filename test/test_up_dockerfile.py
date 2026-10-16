"""Tests for custom-Dockerfile pod deployment via `lium up --dockerfile`.

Covers the three surfaces the feature touches:
  * SDK  — `Lium.up()` payload shape (dockerfile vs template) + the XOR guard.
  * CLI  — `--dockerfile` validation (mutually exclusive with --image/--template_id)
           and the file-read → SDK wiring.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli import up as up_pkg  # noqa: F401  (ensures package import works)
from lium.cli.up import command as up_command


def test_up_command_refuses_copy_of_local_files_before_renting(monkeypatch, tmp_path):
    # Arrange: a backend without build-context uploads builds with the Dockerfile as the
    # only file, so COPY of a file next to it would fail there after the rent.
    (tmp_path / "app.py").write_text("print('hi')\n")
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.12-slim\nCOPY app.py /app/app.py\nCMD [\"python\", \"/app/app.py\"]\n")
    monkeypatch.setattr(up_command, "ensure_config", lambda: None)
    lium = SimpleNamespace(
        supports=lambda feature: False,
        upload_build_context=lambda archive, timeout: pytest.fail("uploaded"),
    )
    monkeypatch.setattr(up_command, "Lium", lambda **kwargs: lium)
    monkeypatch.setattr(up_command, "ResolveExecutorAction", lambda: pytest.fail("rent path reached"))

    # Act
    result = CliRunner().invoke(up_command.up_command, ["brave-fox-3a", "--dockerfile", str(dockerfile)])

    # Assert
    assert result.exit_code == 2
    assert "COPY app.py /app/app.py" in result.output
    assert "--image" in result.output
