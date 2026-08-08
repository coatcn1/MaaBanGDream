from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from agent.realtime.engine import EngineStats
from agent.realtime import profile_play_action
from agent.realtime.profile_play_action import (
    RealtimeLifeSafetyAbortCheck,
    RealtimeProfilePlay,
    ResultCollectionOutcome,
    ResultCollectionStatus,
    _result_report_payload,
    _write_calibration_report,
    pause_overlay_changed,
    resolve_life_policy,
)
from agent.realtime.result_parser import LiveResult
from agent.realtime.performance_settings_action import clear_verified_settings


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

    class Engine:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, capture, stopping, **kwargs):
            capture()
            return EngineStats(1, 0, False)

    monkeypatch.setattr("agent.realtime.profile_play_action.RealtimeEngine", Engine)

    argv = SimpleNamespace(custom_action_param=json.dumps({"difficulty": "Easy"}))
    assert RealtimeProfilePlay()._run(context, argv)
    assert tasker.controller_reads == 1
    assert foreground_checks == [tasker._controller]
    assert dispatcher_options == [{}]


def test_profile_play_refuses_pipeline_start_without_fresh_speed_gate():
    clear_verified_settings()
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


def test_formal_timeout_records_a_specific_failure_reason(monkeypatch):
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
    }
    argv = SimpleNamespace(custom_action_param=json.dumps(params))

    assert not RealtimeProfilePlay()._run(context, argv)
    assert reasons == ["演奏超过安全时限 600 秒，仍未识别到结算画面"]


def _completed_play_harness(monkeypatch, tmp_path, *, debug_recording):
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
    if debug_recording:
        monkeypatch.setattr(
            "agent.realtime.profile_play_action.RealtimeDebugRecorder",
            lambda _root: SimpleNamespace(output_dir=tmp_path / "debug-rec"),
        )

    class Engine:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, _capture, _stopping, **_kwargs):
            return EngineStats(
                120,
                42,
                False,
                completed=True,
                action_counts={"tap": 31, "flick": 4, "down": 7},
                frame_interval_p50_ms=16.4,
                frame_interval_p95_ms=18.2,
                frame_interval_max_ms=24.0,
                effective_fps=59.1,
                terminal_reason="已识别演奏结束并进入结算",
                initial_timing_offset_ms=-11,
                final_timing_offset_ms=-13,
            )

    monkeypatch.setattr("agent.realtime.profile_play_action.RealtimeEngine", Engine)

    image = np.full((720, 1280, 3), 128, dtype=np.uint8)

    def fake_collect(*args, **kwargs):
        return ResultCollectionOutcome(
            ResultCollectionStatus.STABLE,
            result=LiveResult(100, 10, 2, 1, 2, 3, 4),
            image=image,
            elapsed_seconds=1.0,
        )

    monkeypatch.setattr(
        "agent.realtime.profile_play_action.collect_result",
        fake_collect,
    )
    writes = []
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.cv2.imwrite",
        lambda path, _image: (
            writes.append(str(path))
            or Path(path).parent.mkdir(parents=True, exist_ok=True)
            or Path(path).write_bytes(b"png")
            or True
        ),
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
    }
    argv = SimpleNamespace(custom_action_param=json.dumps(params))
    assert RealtimeProfilePlay()._run(context, argv)
    return tmp_path, writes


def test_completed_without_debug_recording_writes_json_only(tmp_path, monkeypatch):
    root, writes = _completed_play_harness(
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


def test_completed_with_debug_recording_writes_json_and_screenshot(
    tmp_path, monkeypatch,
):
    root, writes = _completed_play_harness(
        monkeypatch, tmp_path, debug_recording=True,
    )

    reports = list((root / "screencap").glob("realtime-result-*.json"))
    screenshots = list((root / "screencap").glob("realtime-result-*.png"))
    assert len(reports) == 1
    assert len(screenshots) == 1
    assert len(writes) == 1
    assert str(screenshots[0]) == writes[0]
