from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from agent.realtime.engine import EngineStats
from agent.realtime import profile_play_action
from agent.realtime.profile_play_action import (
    RealtimeLifeSafetyAbortCheck,
    RealtimeProfilePlay,
    ResultCollectionOutcome,
    ResultCollectionStatus,
    _dismiss_reward_popup,
    _result_report_payload,
    _write_calibration_report,
    collect_result,
    pause_overlay_changed,
    resolve_life_policy,
)
from agent.realtime.result_parser import LiveResult
from agent.realtime.performance_settings_action import clear_verified_settings
from agent.realtime.live_session import reset_live_run, update_live_run


class Job:
    def wait(self):
        return self

    def get(self):
        return np.zeros((720, 1280, 3), dtype=np.uint8)


class Controller:
    def post_screencap(self):
        return Job()


class Tasker:
    stopping = False

    def __init__(self):
        self.controller_reads = 0
        self._controller = Controller()

    @property
    def controller(self):
        self.controller_reads += 1
        if self.controller_reads > 1:
            raise RuntimeError("controller proxy retrieved twice")
        return self._controller


def test_profile_play_reuses_one_agent_controller_proxy(monkeypatch):
    tasker = Tasker()
    context = SimpleNamespace(tasker=tasker)
    settings = SimpleNamespace(
        target_fps=60,
        timing_offset_ms=0,
        profile_path=SimpleNamespace(name="easy.json"),
    )

    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.resolve_latest",
        lambda *args, **kwargs: settings,
    )
    foreground_checks = []
    dispatcher_options = []
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.require_game_foreground",
        lambda controller: foreground_checks.append(controller),
    )

    class Dispatcher:
        def __init__(self, controller, stopping, **kwargs):
            dispatcher_options.append(kwargs)

    monkeypatch.setattr(
        "agent.realtime.profile_play_action.ControllerTouchDispatcher",
        Dispatcher,
    )

    engine_options = []

    class Engine:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, capture, stopping, **kwargs):
            engine_options.append(kwargs)
            capture()
            return EngineStats(1, 0, False)

    monkeypatch.setattr("agent.realtime.profile_play_action.RealtimeEngine", Engine)

    argv = SimpleNamespace(custom_action_param=json.dumps({"difficulty": "Easy"}))
    assert RealtimeProfilePlay()._run(context, argv)
    assert tasker.controller_reads == 1
    assert foreground_checks == [tasker._controller]
    assert dispatcher_options == [{}]
    assert engine_options[0]["startup_timeout_seconds"] == 60.0


def test_profile_play_refuses_pipeline_start_without_fresh_speed_gate(
    monkeypatch, tmp_path,
):
    clear_verified_settings()
    reset_live_run(
        mode="pending",
        difficulty="Easy",
        prepared_for_play=True,
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=Controller()),
    )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Easy",
        "require_profile": False,
        "settings_gate_required": True,
    }))

    with pytest.raises(RuntimeError, match="尚未实际验证游戏流速"):
        RealtimeProfilePlay()._run(context, argv)

    report = next((tmp_path / "screencap").glob("realtime-result-*.json"))
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["valid"] is False
    assert payload["result_status"] == "preflight_error"
    assert payload["terminal_stage"] == "profile_play_preflight"


def test_profile_play_stop_during_preflight_is_neutral_and_writes_nothing(
    monkeypatch, tmp_path,
):
    reset_live_run(
        mode="pending",
        difficulty="Easy",
        prepared_for_play=True,
    )
    tasker = Tasker()
    context = SimpleNamespace(tasker=tasker)

    def stop_while_reading_settings(_difficulty):
        tasker.stopping = True
        raise InterruptedError("settings read cancelled")

    monkeypatch.setattr(
        "agent.realtime.profile_play_action.verified_settings",
        stop_while_reading_settings,
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    recorder_constructions = []
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeDebugRecorder",
        lambda root: recorder_constructions.append(root),
    )
    failure_reasons = []
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.record_failure_reason",
        failure_reasons.append,
    )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Easy",
        "settings_gate_required": True,
        "debug_recording": True,
    }))

    assert RealtimeProfilePlay().run(context, argv) is True
    assert recorder_constructions == []
    assert failure_reasons == []
    assert not list(tmp_path.rglob("realtime-result-*.json"))


def test_pause_overlay_requires_a_material_screen_change():
    before = np.zeros((720, 1280, 3), dtype=np.uint8)
    unchanged = before.copy()
    overlay = before.copy()
    overlay[90:630, 160:1120] = 80

    assert not pause_overlay_changed(before, unchanged)
    assert pause_overlay_changed(before, overlay)


