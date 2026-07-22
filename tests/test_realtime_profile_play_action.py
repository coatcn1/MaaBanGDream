from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np

from agent.realtime.engine import EngineStats
from agent.realtime import profile_play_action
from agent.realtime.profile_play_action import (
    RealtimeLifeSafetyAbortCheck,
    RealtimeProfilePlay,
    pause_overlay_changed,
)


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
