from pathlib import Path

from lium.__about__ import __version__
from lium.cli import cli as cli_module
from lium.cli.themed_console import ThemedConsole
from lium.cli.utils import dominates


def test_get_version_falls_back_to_module_version(monkeypatch):
    def raise_missing(_distribution_name: str):
        raise cli_module.PackageNotFoundError

    monkeypatch.setattr(cli_module, "version", raise_missing)
    monkeypatch.delenv("LIUM_BUILD_VERSION", raising=False)

    assert cli_module.get_version() == __version__


def test_get_version_prefers_build_override(monkeypatch):
    def raise_missing(_distribution_name: str):
        raise cli_module.PackageNotFoundError

    monkeypatch.setattr(cli_module, "version", raise_missing)
    monkeypatch.setenv("LIUM_BUILD_VERSION", "9.9.9-binary")

    assert cli_module.get_version() == "9.9.9-binary"


def test_themed_console_loads_themes_from_meipass(monkeypatch, tmp_path):
    extracted_root = tmp_path / "bundle"
    themes_dir = extracted_root / "lium" / "cli"
    themes_dir.mkdir(parents=True)
    themes_file = themes_dir / "themes.json"
    themes_file.write_text(
        '{"dark":{"success":"green","error":"red","warning":"yellow","info":"cyan","pending":"cyan","id":"dim","dim":"dim"},'
        '"light":{"success":"green","error":"red","warning":"yellow","info":"blue","pending":"blue","id":"dim","dim":"dim"}}',
        encoding="utf-8",
    )

    monkeypatch.setattr("lium.cli.themed_console.__file__", str(tmp_path / "missing.py"))
    monkeypatch.setattr("lium.cli.themed_console.config.get", lambda *args, **kwargs: "dark")
    monkeypatch.setattr("sys._MEIPASS", str(extracted_root), raising=False)

    console = ThemedConsole()

    assert console.themes["dark"]["success"] == "green"
    assert Path(str(extracted_root)).exists()


def test_dominates_handles_missing_price_without_unboundlocalerror():
    better = {"price_per_gpu_hour": None, "total_bandwidth": 100, "location_score": 1.0, "net_down": 50, "net_up": 50}
    worse = {"price_per_gpu_hour": 2.0, "total_bandwidth": 10, "location_score": 0.0, "net_down": 5, "net_up": 5}

    assert isinstance(dominates(better, worse), bool)
