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


class ResetTrackingTouch(Touch):
    def __init__(self):
        super().__init__()
        self.active_contacts = set()
        self.force_release_calls = 0
        self.emergency_release_calls = 0

    def force_release_all(self):
        self.force_release_calls += 1

    def emergency_release_all(self):
        self.emergency_release_calls += 1


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
    assert stats.frame_interval_outliers == ()
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


def test_engine_allows_unbounded_listener_only_until_manual_stop():
    engine, _, planner, touch, capture = build()

    stats = engine.run(
        capture,
        lambda: planner.updates == 5,
        duration_seconds=None,
        target_fps=60,
    )

    assert stats.stopped
    assert planner.updates == 5
    assert touch.closed == 1


def test_engine_keeps_bounded_task_limit_at_600_seconds():
    engine, _, _, _, capture = build()

    with pytest.raises(ValueError, match="1..600"):
        engine.run(capture, lambda: False, duration_seconds=601, target_fps=60)


def test_engine_exception_still_releases_everything():
    engine, _, planner, touch, capture = build(fail=True)

    with pytest.raises(RuntimeError, match="detector failed") as raised:
        engine.run(capture, lambda: False, duration_seconds=1, target_fps=60)

    stats = raised.value.realtime_stats
    assert stats.processed_frames == 0
    assert stats.dispatched_actions == 0
    assert stats.cleanup_failed is False
    assert stats.terminal_reason == "实时演奏引擎异常: RuntimeError: detector failed"
    assert stats.stage_timings_ms["capture"]["max"] == pytest.approx(20.0)
    assert planner.resets == 1
    assert touch.closed == 1


def test_engine_does_not_force_release_all_during_periodic_idle():
    clock = Clock()
    planner = Planner()
    touch = ResetTrackingTouch()
    engine = RealtimeEngine(Detector(), planner, touch, clock)

    def capture():
        clock.value += 0.02
        return np.zeros((1, 1, 3), dtype=np.uint8)

    stats = engine.run(
        capture,
        lambda: False,
        duration_seconds=31,
        target_fps=60,
    )

    assert touch.force_release_calls == 0
    assert stats.touch_resets == 0


def test_engine_uses_nonblocking_emergency_release_on_severe_life_drop():
    clock = Clock()
    planner = Planner()
    touch = ResetTrackingTouch()
    engine = RealtimeEngine(Detector(), planner, touch, clock)

    class FallingLife:
        def __init__(self):
            self.frames = 0

        def detect(self, image):
            self.frames += 1
            return LifeReading(True, 1000 if self.frames <= 3 else 250)

    engine.life_detector = FallingLife()
    engine.life_guard = LifeGuard(confirm_frames=3)

    def capture():
        clock.value += 0.02
        return np.zeros((720, 1280, 3), dtype=np.uint8)

    stats = engine.run(
        capture,
        lambda: False,
        duration_seconds=1,
        target_fps=60,
    )

    assert touch.force_release_calls == 0
    assert touch.emergency_release_calls == 1
    assert stats.touch_resets == 1


def test_engine_resets_touch_before_rapid_life_loss_reaches_safety_threshold():
    clock = Clock()
    planner = Planner()
    touch = ResetTrackingTouch()
    engine = RealtimeEngine(Detector(), planner, touch, clock)

    class RapidlyFallingLife:
        def __init__(self):
            self.frames = 0

        def detect(self, image):
            self.frames += 1
            if self.frames <= 3:
                value = 1000
            elif self.frames <= 13:
                value = 920
            elif self.frames <= 23:
                value = 821
            else:
                value = 722
            return LifeReading(True, value)

    engine.life_detector = RapidlyFallingLife()
    engine.life_guard = LifeGuard(confirm_frames=3)

    def capture():
        clock.value += 0.02
        return np.zeros((720, 1280, 3), dtype=np.uint8)

    stats = engine.run(
        capture,
        lambda: False,
        duration_seconds=1,
        target_fps=60,
    )

    assert touch.emergency_release_calls == 1
    assert stats.touch_resets == 1


