from lium.cli.provider._blocking import fallback_reasons, normalise


def test_8x_capability_code_title():
    # The wire code is append-only (consumers key on it); DAH-5197 changes only the title.
    code = "flagship_without_ncu_or_split"
    (reason,) = fallback_reasons({}, [{"code": code}])
    assert reason["code"] == code
    assert normalise({"code": code})["code"] == code
    assert reason["title"] == "Idle 8× H200 / B200 / B300 node offers no NCU profiling, GPU splitting or confidential computing"