def test_life_safety_abort_gate_only_matches_protected_abort(monkeypatch):
    context = SimpleNamespace()
    argv = SimpleNamespace(custom_action_param="{}")
    monkeypatch.setattr(profile_play_action, "_LAST_LIFE_SAFETY_ABORT", False)
    assert not RealtimeLifeSafetyAbortCheck().run(context, argv)
    monkeypatch.setattr(profile_play_action, "_LAST_LIFE_SAFETY_ABORT", True)
    assert RealtimeLifeSafetyAbortCheck().run(context, argv)


def test_rehearsal_life_policy_can_ignore_depletion():
    policy = resolve_life_policy(
        {"require_profile": False, "rehearsal_mode": True},
        {
            "life_safety_enabled": True,
            "life_exit_threshold": 200,
            "rehearsal_ignore_life_safety": True,
        },
    )

    assert policy == (True, True, None)


def test_rehearsal_life_policy_can_enable_normal_protection():
    policy = resolve_life_policy(
        {"require_profile": False, "rehearsal_mode": True},
        {
            "life_safety_enabled": True,
            "life_exit_threshold": 200,
            "rehearsal_ignore_life_safety": False,
        },
    )

    assert policy == (True, False, 200)


def test_formal_calibration_round_uses_life_protection():
    policy = resolve_life_policy(
        {"require_profile": False, "rehearsal_mode": False},
        {
            "life_safety_enabled": True,
            "life_exit_threshold": 200,
            "rehearsal_ignore_life_safety": True,
        },
    )

    assert policy == (False, False, 200)


def test_calibration_report_contains_replay_diagnostics(tmp_path):
    report = tmp_path / "round.json"
    stats = EngineStats(
        100,
        20,
        False,
        completed=True,
        timing_feedback_fast=2,
        timing_feedback_slow=7,
        initial_timing_offset_ms=3,
        final_timing_offset_ms=5,
        timing_feedback_valid=9,
        timing_feedback_ignored=4,
        timing_feedback_ignored_reasons={"active_hold": 4},
        filtered_adjacent_artifacts=7,
        rejected_hold_candidates=2,
    )

    _write_calibration_report(
        report,
        result=LiveResult(90, 5, 2, 1, 2, 2, 7),
        stats=stats,
        timing_offset_ms=5,
        song_id="song-a",
    )

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["initial_timing_offset_ms"] == 3
    assert payload["timing_offset_ms"] == 5
    assert payload["realtime_feedback_ignored_reasons"] == {"active_hold": 4}
    assert payload["filtered_adjacent_artifacts"] == 7
    assert payload["rejected_hold_candidates"] == 2


def test_result_report_contains_runtime_acceptance_metrics():
    stats = EngineStats(
        120,
        42,
        False,
        completed=True,
        action_counts={"tap": 31, "flick": 4, "down": 7},
        frame_interval_p50_ms=16.4,
        frame_interval_p95_ms=18.2,
        frame_interval_max_ms=24.0,
        effective_fps=59.1,
        terminal_reason="completed",
        initial_timing_offset_ms=-11,
        final_timing_offset_ms=-13,
    )

    payload = _result_report_payload(
        LiveResult(100, 10, 2, 1, 2, 3, 4),
        stats,
        timing_offset_ms=-11,
        suggested_timing_offset_ms=-14,
    )

    assert payload["miss"] == 2
    assert payload["processed_frames"] == 120
    assert payload["action_counts"] == {"tap": 31, "flick": 4, "down": 7}
    assert payload["frame_interval_p95_ms"] == pytest.approx(18.2)
    assert payload["effective_fps"] == pytest.approx(59.1)
    assert payload["terminal_reason"] == "completed"


