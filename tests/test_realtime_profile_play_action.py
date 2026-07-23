from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np

from agent.realtime.engine import EngineStats
from agent.realtime import profile_play_action
from agent.realtime.profile_play_action import (
    RealtimeLifeSafetyAbortCheck,
    RealtimeProfilePlay,
    _write_calibration_report,
    pause_overlay_changed,
)
from agent.realtime.result_parser import LiveResult


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
