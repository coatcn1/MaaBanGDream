import json

from agent.realtime.calibration_action import CalibrationRunner, latest_result_report_after


def record(song, *, hit=100, miss=0, fast=0, slow=0, survived=True):
    return {"song_id": song, "perfect": hit, "great": 0, "good": 0, "bad": 0,
            "miss": miss, "fast": fast, "slow": slow, "survived": survived,
            "completed": True, "confidence": 1.0}


def test_calibration_collects_three_distinct_rehearsals_then_distinct_formal():
    records = iter([record("A", slow=20), record("A"), record("B"), record("C"), record("D")])
    calls = []

    def run_round(formal, offset):
        calls.append((formal, offset))
        return next(records)

    offset, rehearsals, formal = CalibrationRunner(run_round).run()
    assert [item["song_id"] for item in rehearsals] == ["A", "B", "C"]
    assert formal["song_id"] == "D"
    assert formal["passed"] is True
    assert offset == 5
    assert [formal for formal, _ in calls] == [False, False, False, False, True]


def test_failed_rehearsal_adjusts_offset_but_is_not_accepted():
    records = iter([
        record("bad", hit=60, miss=40, slow=20),
        record("A"), record("B"), record("C"), record("D"),
    ])
    offset, rehearsals, formal = CalibrationRunner(lambda *_: next(records)).run()
    assert [item["song_id"] for item in rehearsals] == ["A", "B", "C"]
    assert offset == 5
    assert formal["passed"]


def test_formal_failure_returns_unaccepted_candidate():
    records = iter([record("A"), record("B"), record("C"), record("D", hit=70, miss=30)])
    _, _, formal = CalibrationRunner(lambda *_: next(records)).run()
    assert formal["passed"] is False


def test_calibration_is_bounded_when_no_three_valid_songs():
    runner = CalibrationRunner(lambda *_: record("same"), max_attempts=4)
    try:
        runner.run()
    except RuntimeError as exc:
        assert "三首不同歌曲" in str(exc)
    else:
        raise AssertionError("calibration should be bounded")


def test_calibration_reuses_the_result_json_already_saved_by_the_play_action(tmp_path):
    result = record("ignored")
    result.pop("song_id")
    result.pop("survived")
    path = tmp_path / "realtime-result-1.json"
    path.write_text(json.dumps(result), encoding="utf-8")

    loaded = latest_result_report_after(tmp_path, 0, "song-A")

    assert loaded["song_id"] == "song-A"
    assert loaded["survived"] is True
    assert loaded["perfect"] == 100