@pytest.mark.parametrize(
    ("run_mode", "expected_success", "records_failure"),
    [
        ("formal", False, True),
        ("calibration-rehearsal", True, False),
    ],
)
def test_incomplete_round_records_structured_result_and_calibration_can_retry(
    monkeypatch,
    tmp_path,
    run_mode,
    expected_success,
    records_failure,
):
    tasker = Tasker()
    context = SimpleNamespace(tasker=tasker)
    settings = SimpleNamespace(
        target_fps=60,
        timing_offset_ms=0,
        profile_path=SimpleNamespace(name="normal.json"),
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.resolve_latest",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.require_game_foreground",
        lambda _controller: None,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.ControllerTouchDispatcher",
        lambda *_args, **_kwargs: object(),
    )

    class Engine:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, _capture, _stopping, **_kwargs):
            return EngineStats(
                100,
                20,
                False,
                completed=False,
                terminal_reason="演奏超过安全时限 600 秒，仍未识别到结算画面",
            )

    monkeypatch.setattr("agent.realtime.profile_play_action.RealtimeEngine", Engine)
    reasons = []
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.record_failure_reason",
        reasons.append,
    )

    params = {
        "difficulty": "Normal",
        "duration_seconds": 600,
        "require_completion": True,
        "wait_for_completion": True,
        "save_result_frame": True,
        "run_mode": run_mode,
    }
    if run_mode.startswith("calibration-"):
        params["calibration_report"] = "screencap/calibration-retry.json"
    argv = SimpleNamespace(custom_action_param=json.dumps(params))

    assert RealtimeProfilePlay()._run(context, argv) is expected_success
    assert reasons == (
        ["演奏超过安全时限 600 秒，仍未识别到结算画面"]
        if records_failure else []
    )
    reports = list((tmp_path / "screencap").glob("realtime-result-*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["valid"] is False
    assert payload["result_status"] == "engine_incomplete"
    assert payload["terminal_reason"] == (
        "演奏超过安全时限 600 秒，仍未识别到结算画面"
    )
    assert payload["mode"] == run_mode
    if run_mode.startswith("calibration-"):
        calibration = json.loads(
            (tmp_path / "screencap" / "calibration-retry.json").read_text(
                encoding="utf-8"
            )
        )
        assert calibration["valid"] is False
        assert calibration["mode"] == run_mode


def test_life_depleted_calibration_formal_round_can_retry(monkeypatch, tmp_path):
    reset_live_run(mode="calibration", difficulty="Hard")
    tasker = Tasker()
    context = SimpleNamespace(tasker=tasker)
    settings = SimpleNamespace(
        target_fps=60,
        timing_offset_ms=0,
        profile_path=SimpleNamespace(name="hard.json"),
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.resolve_latest",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.require_game_foreground",
        lambda _controller: None,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.ControllerTouchDispatcher",
        lambda *_args, **_kwargs: object(),
    )

    class Engine:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, _capture, _stopping, **_kwargs):
            return EngineStats(
                3438,
                391,
                False,
                aborted_for_life=True,
                life_depleted=True,
                completed=False,
                terminal_reason="生命值触发安全停止",
            )

    monkeypatch.setattr("agent.realtime.profile_play_action.RealtimeEngine", Engine)
    reasons = []
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.record_failure_reason",
        reasons.append,
    )
    params = {
        "difficulty": "Hard",
        "duration_seconds": 600,
        "require_completion": True,
        "wait_for_completion": True,
        "save_result_frame": True,
        "run_mode": "calibration-formal",
        "calibration_report": "screencap/calibration-life-retry.json",
    }
    argv = SimpleNamespace(custom_action_param=json.dumps(params))

    assert RealtimeProfilePlay()._run(context, argv) is True
    assert reasons == []
    calibration = json.loads(
        (tmp_path / "screencap" / "calibration-life-retry.json").read_text(
            encoding="utf-8"
        )
    )
    assert calibration["valid"] is False
    assert calibration["survived"] is False
    assert calibration["completed"] is False
    assert calibration["mode"] == "calibration-formal"


