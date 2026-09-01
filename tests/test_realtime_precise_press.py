"""谱面按压精确派发与结算偏移写回的单元测试。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pytest

from agent.realtime.engine import RealtimeEngine
from agent.realtime.profile_play_action import _persist_profile_timing_offset
from agent.realtime.profile_store import RealtimeProfileStore, RuntimeSettings
from agent.realtime.touch_planner import ActionKind, TouchAction


class _Planner:
    def __init__(self, due_action: TouchAction | None):
        self.due_action = due_action
        self.emitted = False
        self.has_active_holds = False
        self.timing_offset_ms = 0
        self.updates = 0

    def update(self, notes, now):
        self.updates += 1
        if self.due_action is not None and not self.emitted:
            self.emitted = True
            return [
                TouchAction(
                    self.due_action.kind,
                    self.due_action.lane,
                    now + 0.018,
                    reason="chart-predicted",
                )
            ]
        return []

    def drain_diagnostics(self):
        return []

    def reset(self, now):
        return []

    def set_timing_offset_ms(self, value):
        self.timing_offset_ms = value


class _Detector:
    def detect(self, image, now):
        return []


class _Touch:
    def __init__(self, on_dispatch=None):
        self.dispatches: list[tuple[float, list[TouchAction]]] = []
        self.on_dispatch = on_dispatch

    def dispatch(self, actions):
        self.dispatches.append((time.perf_counter(), list(actions)))
        if self.on_dispatch is not None:
            self.on_dispatch(actions)

    def close(self):
        pass

    def has_active_or_pending_contacts(self):
        return False


def test_chart_press_dispatches_at_due_time_not_frame_time():
    stop_flag = {"value": False}

    def on_dispatch(actions):
        if any(action.kind is ActionKind.TAP for action in actions):
            stop_flag["value"] = True

    touch = _Touch(on_dispatch)
    planner = _Planner(TouchAction(ActionKind.TAP, 3, 0.0))
    engine = RealtimeEngine(
        _Detector(), planner, touch, clock=time.perf_counter,
    )
    started = time.perf_counter()

    def capture():
        # StallSafeCapture 是非阻塞的：在途截图未完成时立即返回上一帧，
        # 引擎循环按 next_frame 定速，因此每帧都存在等待窗口。
        return np.zeros((720, 1280, 3), dtype=np.uint8)

    engine.run(
        capture,
        lambda: stop_flag["value"],
        duration_seconds=1,
        target_fps=60,
    )
    assert planner.emitted
    dispatched = [
        (when, action)
        for when, actions in touch.dispatches
        for action in actions
        if action.kind is ActionKind.TAP
    ]
    assert dispatched
    when, _action = dispatched[0]
    # 首次 update 发生在 run 启动后极短时间内；到期时间应在其后 18ms 附近。
    # 容差覆盖 1ms 计时器粒度与派发开销，但必须远小于 16.7ms 的帧量化。
    assert 0.012 <= when - started <= 0.030


def test_persist_profile_timing_offset_keeps_accepted_status(
    tmp_path,
    monkeypatch,
):
    store = RealtimeProfileStore(tmp_path)
    store.write({
        "schema_version": 1,
        "created_at": "2026-09-02T01:00:00",
        "difficulty": "Expert",
        "accepted": True,
        "accepted_at": "2026-09-02T01:05:00",
        "environment": {
            "resolution": [1280, 720],
            "dpi": 240,
            "game_fps": 60,
            "render_quality": "standard",
            "note_speed": 5.0,
            "note_skin_type": 1,
            "tap_effect": 4,
            "judgement_assist_effect": False,
        },
        "settings": {
            "target_fps": 60,
            "timing_offset_ms": 18,
            "frame_timeout_ms": 150,
            "playfield_timeout_ms": 1500,
        },
        "rehearsals": [],
    })
    profile_name = next(tmp_path.glob("*.json")).name
    settings = RuntimeSettings(
        target_fps=60,
        timing_offset_ms=18,
        frame_timeout_ms=150,
        playfield_timeout_ms=1500,
        profile_path=tmp_path / profile_name,
        note_speed=5.0,
    )
    monkeypatch.setattr(
        "agent.realtime.profile_play_action.RealtimeProfileStore",
        lambda *args, **kwargs: store,
    )
    _persist_profile_timing_offset(settings, 26)
    saved = json.loads((tmp_path / profile_name).read_text(encoding="utf-8"))
    assert saved["settings"]["timing_offset_ms"] == 26
    assert saved["accepted"] is True
    assert saved.get("invalidated_reason") is None


def test_persist_profile_timing_offset_failure_is_contained(tmp_path):
    settings = RuntimeSettings(
        target_fps=60,
        timing_offset_ms=18,
        frame_timeout_ms=150,
        playfield_timeout_ms=1500,
        profile_path=Path("missing-profile.json"),
        note_speed=5.0,
    )
    # 不存在的 Profile 只打印日志，不抛异常。
    _persist_profile_timing_offset(settings, 26)