@pytest.mark.parametrize("failure_stage", ["capture", "detector", "planner", "dispatch"])
def test_engine_stage_error_keeps_metrics_from_completed_frames(failure_stage):
    clock = Clock()

    class StageDetector:
        def __init__(self):
            self.calls = 0

        def detect(self, image, now):
            self.calls += 1
            if failure_stage == "detector" and self.calls == 3:
                raise RuntimeError("detector stage failed")
            return [object()]

    class StagePlanner(Planner):
        def update(self, notes, now):
            if failure_stage == "planner" and self.updates == 2:
                raise RuntimeError("planner stage failed")
            return super().update(notes, now)

    class StageTouch(Touch):
        def __init__(self):
            super().__init__()
            self.tap_batches = 0

        def dispatch(self, actions):
            if actions and actions[0].kind is ActionKind.TAP:
                self.tap_batches += 1
                if failure_stage == "dispatch" and self.tap_batches == 3:
                    raise RuntimeError("dispatch stage failed")
            super().dispatch(actions)

    capture_calls = 0

    def capture():
        nonlocal capture_calls
        capture_calls += 1
        clock.value += .02
        if failure_stage == "capture" and capture_calls == 3:
            raise RuntimeError("capture stage failed")
        return np.zeros((1, 1, 3), dtype=np.uint8)

    planner = StagePlanner()
    touch = StageTouch()
    engine = RealtimeEngine(StageDetector(), planner, touch, clock)

    with pytest.raises(RuntimeError, match=f"{failure_stage} stage failed") as raised:
        engine.run(capture, lambda: False, duration_seconds=1, target_fps=60)

    stats = raised.value.realtime_stats
    assert stats.processed_frames == 2
    assert stats.dispatched_actions == 2
    assert stats.action_counts == {"tap": 2}
    assert stats.frame_interval_p50_ms == pytest.approx(20.0)
    assert stats.effective_fps == pytest.approx(50.0)
    assert stats.cleanup_failed is False
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
            diagnostics, timing_state, life_value=None, touch_state=None,
        ):
            self.records.append((
                timestamp, notes, actions, life_status, life_value, touch_state,
            ))

        def close(self):
            self.closed += 1

    recorder = Recorder()
    engine.debug_recorder = recorder

    stats = engine.run(capture, lambda: False, duration_seconds=1, target_fps=60)

    assert len(recorder.records) == stats.processed_frames
    assert len(recorder.records[0][1]) == 1
    assert recorder.records[0][2][0].kind is ActionKind.TAP
    assert recorder.closed == 1


def test_engine_records_numeric_life_and_post_dispatch_touch_state():
    clock = Clock()
    planner = Planner()

    class ObservableTouch(Touch):
        def __init__(self):
            super().__init__()
            self.active_contacts = set()

        def dispatch(self, actions):
            super().dispatch(actions)
            self.active_contacts.add(8)

        def trace_state(self):
            return {
                "active_contacts": sorted(self.active_contacts),
                "contact_aliases": {"1": 8},
            }

    class Life:
        def detect(self, image):
            return LifeReading(True, 742)

    class Recorder:
        def __init__(self):
            self.records = []

        def record(
            self, image, timestamp, notes, actions, life_status,
            diagnostics, timing_state, life_value=None, touch_state=None,
        ):
            self.records.append((life_value, touch_state))

        def close(self):
            pass

    touch = ObservableTouch()
    recorder = Recorder()
    engine = RealtimeEngine(
        Detector(), planner, touch, clock,
        life_detector=Life(), life_guard=LifeGuard(confirm_frames=3),
        debug_recorder=recorder,
    )

    def capture():
        clock.value += .02
        return np.zeros((720, 1280, 3), dtype=np.uint8)

    engine.run(
        capture,
        lambda: planner.updates == 1,
        duration_seconds=1,
        target_fps=60,
    )

    assert recorder.records == [(742, {
        "active_contacts": [8],
        "contact_aliases": {"1": 8},
    })]


def test_engine_records_terminal_life_value_before_safety_abort():
    clock = Clock()
    planner = Planner()
    touch = ResetTrackingTouch()

    class Life:
        def __init__(self):
            self.values = iter([800, 800, 800, 0, 0, 0])

        def detect(self, image):
            return LifeReading(True, next(self.values))

    class Recorder:
        def __init__(self):
            self.records = []

        def record(
            self, image, timestamp, notes, actions, life_status,
            diagnostics, timing_state, life_value=None, touch_state=None,
        ):
            self.records.append({
                "notes": notes,
                "actions": actions,
                "life_value": life_value,
                "diagnostics": diagnostics,
            })

        def close(self):
            pass

    recorder = Recorder()
    engine = RealtimeEngine(
        Detector(), planner, touch, clock,
        life_detector=Life(), life_guard=LifeGuard(confirm_frames=3),
        debug_recorder=recorder,
    )

    def capture():
        clock.value += .02
        return np.zeros((720, 1280, 3), dtype=np.uint8)

    stats = engine.run(
        capture,
        lambda: False,
        duration_seconds=1,
        target_fps=60,
    )

    assert stats.aborted_for_life
    assert recorder.records[-1]["life_value"] == 0
    assert recorder.records[-1]["notes"] == []
    assert recorder.records[-1]["actions"] == []
    assert recorder.records[-1]["diagnostics"] == [{
        "event": "life_terminal",
        "timestamp": pytest.approx(.12),
        "reason": "life-dead",
        "life_value": 0,
    }]
