import json
import os
from pathlib import Path

from agent.realtime.calibration_action import (
    CalibrationRunner,
    calibration_round_plan,
    latest_result_report_since,
    result_report_snapshot,
)


def record(song, *, hit=100, miss=0, fast=0, slow=0, survived=True):
    return {"song_id": song, "perfect": hit, "great": 0, "good": 0, "bad": 0,
            "miss": miss, "fast": fast, "slow": slow, "survived": survived,
            "completed": True, "confidence": 1.0}


def test_calibration_round_plan_never_falls_back_to_prepare():
    report = Path("screencap/calibration-round-test.json")
    play_params, override = calibration_round_plan(
        difficulty="Normal",
        note_speed=3.5,
        calibration_debug=True,
        formal=False,
        play_node="RealtimeLivePlayNormal",
        offset=-13,
        report_path=report,
    )
    assert override["RealtimeLiveFreeLive"]["next"] == [
        "RealtimeLiveSongSelectMarker"
    ]
    assert override["RealtimeLiveSongSelectMarker"]["next"] == [
        "RealtimeLiveDifficulty"
    ]
    assert "RandomSongSelect" not in json.dumps(override, ensure_ascii=False)
    assert "RealtimeLivePrepare" not in str(override)
    assert play_params["run_mode"] == "calibration-rehearsal"
    assert play_params["rehearsal_mode"] is True
    assert play_params["calibration_report"] == str(report)


def test_calibration_round_plan_forces_formal_mode_gate():
    report = Path("screencap/calibration-round-test.json")
    _, override = calibration_round_plan(
        difficulty="Normal",
        note_speed=3.5,
        calibration_debug=True,
        formal=True,
        play_node="RealtimeLivePlayNormal",
        offset=-13,
        report_path=report,
    )
    assert override["RealtimeLiveFormalModeGate"]["next"] == [
        "RealtimeLiveRehearsalToFormal",
        "RealtimeLiveFormalReady",
    ]


def test_calibration_accepts_repeated_song_rehearsals_then_formal():
    records = iter([record("A", slow=20), record("A"), record("A"), record("A")])
    calls = []

    def run_round(formal, offset):
        calls.append((formal, offset))
        return next(records)

    offset, rehearsals, formal = CalibrationRunner(run_round).run()
    assert len(rehearsals) == 3
    assert all(item["song_id"] == "A" for item in rehearsals)
    assert formal["song_id"] == "A"
    assert formal["passed"] is True
    assert offset == 12
    assert [formal for formal, _ in calls] == [False, False, False, True]


def test_failed_rehearsal_counts_and_adjusts_offset():
    records = iter([
        record("bad", hit=60, miss=40, slow=20),
        record("A"), record("B"), record("D"),
    ])
    offset, rehearsals, formal = CalibrationRunner(lambda *_: next(records)).run()
    assert [item["song_id"] for item in rehearsals] == ["bad", "A", "B"]
    assert rehearsals[0]["passed"] is False
    assert offset == 12
    assert formal["passed"]


def test_next_round_starts_from_the_live_adjusted_offset():
    records = iter([
        {**record("A"), "timing_offset_ms": 30},
        record("B"),
        record("C"),
        record("D"),
    ])
    calls = []

    def run_round(formal, offset):
        calls.append((formal, offset))
        return next(records)

    offset, _, _ = CalibrationRunner(run_round).run()

    assert calls[0] == (False, 0)
    assert calls[1] == (False, 15)
    assert offset == 15


def test_three_failed_rehearsals_stop_before_formal():
    calls = []
    records = iter([
        record("A", hit=60, miss=40),
        record("B", hit=60, miss=40),
        record("C", hit=60, miss=40),
    ])

    def run_round(formal, offset):
        calls.append(formal)
        return next(records)

    try:
        CalibrationRunner(run_round).run()
    except RuntimeError as exc:
        assert "三首排练全部失败" in str(exc)
    else:
        raise AssertionError("all failed rehearsals must stop calibration")
    assert calls == [False, False, False]


def test_formal_failure_returns_unaccepted_candidate():
    records = iter([record("A"), record("B"), record("C"), record("D", hit=70, miss=30)])
    _, _, formal = CalibrationRunner(lambda *_: next(records)).run()
    assert formal["passed"] is False


def test_calibration_is_bounded_when_no_three_valid_results():
    runner = CalibrationRunner(lambda *_: {"valid": False}, max_attempts=4)
    try:
        runner.run()
    except RuntimeError as exc:
        assert "三次有效排练" in str(exc)
    else:
        raise AssertionError("calibration should be bounded")


def test_invalid_result_rounds_are_retried_within_existing_budget():
    records = iter([
        {"valid": False, "song_id": "invalid-1"},
        record("A"),
        {"valid": False, "song_id": "invalid-2"},
        record("B"), record("C"), record("D"),
    ])

    _, rehearsals, formal = CalibrationRunner(
        lambda *_: next(records), max_attempts=10,
    ).run()

    assert [item["song_id"] for item in rehearsals] == ["A", "B", "C"]
    assert formal["song_id"] == "D"


def test_unknown_song_identity_still_counts_when_result_is_valid():
    missing_identity = record("discarded")
    missing_identity.pop("song_id")
    records = iter([
        record("unknown"), missing_identity, record(None), record("A"),
    ])

    _, rehearsals, formal = CalibrationRunner(
        lambda *_: next(records), max_attempts=10,
    ).run()

    assert len(rehearsals) == 3
    assert formal["song_id"] == "A"


def test_calibration_reuses_the_result_json_already_saved_by_the_play_action(tmp_path):
    before = result_report_snapshot(tmp_path)
    result = record("ignored")
    result.pop("song_id")
    result.pop("survived")
    path = tmp_path / "realtime-result-1.json"
    path.write_text(json.dumps(result), encoding="utf-8")

    loaded = latest_result_report_since(tmp_path, before, "song-A")

    assert loaded["song_id"] == "song-A"
    assert loaded["survived"] is True
    assert loaded["perfect"] == 100


def test_calibration_report_selection_is_not_broken_by_filesystem_clock_skew(tmp_path):
    before = result_report_snapshot(tmp_path)
    path = tmp_path / "realtime-result-clock-skew.json"
    path.write_text(json.dumps({
        "perfect": 90, "great": 5, "good": 0, "bad": 0, "miss": 0,
        "fast": 2, "slow": 3, "confidence": 1.0,
    }), encoding="utf-8")
    os.utime(path, (1, 1))

    loaded = latest_result_report_since(tmp_path, before, "song-clock-skew")

    assert loaded["song_id"] == "song-clock-skew"
    assert loaded["perfect"] == 90
