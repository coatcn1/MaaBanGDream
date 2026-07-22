from __future__ import annotations

import numpy as np
import pytest

from agent.realtime.engine import RealtimeEngine
from agent.realtime.touch_planner import ActionKind, TouchAction
from agent.realtime.life_monitor import LifeGuard, LifeReading, PlayfieldCompletionGuard


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


def test_engine_records_each_processed_frame_and_closes_debug_recorder():
    engine, _, planner, _, capture = build()

    class Recorder:
        def __init__(self):
            self.records = []
            self.closed = 0

        def record(self, image, timestamp, notes, actions, life_status):
            self.records.append((timestamp, notes, actions, life_status))

        def close(self):
            self.closed += 1

    recorder = Recorder()
    engine.debug_recorder = recorder

    stats = engine.run(capture, lambda: False, duration_seconds=1, target_fps=60)

    assert len(recorder.records) == stats.processed_frames
    assert len(recorder.records[0][1]) == 1
    assert recorder.records[0][2][0].kind is ActionKind.TAP
    assert recorder.closed == 1


def test_engine_aborts_and_cleans_up_after_confirmed_zero_life():
    engine, _, planner, touch, capture = build()

    class ZeroLife:
        def __init__(self):
            self.frames = 0

        def detect(self, image):
            self.frames += 1
            return LifeReading(True, 800 if self.frames <= 3 else 0)

    engine.life_detector = ZeroLife()
    engine.life_guard = LifeGuard(confirm_frames=3)

    stats = engine.run(capture, lambda: False, duration_seconds=10, target_fps=60)

    assert stats.aborted_for_life
    assert planner.resets == 1
    assert touch.closed == 1


def test_engine_can_continue_after_zero_life_until_completion():
    engine, _, planner, touch, capture = build()

    class DepletesThenEnds:
        def __init__(self): self.frames = 0
        def detect(self, image):
            self.frames += 1
            if self.frames <= 3: return LifeReading(True, 800)
            if self.frames <= 6: return LifeReading(True, 0)
            return LifeReading(False)

    engine.life_detector = DepletesThenEnds()
    engine.life_guard = LifeGuard(confirm_frames=3)
    engine.completion_guard = PlayfieldCompletionGuard(missing_frames=3)

    stats = engine.run(
        capture, lambda: False, duration_seconds=10, target_fps=60,
        continue_after_life_depleted=True,
    )

    assert stats.life_depleted
    assert not stats.aborted_for_life
    assert stats.completed
    assert planner.resets == 1
    assert touch.closed == 1


def test_engine_invokes_life_safety_after_three_frames_below_threshold():
    engine, _, _, touch, capture = build()
    triggered = []

    class FallingLife:
        def __init__(self): self.frames = 0
        def detect(self, image):
            self.frames += 1
            return LifeReading(True, 800 if self.frames <= 3 else 190)

    engine.life_detector = FallingLife()
    engine.life_guard = LifeGuard(confirm_frames=3)

    stats = engine.run(
        capture, lambda: False, duration_seconds=10, target_fps=60,
        life_exit_threshold=200,
        on_life_safety=lambda reading: triggered.append(reading.value),
    )

    assert stats.aborted_for_life
    assert not stats.life_depleted
    assert triggered == [190]
    assert touch.closed == 1


def test_zero_life_uses_safety_pause_callback_before_plain_abort():
    engine, _, _, touch, capture = build()
    triggered = []

    class ZeroLife:
        def __init__(self): self.frames = 0
        def detect(self, image):
            self.frames += 1
            return LifeReading(True, 800 if self.frames <= 3 else 0)

    engine.life_detector = ZeroLife()
    engine.life_guard = LifeGuard(confirm_frames=3)

    stats = engine.run(
        capture, lambda: False, duration_seconds=10, target_fps=60,
        life_exit_threshold=200,
        on_life_safety=lambda reading: triggered.append(reading.value),
    )

    assert stats.aborted_for_life
    assert stats.life_depleted
    assert triggered == [0]
    assert touch.closed == 1


def test_engine_never_dispatches_before_alive_life_is_confirmed():
    engine, clock, planner, touch, _ = build()

    class NeverAlive:
        def detect(self, image):
            return LifeReading(True, 0)

    engine.life_detector = NeverAlive()
    engine.life_guard = LifeGuard(confirm_frames=3)

    def capture():
        clock.value += 0.02
        return np.zeros((720, 1280, 3), dtype=np.uint8)

    stats = engine.run(capture, lambda: False, duration_seconds=1, target_fps=60)

    assert not stats.aborted_for_life
    assert planner.updates == 0
    assert not [batch for batch in touch.batches if batch[0].kind is ActionKind.TAP]


def test_engine_completes_after_confirmed_playfield_disappears():
    engine, _, planner, touch, capture = build()

    class EndsAfterAlive:
        def __init__(self):
            self.frames = 0

        def detect(self, image):
            self.frames += 1
            return LifeReading(self.frames <= 4, 800)

    engine.life_detector = EndsAfterAlive()
    engine.life_guard = LifeGuard(confirm_frames=3)
    engine.completion_guard = PlayfieldCompletionGuard(missing_frames=3)

    stats = engine.run(capture, lambda: False, duration_seconds=10, target_fps=60)

    assert stats.completed
    assert not stats.aborted_for_life
    assert planner.resets == 1
    assert touch.closed == 1