def test_engine_error_writes_invalid_result_with_partial_stats(monkeypatch, tmp_path):
    reset_live_run(mode="formal", difficulty="Normal")
    tasker = Tasker()
    context = SimpleNamespace(tasker=tasker)
    settings = SimpleNamespace(
        target_fps=60,
        timing_offset_ms=-9,
        profile_path=SimpleNamespace(name="normal.json"),
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.resolve_latest",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.require_game_foreground",
        lambda _controller: None,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.ControllerTouchDispatcher",
        lambda *_args, **_kwargs: object(),
    )

    class Engine:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, _capture, _stopping, **_kwargs):
            error = RuntimeError("detector exploded")
            error.realtime_stats = EngineStats(
                17,
                6,
                False,
                terminal_reason=(
                    "实时演奏引擎异常: RuntimeError: detector exploded"
                ),
                action_counts={"tap": 6},
                frame_interval_p50_ms=16.7,
                frame_interval_p95_ms=22.5,
                frame_interval_max_ms=41.0,
                effective_fps=57.3,
            )
            raise error

    monkeypatch.setattr("agent.realtime.profile_play_action.RealtimeEngine", Engine)
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Normal",
        "duration_seconds": 600,
        "require_completion": True,
        "save_result_frame": True,
        "run_mode": "formal",
    }))

    assert not RealtimeProfilePlay().run(context, argv)
    reports = list((tmp_path / "screencap").glob("realtime-result-*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["valid"] is False
    assert payload["result_status"] == "engine_error"
    assert payload["processed_frames"] == 17
    assert payload["dispatched_actions"] == 6
    assert payload["action_counts"] == {"tap": 6}
    assert payload["frame_interval_p95_ms"] == pytest.approx(22.5)
    assert payload["reason"] == payload["terminal_reason"]
    assert payload["run_id"] == payload["session"]["run_id"]


def test_engine_interrupt_after_stop_writes_neutral_partial_result(
    monkeypatch, tmp_path,
):
    reset_live_run(mode="formal", difficulty="Normal")
    tasker = Tasker()
    context = SimpleNamespace(tasker=tasker)
    settings = SimpleNamespace(
        target_fps=60,
        timing_offset_ms=-9,
        profile_path=SimpleNamespace(name="normal.json"),
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.resolve_latest",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.require_game_foreground",
        lambda _controller: None,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.ControllerTouchDispatcher",
        lambda *_args, **_kwargs: object(),
    )

    class Engine:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, _capture, _stopping, **_kwargs):
            tasker.stopping = True
            error = InterruptedError("stop observed during dispatch")
            error.realtime_stats = EngineStats(
                17,
                6,
                False,
                terminal_reason=(
                    "实时演奏引擎异常: InterruptedError: "
                    "stop observed during dispatch"
                ),
                action_counts={"tap": 6},
                frame_interval_p50_ms=16.7,
                frame_interval_p95_ms=22.5,
                frame_interval_max_ms=41.0,
                effective_fps=57.3,
            )
            raise error

    monkeypatch.setattr("agent.realtime.profile_play_action.RealtimeEngine", Engine)
    failure_reasons = []
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.record_failure_reason",
        failure_reasons.append,
    )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Normal",
        "duration_seconds": 600,
        "require_completion": True,
        "save_result_frame": True,
        "run_mode": "formal",
    }))

    assert RealtimeProfilePlay().run(context, argv) is True
    reports = list((tmp_path / "screencap").glob("realtime-result-*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["valid"] is False
    assert payload["result_status"] == "stopped"
    assert payload["processed_frames"] == 17
    assert payload["dispatched_actions"] == 6
    assert payload["terminal_reason"] == "用户已停止任务"
    assert payload["reason"] == "用户已停止任务"
    assert failure_reasons == []


def test_profile_resolution_failure_writes_correlated_preflight_result(
    monkeypatch, tmp_path,
):
    live_run = reset_live_run(
        mode="pending",
        difficulty="Normal",
        prepared_for_play=True,
    )
    update_live_run(
        song_id="song-phash-v1-profile-preflight",
        song_id_method="song-phash-v1",
    )
    tasker = Tasker()
    context = SimpleNamespace(tasker=tasker)
    verified = SimpleNamespace(
        difficulty="Normal",
        actual_note_speed=3.5,
        expected_note_speed=3.5,
        profile="normal.json",
        verified_at=1.0,
    )
    visual = SimpleNamespace(
        note_skin_type=7,
        tap_effect=5,
        judgement_assist_effect=False,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.verified_settings",
        lambda _difficulty: verified,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.verified_game_visual_settings",
        lambda: visual,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.resolve_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("profile mismatch")
        ),
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    recorder_constructions = []
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeDebugRecorder",
        lambda root: recorder_constructions.append(root),
    )
    failure_reasons = []
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.record_failure_reason",
        failure_reasons.append,
    )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Normal",
        "require_profile": True,
        "settings_gate_required": True,
        "debug_recording": True,
        "run_mode": "formal",
    }))

    assert RealtimeProfilePlay().run(context, argv) is False
    assert recorder_constructions == []
    reports = list((tmp_path / "screencap").glob("realtime-result-*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["valid"] is False
    assert payload["result_status"] == "preflight_error"
    assert payload["terminal_stage"] == "profile_play_preflight"
    assert payload["run_id"] == live_run.run_id
    assert payload["song_id"] == "song-phash-v1-profile-preflight"
    assert payload["profile"] == "normal.json"
    assert payload["settings"]["expected_note_speed"] == pytest.approx(3.5)
    assert payload["settings"]["actual_note_speed"] == pytest.approx(3.5)
    assert payload["settings"]["note_skin_type"] == 7
    assert payload["settings"]["tap_effect"] == 5
    assert payload["settings"]["judgement_assist"] is False
    assert payload["reason"] == "ValueError: profile mismatch"
    assert failure_reasons == ["ValueError: profile mismatch"]


def test_late_preflight_failure_preserves_verified_visual_and_speed(
    monkeypatch, tmp_path,
):
    reset_live_run(
        mode="pending",
        difficulty="Normal",
        prepared_for_play=True,
    )
    update_live_run(
        song_id="song-phash-v1-visual-preflight",
        song_id_method="song-phash-v1",
    )
    context = SimpleNamespace(tasker=Tasker())
    verified = SimpleNamespace(
        difficulty="Normal",
        actual_note_speed=3.5,
        expected_note_speed=3.5,
        profile="normal.json",
        verified_at=1.0,
    )
    visual = SimpleNamespace(
        note_skin_type=7,
        tap_effect=5,
        judgement_assist_effect=False,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.verified_settings",
        lambda _difficulty: verified,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.verified_game_visual_settings",
        lambda: visual,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.runtime_options",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.debug_enabled",
        lambda: (_ for _ in ()).throw(RuntimeError("debug option failed")),
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    recorder_constructions = []
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeDebugRecorder",
        lambda root: recorder_constructions.append(root),
    )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Normal",
        "require_profile": False,
        "settings_gate_required": True,
        "debug_recording": False,
        "run_mode": "formal",
    }))

    assert RealtimeProfilePlay().run(context, argv) is False
    assert recorder_constructions == []
    report = next((tmp_path / "screencap").glob("realtime-result-*.json"))
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["result_status"] == "preflight_error"
    assert payload["terminal_stage"] == "profile_play_preflight"
    assert payload["song_id"] == "song-phash-v1-visual-preflight"
    assert payload["profile"] == "normal.json"
    assert payload["settings"] == {
        "expected_note_speed": 3.5,
        "actual_note_speed": 3.5,
        "note_skin_type": 7,
        "tap_effect": 5,
        "judgement_assist": False,
    }


