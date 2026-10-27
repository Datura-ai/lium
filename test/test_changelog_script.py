"""scripts/changelog.py folds changelog.d/*.md into CHANGELOG.md the way changelog.d/README.md says."""
import importlib.util
import pathlib
import subprocess
import sys

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "changelog.py"
spec = importlib.util.spec_from_file_location("changelog_script", SCRIPT)
changelog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(changelog)

RELEASED = "# Changelog\n\nintro\n\n## [0.0.37] - 2026-09-08\n\n### Added\n- old\n"


def _repo(tmp_path, changelog_text=RELEASED, fragments=None):
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / "README.md").write_text("# not a fragment\n", encoding="utf-8")
    for name, body in (fragments or {}).items():
        (tmp_path / "changelog.d" / name).write_text(body, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(changelog_text, encoding="utf-8")
    return tmp_path


def test_fold_stamps_the_release_version_into_server_json(tmp_path):
    root = _repo(tmp_path, fragments={"DAH-1.md": "### Fixed\n- one\n"})
    (root / "server.json").write_text('{"version": "0.0.1", "packages": [{"version": "0.0.1"}]}', encoding="utf-8")
    subprocess.run([sys.executable, str(SCRIPT), "--version", "0.0.38", "--root", str(root)], check=True)
    assert (root / "server.json").read_text(encoding="utf-8") == '{"version": "0.0.38", "packages": [{"version": "0.0.38"}]}'
