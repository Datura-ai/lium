from lium.cli.provider._blocking import fallback_reasons, normalise


def test_8x_capability_code_title():
    # The wire code is append-only (consumers key on it); DAH-5197 changes only the title.
    code = "flagship_without_ncu_or_split"
    (reason,) = fallback_reasons({}, [{"code": code}])
    assert reason["code"] == code
    assert normalise({"code": code})["code"] == code
    assert reason["title"] == "Idle 8× H200 / B200 / B300 / GB300 node offers no NCU profiling, GPU splitting or confidential computing"


SYSBOX_OUTDATED = {
    "code": "SYSBOX_OUTDATED",
    "kind": "advisory",
    "title": "Sysbox 0.6.6 is older than 0.7.1",
    "message": "Some renter images (those with many layers) cannot start on Sysbox 0.6.6, so those rentals fail.",
    "measured": "0.6.6",
    "required": "0.7.1 or newer",
    "fix": "When the node has no rental, re-run the installer here.",
    "fix_command": "curl -fsSL https://example.invalid/nvidia_docker_sysbox_setup.sh | sudo bash",
    "requires": ["sudo", "no_rentals"],
    "gating": False,
}


def test_an_advisory_is_a_notice_not_a_block():
    from lium.cli.provider import _blocking

    row = {"id": "node-1", "blocking_reasons": [dict(SYSBOX_OUTDATED)]}
    _blocking.attach([row])
    assert _blocking.node_reasons(row) == []
    assert _blocking.not_eligible_reasons(row) == []
    assert _blocking.blocked_count([row]) == 0
    assert _blocking.blocking_panel(row) is None
    assert row["blocking_reasons"][0]["kind"] == "advisory"  # --json keeps it
    with _blocking.console.capture() as captured:
        _blocking.print_not_eligible([row], short=False)
    printed = " ".join(captured.get().split())
    assert "Notice: Sysbox 0.6.6 is older than 0.7.1" in printed
    assert "measured 0.6.6 · required 0.7.1 or newer" in printed
    assert "Fix: When the node has no rental" in printed and "sysbox_setup.sh" in printed
    assert "idle pay" not in printed.lower().replace("idle pay and listing", "")
    with _blocking.console.capture() as captured:
        _blocking.print_not_eligible([row], short=True)
    assert "node-1: notice: Sysbox 0.6.6 is older than 0.7.1 (measured 0.6.6" in captured.get()


def test_an_unknown_future_kind_still_blocks():
    assert normalise({**SYSBOX_OUTDATED, "kind": "something_new"})["blocks"] is True
