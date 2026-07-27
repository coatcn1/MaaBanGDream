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
        self.timing_offset_ms = 0
        self.offset_changes = []
        self.has_active_holds = False

    def update(self, notes, now):
        self.updates += 1
        return [TouchAction(ActionKind.TAP, 1, now)]

    def reset(self, now):
        self.resets += 1
        return [TouchAction(ActionKind.UP, 5, now, 5)]

    def set_timing_offset_ms(self, value):
        self.timing_offset_ms = value
        self.offset_changes.append(value)

    def drain_diagnostics(self):
        return []


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
    assert stats.action_counts == {"tap": 50}
    assert stats.frame_interval_p50_ms == pytest.approx(20.0)
    assert stats.frame_interval_p95_ms == pytest.approx(20.0)
    assert stats.frame_interval_max_ms == pytest.approx(20.0)
    assert stats.effective_fps == pytest.approx(50.0)
    assert stats.terminal_reason == (
        "演奏超过安全时限 1 秒，仍未识别到结算画面"
    )
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

        def record(
            self, image, timestamp, notes, actions, life_status,
            diagnostics, timing_state,
        ):
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


def test_invisible_transition_frames_do_not_trigger_life_safety():
    engine, _, planner, touch, capture = build()
    triggered = []

    class EndsWithDefaultInvisibleReading:
        def __init__(self):
            self.frames = 0

        def detect(self, image):
            self.frames += 1
            if self.frames <= 4:
                return LifeReading(True, 800)
            return LifeReading(False)

    engine.life_detector = EndsWithDefaultInvisibleReading()
    engine.life_guard = LifeGuard(confirm_frames=3)
    engine.completion_guard = PlayfieldCompletionGuard(missing_frames=4)

    stats = engine.run(
        capture,
        lambda: False,
        duration_seconds=10,
        target_fps=60,
        life_exit_threshold=200,
        on_life_safety=lambda reading: triggered.append(reading.value),
    )

    assert stats.completed
    assert not stats.aborted_for_life
    assert triggered == []
    assert planner.resets == 1
    assert touch.closed == 1


def test_engine_applies_live_timing_feedback_to_the_planner():
    engine, _, planner, _, capture = build()

    class FeedbackDetector:
        def __init__(self):
            self.index = 0

        def detect(self, image):
            self.index += 1
            return "slow" if self.index % 2 else None

    class FeedbackController:
        current_offset_ms = 0
        fast_samples = 0
        slow_samples = 0
        valid_samples = 0
        ignored_samples = 0
        ignored_reasons = {}

        def update(self, feedback, now, *, eligible, ignored_reason):
            if feedback == "slow":
                self.slow_samples += 1
                self.valid_samples += 1
            if feedback == "slow" and self.slow_samples == 5:
                self.current_offset_ms = 2
                return 2
            return None

    engine.timing_feedback_detector = FeedbackDetector()
    engine.timing_controller = FeedbackController()

    stats = engine.run(capture, lambda: False, duration_seconds=1, target_fps=60)

    assert planner.offset_changes == [2]
    assert stats.initial_timing_offset_ms == 0
    assert stats.final_timing_offset_ms == 2
    assert stats.timing_feedback_slow == 25


def test_engine_ignores_feedback_while_a_hold_is_active():
    from agent.realtime.timing_feedback import AdaptiveTimingController

    engine, _, planner, _, capture = build()
    planner.has_active_holds = True

    class FeedbackDetector:
        def __init__(self):
            self.index = 0

        def detect(self, image):
            self.index += 1
            return "slow" if self.index % 2 else None

    engine.timing_feedback_detector = FeedbackDetector()
    engine.timing_controller = AdaptiveTimingController(
        0,
        minimum_samples=3,
        imbalance=3,
        adjustment_cooldown_seconds=0,
    )

    stats = engine.run(capture, lambda: False, duration_seconds=1, target_fps=60)

    assert planner.offset_changes == []
    assert stats.timing_feedback_valid == 0
    assert stats.timing_feedback_ignored == 25
    assert stats.timing_feedback_ignored_reasons == {"active_hold": 25}
