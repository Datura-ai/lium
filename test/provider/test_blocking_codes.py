from lium.cli.provider._blocking import fallback_reasons, normalise


def test_full_8x_code_and_legacy_alias_share_title():
    titles = set()
    for code in ("full_8x_without_ncu_or_split", "flagship_without_ncu_or_split"):
        (reason,) = fallback_reasons({}, [{"code": code}])
        assert reason["code"] == "full_8x_without_ncu_or_split"
        assert normalise({"code": code})["code"] == "full_8x_without_ncu_or_split"
        titles.add(reason["title"])
    assert titles == {"Idle 8× H200 / B200 / B300 node offers no NCU profiling, GPU splitting or confidential computing"}