def test_debug_recorder_close_failure_is_reported_without_losing_stats():
    engine, _, _, _, capture = build()

    class FailingRecorder:
        def record(self, *args):
            pass

        def close(self):
            raise OSError("simulated disk failure")

    engine.debug_recorder = FailingRecorder()

    stats = engine.run(capture, lambda: False, duration_seconds=1, target_fps=60)

    assert stats.processed_frames == 50
    assert stats.cleanup_failed is False
    assert stats.recorder_error == "OSError: simulated disk failure"


def test_engine_reports_hot_path_stage_percentiles():
    clock = Clock()

    class TimedDetector:
        def detect(self, image, now):
            clock.value += .003
            return [object()]

    class TimedPlanner(Planner):
        def update(self, notes, now):
            clock.value += .004
            return super().update(notes, now)

    class TimedTouch(Touch):
        active_contacts = {2, 4}

        def advance(self, now):
            clock.value += .002

        def dispatch(self, actions):
            clock.value += .008
            super().dispatch(actions)

    class TimedLifeDetector:
        def detect(self, image):
            clock.value += .001
            return LifeReading(True, 800)

    class TimedFeedbackDetector:
        def detect(self, image):
            clock.value += .005
            return None

    class TimedFeedbackController:
        current_offset_ms = 0
        fast_samples = 0
        slow_samples = 0
        valid_samples = 0
        ignored_samples = 0
        ignored_reasons = {}

        def update(self, feedback, now, *, eligible, ignored_reason):
            clock.value += .001
            return None

    class TimedRecorder:
        def record(self, *args):
            clock.value += .007

        def close(self):
            pass

    planner = TimedPlanner()
    engine = RealtimeEngine(
        TimedDetector(),
        planner,
        TimedTouch(),
        clock,
        life_detector=TimedLifeDetector(),
        life_guard=LifeGuard(confirm_frames=1),
        debug_recorder=TimedRecorder(),
        timing_feedback_detector=TimedFeedbackDetector(),
        timing_controller=TimedFeedbackController(),
    )

    def capture():
        clock.value += .020
        return np.zeros((1, 1, 3), dtype=np.uint8)

    stats = engine.run(
        capture,
        lambda: planner.updates == 2,
        duration_seconds=10,
        target_fps=60,
    )

    expected = {
        "capture": 20.0,
        "touch_advance": 2.0,
        "life": 1.0,
        "detector": 3.0,
        "planner": 4.0,
        "timing_feedback": 6.0,
        "recorder_enqueue": 7.0,
        "dispatch": 8.0,
    }
    assert set(stats.stage_timings_ms) == set(expected)
    for stage, milliseconds in expected.items():
        timing = stats.stage_timings_ms[stage]
        assert timing["p50"] == pytest.approx(milliseconds)
        assert timing["p95"] == pytest.approx(milliseconds)
        assert timing["max"] == pytest.approx(milliseconds)
        assert timing["sample_count"] == 2
        assert timing["retained_samples"] == 2
        assert timing["percentile_scope"] == "full_run"


def test_unbounded_listener_uses_a_bounded_recent_stage_window(monkeypatch):
    monkeypatch.setattr("agent.realtime.engine._STAGE_SAMPLE_CAPACITY", 3)
    clock = Clock()
    planner = Planner()
    delays = iter([.1, .02, .02, .02, .02])

    def capture():
        clock.value += next(delays)
        return np.zeros((1, 1, 3), dtype=np.uint8)

    engine = RealtimeEngine(Detector(), planner, Touch(), clock)
    stats = engine.run(
        capture,
        lambda: planner.updates == 5,
        duration_seconds=None,
        target_fps=60,
    )

    capture_timing = stats.stage_timings_ms["capture"]
    assert capture_timing["sample_count"] == 5
    assert capture_timing["retained_samples"] == 3
    assert capture_timing["percentile_scope"] == "recent_window"
    assert capture_timing["p50"] == pytest.approx(20.0)
    assert capture_timing["p95"] == pytest.approx(20.0)
    assert capture_timing["max"] == pytest.approx(100.0)


def test_unbounded_listener_uses_bounded_frame_intervals_but_global_fps(monkeypatch):
    monkeypatch.setattr(
        "agent.realtime.engine._FRAME_INTERVAL_SAMPLE_CAPACITY", 2
    )
    clock = Clock()
    planner = Planner()
    delays = iter([.02, .1, .02, .02, .02])

    def capture():
        clock.value += next(delays)
        return np.zeros((1, 1, 3), dtype=np.uint8)

    engine = RealtimeEngine(Detector(), planner, Touch(), clock)
    stats = engine.run(
        capture,
        lambda: planner.updates == 5,
        duration_seconds=None,
        target_fps=60,
    )

    assert stats.frame_interval_p50_ms == pytest.approx(20.0)
    assert stats.frame_interval_p95_ms == pytest.approx(20.0)
    assert stats.frame_interval_max_ms == pytest.approx(100.0)
    assert stats.effective_fps == pytest.approx(25.0)