def test_foreground_failure_does_not_start_debug_recorder(monkeypatch, tmp_path):
    reset_live_run(mode="formal", difficulty="Easy")
    context = SimpleNamespace(tasker=Tasker())
    settings = SimpleNamespace(
        target_fps=60,
        timing_offset_ms=0,
        profile_path=SimpleNamespace(name="easy.json"),
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.resolve_latest",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.require_game_foreground",
        lambda _controller: (_ for _ in ()).throw(
            RuntimeError("game is not foreground")
        ),
    )
    recorder_constructions = []
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeDebugRecorder",
        lambda _root: recorder_constructions.append(_root),
    )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Easy",
        "require_profile": True,
        "debug_recording": True,
    }))

    assert not RealtimeProfilePlay().run(context, argv)
    assert recorder_constructions == []


def test_touch_construction_failure_does_not_start_debug_recorder(
    monkeypatch, tmp_path,
):
    reset_live_run(mode="formal", difficulty="Easy")
    context = SimpleNamespace(tasker=Tasker())
    settings = SimpleNamespace(
        target_fps=60,
        timing_offset_ms=0,
        profile_path=SimpleNamespace(name="easy.json"),
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.resolve_latest",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.require_game_foreground",
        lambda _controller: None,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.ControllerTouchDispatcher",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("touch construction failed")
        ),
    )
    recorder_constructions = []
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeDebugRecorder",
        lambda _root: recorder_constructions.append(_root),
    )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Easy",
        "require_profile": True,
        "debug_recording": True,
    }))

    assert not RealtimeProfilePlay().run(context, argv)
    assert recorder_constructions == []


@pytest.mark.parametrize("failure_point", ["construction", "run"])
def test_preflight_failure_closes_unowned_debug_recorder(
    monkeypatch, tmp_path, failure_point,
):
    reset_live_run(mode="formal", difficulty="Easy")
    context = SimpleNamespace(tasker=Tasker())
    settings = SimpleNamespace(
        target_fps=60,
        timing_offset_ms=0,
        profile_path=SimpleNamespace(name="easy.json"),
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.resolve_latest",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.require_game_foreground",
        lambda _controller: None,
    )

    class Touch:
        def __init__(self):
            self.closed = 0

        def close(self):
            self.closed += 1

    touch = Touch()
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.ControllerTouchDispatcher",
        lambda *_args, **_kwargs: touch,
    )

    class Recorder:
        def __init__(self):
            self.output_dir = tmp_path / "debug-rec"
            self.output_dir.mkdir()
            self.closed = 0
            self.session_metadata = None

        def set_session_metadata(self, metadata):
            self.session_metadata = metadata

        def close(self):
            self.closed += 1
            (self.output_dir / "summary.json").write_text(
                json.dumps({"session": self.session_metadata}),
                encoding="utf-8",
            )

    recorder = Recorder()
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeDebugRecorder",
        lambda _root: recorder,
    )
    if failure_point == "construction":
        monkeypatch.setattr(
            "agent.realtime.profile_play_action.RealtimeEngine",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("engine construction failed")
            ),
        )
    else:
        class Engine:
            def __init__(self, *_args, **_kwargs):
                pass

            def run(self, *_args, **_kwargs):
                raise ValueError("duration_seconds must be in 1..600")

        monkeypatch.setattr(
            "agent.realtime.profile_play_action.RealtimeEngine", Engine,
        )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Easy",
        "require_profile": True,
        "debug_recording": True,
        "save_result_frame": True,
    }))

    assert not RealtimeProfilePlay().run(context, argv)
    assert recorder.closed == 1
    assert touch.closed == 1
    report = next((tmp_path / "screencap").glob("realtime-result-*.json"))
    payload = json.loads(report.read_text(encoding="utf-8"))
    summary = json.loads(
        (recorder.output_dir / "summary.json").read_text(encoding="utf-8")
    )
    assert payload["valid"] is False
    assert payload["result_status"] == "preflight_error"
    assert payload["run_id"] == summary["session"]["run_id"]


