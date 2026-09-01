import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent.realtime.calibration_action as calibration_action_module
from agent.realtime.calibration_action import (
    CalibrationRunner,
    RealtimeCalibration,
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
        diagnostic_trace=True,
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
    assert play_params["diagnostic_trace"] is True


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


def test_calibration_round_plan_random_preserves_filter_and_excludes_used_songs():
    _, override = calibration_round_plan(
        difficulty="Hard",
        note_speed=5.0,
        calibration_debug=False,
        formal=False,
        play_node="RealtimeLivePlayHard",
        offset=0,
        report_path=Path("screencap/calibration-round-test.json"),
        song_mode="random",
        excluded_song_ids=["song-a", "song-b"],
    )
    assert override["RealtimeLiveSongSelectMarker"]["next"] == [
        "RealtimeLiveRandomSong",
    ]
    params = override["RealtimeLiveRandomSong"]["custom_action_param"]
    assert params["preserve_filter"] is True
    assert params["excluded_song_ids"] == ["song-a", "song-b"]


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


def test_low_score_rehearsal_still_counts_and_adjusts_offset():
    records = iter([
        record("bad", hit=60, miss=40, slow=20),
        record("A"), record("B"), record("D"),
    ])
    offset, rehearsals, formal = CalibrationRunner(lambda *_: next(records)).run()
    assert [item["song_id"] for item in rehearsals] == ["bad", "A", "B"]
    assert rehearsals[0]["passed"] is True
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


def test_three_low_score_rehearsals_still_reach_single_formal():
    calls = []
    records = iter([
        record("A", hit=60, miss=40),
        record("B", hit=60, miss=40),
        record("C", hit=60, miss=40),
        record("D"),
    ])

    def run_round(formal, offset):
        calls.append(formal)
        return next(records)

    _, rehearsals, formal = CalibrationRunner(run_round).run()
    assert len(rehearsals) == 3
    assert formal["passed"] is True
    assert calls == [False, False, False, True]


def test_formal_failure_returns_unaccepted_candidate():
    records = iter([record("A"), record("B"), record("C"), record("D", hit=70, miss=30)])
    _, _, formal = CalibrationRunner(lambda *_: next(records)).run()
    assert formal["passed"] is False


def test_invalid_rehearsal_ends_invocation_without_automatic_retry():
    calls = []

    def run_round(formal, offset):
        calls.append((formal, offset))
        return {"valid": False, "completed": False}

    runner = CalibrationRunner(run_round)
    try:
        runner.run()
    except RuntimeError as exc:
        assert "排练1" in str(exc)
    else:
        raise AssertionError("invalid result must end this invocation")
    assert calls == [(False, 0)]


def test_invalid_formal_is_not_retried_in_same_invocation():
    records = iter([
        record("A"),
        record("B"), record("C"),
        {"valid": False, "completed": False, "song_id": "invalid-formal"},
    ])
    calls = []

    def run_round(formal, offset):
        calls.append(formal)
        return next(records)

    with pytest.raises(RuntimeError, match="正式验证"):
        CalibrationRunner(run_round).run()
    assert calls == [False, False, False, True]


def test_unknown_song_identity_still_counts_when_result_is_valid():
    missing_identity = record("discarded")
    missing_identity.pop("song_id")
    records = iter([
        record("unknown"), missing_identity, record(None), record("A"),
    ])

    _, rehearsals, formal = CalibrationRunner(lambda *_: next(records)).run()

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


def test_user_stop_is_neutral_success_for_calibration(monkeypatch):
    action = RealtimeCalibration()

    def stopped(*_args, **_kwargs):
        raise InterruptedError("校准已停止")

    monkeypatch.setattr(action, "_run", stopped)
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=True))
    argv = SimpleNamespace(custom_action_param="{}")

    assert action.run(context, argv) is True


def test_user_stop_after_nested_round_is_neutral_before_result_lookup(
    monkeypatch, capsys, tmp_path,
):
    class Job:
        def wait(self):
            return self

        def get(self):
            return object()

    controller = SimpleNamespace(post_screencap=lambda: Job())
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=controller),
    )

    def run_task(*_args, **_kwargs):
        context.tasker.stopping = True
        return SimpleNamespace(status=SimpleNamespace(succeeded=True))

    context.run_task = run_task
    monkeypatch.setattr(
        calibration_action_module, "calibration_difficulty", lambda: "Expert",
    )
    monkeypatch.setattr(calibration_action_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(calibration_action_module, "debug_enabled", lambda: False)
    monkeypatch.setattr(calibration_action_module, "calibration_song_mode", lambda: "current")
    monkeypatch.setattr(calibration_action_module, "calibration_resume_mode", lambda: "auto")
    monkeypatch.setattr(calibration_action_module, "frame_resolution", lambda _image: (1280, 720))
    monkeypatch.setattr(
        calibration_action_module,
        "verified_game_visual_settings",
        lambda: SimpleNamespace(
            note_skin_type=1,
            tap_effect=1,
            judgement_assist_effect=True,
        ),
    )
    monkeypatch.setattr(
        calibration_action_module, "result_report_snapshot", lambda _root: set(),
    )
    argv = SimpleNamespace(custom_action_param="{}")

    assert RealtimeCalibration().run(context, argv) is True
    output = capsys.readouterr().out
    assert "RealtimeCalibration stopped=true" in output
    assert "RealtimeCalibration failed=" not in output