def test_engine_keeps_only_the_eight_largest_frame_interval_outliers():
    clock = Clock()
    planner = Planner()

    class ActiveTouch(Touch):
        active_contacts = {2, 4}

    capture_delays = iter([.02, .11, .12, .13, .14, .15, .16, .17, .18, .19, .20])

    def capture():
        clock.value += next(capture_delays)
        return np.zeros((1, 1, 3), dtype=np.uint8)

    engine = RealtimeEngine(Detector(), planner, ActiveTouch(), clock)
    stats = engine.run(
        capture,
        lambda: planner.updates == 11,
        duration_seconds=10,
        target_fps=60,
    )

    assert len(stats.frame_interval_outliers) == 8
    assert [event["interval_ms"] for event in stats.frame_interval_outliers] == [
        pytest.approx(value) for value in (200, 190, 180, 170, 160, 150, 140, 130)
    ]
    largest = stats.frame_interval_outliers[0]
    assert largest == {
        "frame": 10,
        "dominant_stage_frame": 10,
        "elapsed_ms": pytest.approx(1570.0),
        "interval_ms": pytest.approx(200.0),
        "dominant_stage": "capture",
        "dominant_stage_ms": pytest.approx(200.0),
        "unattributed_ms": pytest.approx(0.0),
        "notes": 1,
        "actions": 1,
        "active_contacts": 2,
    }


def test_engine_attributes_next_frame_interval_to_previous_dispatch_stall():
    clock = Clock()

    class VariablePlanner(Planner):
        def update(self, notes, now):
            self.updates += 1
            if self.updates == 1:
                return [
                    TouchAction(ActionKind.TAP, lane, now)
                    for lane in (1, 2, 3)
                ]
            return []

    planner = VariablePlanner()

    class StallingTouch(Touch):
        def dispatch(self, actions):
            if actions and actions[0].kind is ActionKind.TAP and not self.batches:
                clock.value += .2
            super().dispatch(actions)

    def capture():
        clock.value += .02
        return np.zeros((1, 1, 3), dtype=np.uint8)

    engine = RealtimeEngine(Detector(), planner, StallingTouch(), clock)
    stats = engine.run(
        capture,
        lambda: planner.updates == 2,
        duration_seconds=10,
        target_fps=60,
    )

    assert len(stats.frame_interval_outliers) == 1
    outlier = stats.frame_interval_outliers[0]
    assert outlier["frame"] == 1
    assert outlier["interval_ms"] == pytest.approx(220)
    assert outlier["dominant_stage"] == "dispatch"
    assert outlier["dominant_stage_frame"] == 0
    assert outlier["actions"] == 3


def test_engine_attributes_stage_external_clock_gap_as_unattributed():
    engine, clock, planner, _, capture = build()
    jumped = False

    def stopping():
        nonlocal jumped
        if planner.updates == 1 and not jumped:
            clock.value += .25
            jumped = True
        return planner.updates == 2

    stats = engine.run(
        capture,
        stopping,
        duration_seconds=10,
        target_fps=60,
    )

    assert len(stats.frame_interval_outliers) == 1
    outlier = stats.frame_interval_outliers[0]
    assert outlier["interval_ms"] == pytest.approx(270)
    assert outlier["dominant_stage"] == "unattributed"
    assert outlier["unattributed_ms"] == pytest.approx(250)


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


def test_life_safety_callback_failure_becomes_structured_cleanup_failure():
    engine, _, _, touch, capture = build()

    class FallingLife:
        def __init__(self):
            self.frames = 0

        def detect(self, image):
            self.frames += 1
            return LifeReading(True, 800 if self.frames <= 3 else 190)

    engine.life_detector = FallingLife()
    engine.life_guard = LifeGuard(confirm_frames=3)

    def fail_pause(_reading):
        raise RuntimeError("pause overlay did not appear")

    stats = engine.run(
        capture,
        lambda: False,
        duration_seconds=10,
        target_fps=60,
        life_exit_threshold=200,
        on_life_safety=fail_pause,
    )

    assert stats.aborted_for_life
    assert stats.cleanup_failed
    assert stats.cleanup_errors == (
        "life_safety=RuntimeError: pause overlay did not appear",
    )
    assert "实时触控收尾失败" in stats.terminal_reason
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