def _completed_play_harness(
    monkeypatch,
    tmp_path,
    *,
    debug_recording,
    diagnostic_trace=True,
    collection_status=ResultCollectionStatus.STABLE,
    engine_stopped=False,
    calibration_report=False,
    prepared_for_play=True,
    collection_exception=None,
    screenshot_success=True,
    expected_success=True,
    startup_timed_out=False,
):
    reset_live_run(
        mode="pending",
        difficulty="Easy",
        prepared_for_play=prepared_for_play,
    )
    update_live_run(
        song_id="song-phash-v1-0123456789abcdef",
        song_id_method="song-phash-v1",
    )
    tasker = Tasker()
    context = SimpleNamespace(tasker=tasker)
    settings = SimpleNamespace(
        target_fps=60,
        timing_offset_ms=0,
        profile_path=SimpleNamespace(name="easy.json"),
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.PROJECT_ROOT",
        tmp_path,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.resolve_latest",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr("agent.realtime.profile_play_action.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore.runtime_options",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.require_game_foreground",
        lambda _controller: None,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.ControllerTouchDispatcher",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.debug_enabled",
        lambda: False,
    )
    recorder_holder = {}
    class FakeRecorder:
        def __init__(self, *, video_enabled=True):
            self.output_dir = tmp_path / "debug-rec"
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.video_enabled = video_enabled
            self.session_metadata = None
            recorder_holder["value"] = self

        def set_session_metadata(self, metadata):
            self.session_metadata = metadata

        def close(self):
            (self.output_dir / "summary.json").write_text(json.dumps({
                "schema_version": 2,
                "recording_mode": (
                    "video" if self.video_enabled else "trace-only"
                ),
                "session": self.session_metadata,
            }), encoding="utf-8")

    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeDebugRecorder",
        lambda _root, **kwargs: FakeRecorder(
            video_enabled=kwargs.get("video_enabled", True)
        ),
    )

    class Engine:
        def __init__(self, *args, **kwargs):
            self.debug_recorder = kwargs.get("debug_recorder")

        def run(self, _capture, _stopping, **_kwargs):
            if startup_timed_out:
                _capture()
            if self.debug_recorder is not None:
                self.debug_recorder.close()
            return EngineStats(
                120,
                42,
                engine_stopped,
                completed=not engine_stopped and not startup_timed_out,
                action_counts={"tap": 31, "flick": 4, "down": 7},
                frame_interval_p50_ms=16.4,
                frame_interval_p95_ms=18.2,
                frame_interval_max_ms=24.0,
                effective_fps=59.1,
                terminal_reason=(
                    "用户已停止任务"
                    if engine_stopped
                    else "开演后 20 秒仍未识别到生命条"
                    if startup_timed_out
                    else "已识别演奏结束并进入结算"
                ),
                initial_timing_offset_ms=-11,
                final_timing_offset_ms=-13,
                startup_timed_out=startup_timed_out,
            )

    monkeypatch.setattr("agent.realtime.profile_play_action.RealtimeEngine", Engine)

    image = np.full((720, 1280, 3), 128, dtype=np.uint8)

    def fake_collect(*args, **kwargs):
        if collection_exception is not None:
            raise collection_exception
        return ResultCollectionOutcome(
            collection_status,
            result=(
                LiveResult(100, 10, 2, 1, 2, 3, 4)
                if collection_status is ResultCollectionStatus.STABLE else None
            ),
            image=image,
            elapsed_seconds=1.0,
        )

    monkeypatch.setattr(
        "agent.realtime.profile_play_action.collect_result",
        fake_collect,
    )
    writes = []

    def fake_imwrite(path, _image):
        writes.append(str(path))
        if not screenshot_success:
            return False
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"png")
        return True

    monkeypatch.setattr(
        "agent.realtime.profile_play_action.cv2.imwrite",
        fake_imwrite,
    )

    params = {
        "difficulty": "Easy",
        "require_profile": False,
        "settings_gate_required": False,
        "duration_seconds": 600,
        "wait_for_completion": True,
        "require_completion": True,
        "save_result_frame": True,
        "debug_recording": debug_recording,
        "diagnostic_trace": diagnostic_trace,
    }
    if calibration_report:
        params["calibration_report"] = "screencap/calibration-round.json"
    argv = SimpleNamespace(custom_action_param=json.dumps(params))
    if collection_exception is not None:
        with pytest.raises(type(collection_exception), match=str(collection_exception)):
            RealtimeProfilePlay()._run(context, argv)
    else:
        assert RealtimeProfilePlay()._run(context, argv) is expected_success
    return tmp_path, writes, recorder_holder.get("value")


