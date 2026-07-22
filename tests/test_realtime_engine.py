from __future__ import annotations

import numpy as np
import pytest

from agent.realtime.engine import RealtimeEngine
from agent.realtime.touch_planner import ActionKind, TouchAction
from agent.realtime.life_monitor import LifeGuard, LifeReading


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class Detector:
    def __init__(self, fail=False):
        self.fail = fail

    def detect(self, image, now):
        if self.fail:
            raise RuntimeError("detector failed")
        return [object()]


class Planner:
    def __init__(self):
        self.updates = 0
        self.resets = 0

    def update(self, notes, now):
        self.updates += 1
        return [TouchAction(ActionKind.TAP, 1, now)]

    def reset(self, now):
        self.resets += 1
        return [TouchAction(ActionKind.UP, 5, now, 5)]


class Touch:
    def __init__(self):
        self.batches = []
        self.closed = 0

    def dispatch(self, actions):
        self.batches.append(actions)

    def close(self):
        self.closed += 1


def build(fail=False):
    clock = Clock()
    planner = Planner()
    touch = Touch()
    engine = RealtimeEngine(Detector(fail), planner, touch, clock)

    def capture():
        clock.value += 0.02
        return np.zeros((1, 1, 3), dtype=np.uint8)

    return engine, clock, planner, touch, capture


def test_engine_normal_exit_releases_planner_and_dispatcher_state():
    engine, _, planner, touch, capture = build()

    stats = engine.run(capture, lambda: False, duration_seconds=1, target_fps=60)

    assert stats.processed_frames == 50
    assert planner.resets == 1
    assert touch.batches[-1][0].kind == ActionKind.UP
    assert touch.closed == 1


def test_engine_stop_releases_everything_without_another_capture():
    engine, _, planner, touch, capture = build()

    stats = engine.run(
        capture, lambda: planner.updates == 2, duration_seconds=10, target_fps=60
    )

    assert stats.stopped
    assert planner.updates == 2
    assert planner.resets == 1
    assert touch.closed == 1


def test_engine_exception_still_releases_everything():
    engine, _, planner, touch, capture = build(fail=True)

    with pytest.raises(RuntimeError, match="detector failed"):
        engine.run(capture, lambda: False, duration_seconds=1, target_fps=60)

    assert planner.resets == 1
    assert touch.closed == 1


def test_engine_aborts_and_cleans_up_after_confirmed_zero_life():
    engine, _, planner, touch, capture = build()

    class ZeroLife:
        def detect(self, image):
            return LifeReading(True, 0)

    engine.life_detector = ZeroLife()
    engine.life_guard = LifeGuard(confirm_frames=3)

    stats = engine.run(capture, lambda: False, duration_seconds=10, target_fps=60)

    assert stats.aborted_for_life
    assert planner.resets == 1
    assert touch.closed == 1
