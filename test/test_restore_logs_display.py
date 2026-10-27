from types import SimpleNamespace

from lium.cli.bk.restore_logs.display import format_single_restore


def test_scanning_restore_shows_label_and_discovered_files():
    log = SimpleNamespace(
        status="IN_PROGRESS",
        stage="SCANNING",
        total_files=1200,
        created_at="2026-10-27T00:00:00Z",
    )

    text = format_single_restore("pod-a", log)

    assert "Stage: Scanning the backup" in text
    assert "Work: 1,200 files discovered" in text