def test_completed_without_video_writes_json_and_trace_only(tmp_path, monkeypatch):
    root, writes, recorder = _completed_play_harness(
        monkeypatch, tmp_path, debug_recording=False,
    )

    reports = list((root / "screencap").glob("realtime-result-*.json"))
    screenshots = list((root / "screencap").glob("realtime-result-*.png"))
    assert len(reports) == 1
    assert screenshots == []
    assert writes == []
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["perfect"] == 100
    assert payload["processed_frames"] == 120
    assert payload["run_id"]
    assert payload["song_id"] == "song-phash-v1-0123456789abcdef"
    assert payload["debug_recording_path"].endswith("debug-rec")
    assert recorder.video_enabled is False
    summary = json.loads(
        (recorder.output_dir / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["recording_mode"] == "trace-only"


def test_completed_with_diagnostics_disabled_writes_result_only(
    tmp_path, monkeypatch,
):
    root, writes, recorder = _completed_play_harness(
        monkeypatch,
        tmp_path,
        debug_recording=False,
        diagnostic_trace=False,
    )

    report = next((root / "screencap").glob("realtime-result-*.json"))
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["debug_recording_path"] is None
    assert recorder is None
    assert writes == []


def test_direct_profile_play_does_not_reuse_unprepared_song_identity(
    tmp_path, monkeypatch,
):
    root, _, _ = _completed_play_harness(
        monkeypatch,
        tmp_path,
        debug_recording=False,
        prepared_for_play=False,
    )

    report = next((root / "screencap").glob("realtime-result-*.json"))
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["song_id"] == "unknown"
    assert payload["song_id_method"] == "unknown"


def test_completed_with_debug_recording_writes_json_and_screenshot(
    tmp_path, monkeypatch,
):
    root, writes, recorder = _completed_play_harness(
        monkeypatch, tmp_path, debug_recording=True,
    )

    reports = list((root / "screencap").glob("realtime-result-*.json"))
    screenshots = list((root / "screencap").glob("realtime-result-*.png"))
    assert len(reports) == 1
    assert len(screenshots) == 1
    assert len(writes) == 1
    assert str(screenshots[0]) == writes[0]
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["debug_recording_path"].endswith("debug-rec")
    assert recorder.video_enabled is True


def test_result_collection_timeout_writes_invalid_correlated_json(
    tmp_path, monkeypatch,
):
    root, writes, _ = _completed_play_harness(
        monkeypatch,
        tmp_path,
        debug_recording=False,
        collection_status=ResultCollectionStatus.TIMED_OUT,
        expected_success=False,
    )

    reports = list((root / "screencap").glob("realtime-result-*.json"))
    assert len(reports) == 1
    assert len(writes) == 1
    assert "realtime-result-timeout-" in writes[0]
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["valid"] is False
    assert payload["result_status"] == "timed_out"
    assert payload["run_id"]
    assert payload["song_id"] == "song-phash-v1-0123456789abcdef"
    assert payload["result_diagnostic_frame"].startswith(
        "screencap/realtime-result-timeout-"
    )
    assert "perfect" not in payload


def test_result_collection_timeout_stays_reportable_for_calibration_round(
    tmp_path, monkeypatch,
):
    root, writes, _ = _completed_play_harness(
        monkeypatch,
        tmp_path,
        debug_recording=False,
        collection_status=ResultCollectionStatus.TIMED_OUT,
        calibration_report=True,
        expected_success=True,
    )

    calibration = json.loads(
        (root / "screencap" / "calibration-round.json").read_text(
            encoding="utf-8"
        )
    )
    assert calibration["valid"] is False
    assert calibration["result_status"] == "timed_out"
    assert len(writes) == 1


def test_playfield_start_timeout_saves_frame_and_fails_ordinary_play(
    tmp_path, monkeypatch,
):
    root, writes, _ = _completed_play_harness(
        monkeypatch,
        tmp_path,
        debug_recording=False,
        startup_timed_out=True,
        expected_success=False,
    )

    report = next((root / "screencap").glob("realtime-result-*.json"))
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["valid"] is False
    assert payload["result_status"] == "playfield_start_timeout"
    assert payload["startup_timed_out"] is True
    assert payload["startup_diagnostic_frame"].startswith(
        "screencap/realtime-startup-timeout-"
    )
    assert len(writes) == 1


def test_result_collection_exception_writes_invalid_correlated_json(
    tmp_path, monkeypatch,
):
    root, _, _ = _completed_play_harness(
        monkeypatch,
        tmp_path,
        debug_recording=False,
        collection_exception=RuntimeError("capture failed"),
    )

    report = next((root / "screencap").glob("realtime-result-*.json"))
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["valid"] is False
    assert payload["result_status"] == "result_collection_error"
    assert payload["reason"] == "结算读取异常: RuntimeError: capture failed"
    assert payload["processed_frames"] == 120
    assert payload["run_id"] == payload["session"]["run_id"]


def test_debug_screenshot_failure_keeps_stable_json_result(
    tmp_path, monkeypatch,
):
    root, writes, _ = _completed_play_harness(
        monkeypatch,
        tmp_path,
        debug_recording=True,
        screenshot_success=False,
    )

    report = next((root / "screencap").glob("realtime-result-*.json"))
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert writes
    assert payload["valid"] is True
    assert payload["result_status"] == "stable"
    assert "无法保存结算截图" in payload["result_screenshot_error"]


def test_engine_stop_writes_neutral_structured_result_without_collecting_frame(
    tmp_path, monkeypatch,
):
    root, writes, _ = _completed_play_harness(
        monkeypatch,
        tmp_path,
        debug_recording=False,
        engine_stopped=True,
    )

    reports = list((root / "screencap").glob("realtime-result-*.json"))
    assert len(reports) == 1
    assert writes == []
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["valid"] is False
    assert payload["result_status"] == "stopped"
    assert payload["reason"] == "用户已停止任务"
    assert payload["eligible_for_profile_acceptance"] is False


def test_result_collection_stop_writes_neutral_structured_result(
    tmp_path, monkeypatch,
):
    root, writes, _ = _completed_play_harness(
        monkeypatch,
        tmp_path,
        debug_recording=False,
        collection_status=ResultCollectionStatus.STOPPED,
    )

    reports = list((root / "screencap").glob("realtime-result-*.json"))
    assert len(reports) == 1
    assert writes == []
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["valid"] is False
    assert payload["result_status"] == "stopped"
    assert payload["reason"] == "用户在结算读取期间停止任务"


def test_one_run_links_result_calibration_and_recorder_summary(
    tmp_path, monkeypatch,
):
    root, _, recorder = _completed_play_harness(
        monkeypatch,
        tmp_path,
        debug_recording=True,
        calibration_report=True,
    )

    result_path = next((root / "screencap").glob("realtime-result-*.json"))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    calibration = json.loads(
        (root / "screencap" / "calibration-round.json").read_text(
            encoding="utf-8"
        )
    )
    summary = json.loads(
        (recorder.output_dir / "summary.json").read_text(encoding="utf-8")
    )

    assert result["run_id"] == calibration["run_id"]
    assert result["run_id"] == summary["session"]["run_id"]
    assert result["song_id"] == calibration["song_id"]
    assert result["song_id"] == summary["session"]["song_id"]


def test_dismiss_reward_popup_clicks_matched_button():
    template = cv2.imread(str(profile_play_action.REWARD_OK_TEMPLATE))
    assert template is not None
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    image[568:642, 562:716] = template
    clicks = []
    foreground_checks = []

    class FakeController:
        def post_click(self, x, y):
            clicks.append((x, y))
            return SimpleNamespace(wait=lambda: None)

    assert _dismiss_reward_popup(
        FakeController(),
        image,
        before_input=lambda: foreground_checks.append(1),
        threshold=0.8,
    ) is True
    assert clicks == [(639, 605)]
    assert foreground_checks == [1]


def test_dismiss_reward_popup_ignores_clean_result_screen():
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    clicks = []

    class FakeController:
        def post_click(self, x, y):
            clicks.append((x, y))
            return SimpleNamespace(wait=lambda: None)

    assert _dismiss_reward_popup(FakeController(), image, threshold=0.8) is False
    assert clicks == []


def test_collect_result_dismisses_reward_popup_before_stabilizing():
    template = cv2.imread(str(profile_play_action.REWARD_OK_TEMPLATE))
    popup = np.zeros((720, 1280, 3), dtype=np.uint8)
    popup[568:642, 562:716] = template
    clean = np.zeros((720, 1280, 3), dtype=np.uint8)
    clean[0:10, 0:10] = 255
    images = [popup, popup, clean, clean]

    class FakeParser:
        def parse(self, image):
            if image is popup:
                raise ValueError("reward popup covers digits")
            return LiveResult(
                perfect=100, great=0, good=0, bad=0, miss=0,
                fast=0, slow=0, confidence=1.0,
            )

    clicks = []
    keys = []

    class FakeJob:
        def __init__(self, value=None):
            self.value = value

        def wait(self):
            return self

        def get(self):
            return self.value

    class FakeController:
        def post_screencap(self):
            return FakeJob(images.pop(0))

        def post_click(self, x, y):
            clicks.append((x, y))
            return FakeJob()

        def post_click_key(self, key):
            keys.append(key)
            return FakeJob()

    clock_state = [0.0]

    def clock():
        clock_state[0] += 1.0
        return clock_state[0]

    outcome = collect_result(
        FakeController(),
        lambda: False,
        parser=FakeParser(),
        sleeper=lambda _: None,
        clock=clock,
        reward_click_delay_seconds=0.0,
        reward_threshold=0.8,
        judgement_details_template=None,
    )

    assert outcome.status is ResultCollectionStatus.STABLE
    assert outcome.result is not None
    assert clicks == []
    assert keys == [4, 4]
