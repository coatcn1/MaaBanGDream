from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from .controller_touch import ControllerTouchDispatcher
from .note_detector import NoteDetector
from .life_monitor import LifeDetector, LifeGuard, LifeStatus, PlayfieldCompletionGuard
from .timing_feedback import AdaptiveTimingController, TimingFeedbackDetector
from .touch_planner import RealtimePlanner


class DebugRecorder:
    def record(
        self, image, timestamp, notes, actions, life_status,
        diagnostics=None, timing_state=None,
    ): ...
    def close(self): ...


@dataclass(frozen=True)
class EngineStats:
    processed_frames: int
    dispatched_actions: int
    stopped: bool
    aborted_for_life: bool = False
    completed: bool = False
    life_depleted: bool = False
    timing_feedback_fast: int = 0
    timing_feedback_slow: int = 0
    initial_timing_offset_ms: int = 0
    final_timing_offset_ms: int = 0
    timing_feedback_valid: int = 0
    timing_feedback_ignored: int = 0
    timing_feedback_ignored_reasons: dict[str, int] = field(default_factory=dict)
    filtered_adjacent_artifacts: int = 0
    rejected_hold_candidates: int = 0


class RealtimeEngine:
    """Own the realtime lifecycle and guarantee touch cleanup on every exit."""

    def __init__(
        self,
        detector: NoteDetector,
        planner: RealtimePlanner,
        touch: ControllerTouchDispatcher,
        clock: Callable[[], float] = time.monotonic,
        life_detector: LifeDetector | None = None,
        life_guard: LifeGuard | None = None,
        completion_guard: PlayfieldCompletionGuard | None = None,
        debug_recorder: DebugRecorder | None = None,
        timing_feedback_detector: TimingFeedbackDetector | None = None,
        timing_controller: AdaptiveTimingController | None = None,
    ) -> None:
        self.detector = detector
        self.planner = planner
        self.touch = touch
        self.clock = clock
        self.life_detector = life_detector
        self.life_guard = life_guard
        self.completion_guard = completion_guard
        self.debug_recorder = debug_recorder
        self.timing_feedback_detector = timing_feedback_detector
        self.timing_controller = timing_controller

    def run(
        self,
        capture: Callable[[], np.ndarray],
        stopping: Callable[[], bool],
        *,
        duration_seconds: float,
        target_fps: int,
        continue_after_life_depleted: bool = False,
        life_exit_threshold: int | None = None,
        on_life_safety: Callable[[object], None] | None = None,
    ) -> EngineStats:
        if not 1 <= duration_seconds <= 600:
            raise ValueError("duration_seconds 必须在 1..600 之间")
        if not 15 <= target_fps <= 120:
            raise ValueError("target_fps 必须在 15..120 之间")
        interval = 1 / target_fps
        deadline = self.clock() + duration_seconds
        next_frame = self.clock()
        frames = actions_count = 0
        was_stopped = False
        aborted_for_life = False
        completed = False
        life_depleted = False
        below_threshold_streak = 0
        safety_reading = None
        initial_timing_offset_ms = int(
            getattr(self.planner, "timing_offset_ms", 0)
        )
        last_transient_action_at = float("-inf")
        hold_feedback_block_until = float("-inf")
        try:
            while self.clock() < deadline:
                if stopping():
                    was_stopped = True
                    break
                image = capture()
                now = self.clock()
                if stopping():
                    was_stopped = True
                    break
                if now < next_frame:
                    continue
                next_frame += interval
                if now - next_frame > interval:
                    next_frame = now + interval
                life_status = None
                if self.life_detector is not None and self.life_guard is not None:
                    reading = self.life_detector.detect(image)
                    status = self.life_guard.update(reading)
                    life_status = status.value
                    if (
                        self.completion_guard is not None
                        and self.completion_guard.update(
                            reading, alive_confirmed=self.life_guard.alive_confirmed
                        )
                    ):
                        completed = True
                        break
                    if (
                        life_exit_threshold is not None
                        and self.life_guard.alive_confirmed
                        and reading.visible
                    ):
                        below_threshold_streak = (
                            below_threshold_streak + 1
                            if reading.value < life_exit_threshold else 0
                        )
                        if below_threshold_streak >= 3:
                            life_depleted = life_depleted or status is LifeStatus.DEAD
                            aborted_for_life = True
                            safety_reading = reading
                            break
                    elif not reading.visible:
                        # The default value of an invisible reading is zero.
                        # Song-end fades must contribute to completion, not to
                        # the low-life debounce.
                        below_threshold_streak = 0
                    if status is LifeStatus.DEAD:
                        life_depleted = True
                        if not continue_after_life_depleted:
                            aborted_for_life = True
                            break
                    # A custom action can start during a transition or on a
                    # non-playfield screen. Never interpret those pixels as
                    # notes until a non-zero life bar has been confirmed.
                    if not self.life_guard.alive_confirmed:
                        frames += 1
                        continue
                notes = self.detector.detect(image, now)
                actions = self.planner.update(notes, now)
                if any(
                    action.kind.value in {"tap", "flick"} for action in actions
                ):
                    last_transient_action_at = now
                if any(action.kind.value == "up" for action in actions):
                    hold_feedback_block_until = max(
                        hold_feedback_block_until, now + .4
                    )
                if (
                    self.timing_feedback_detector is not None
                    and self.timing_controller is not None
                ):
                    feedback = self.timing_feedback_detector.detect(image)
                    if self.planner.has_active_holds:
                        eligible = False
                        ignored_reason = "active_hold"
                    elif now < hold_feedback_block_until:
                        eligible = False
                        ignored_reason = "recent_hold_release"
                    elif now - last_transient_action_at > .6:
                        eligible = False
                        ignored_reason = "no_recent_transient_input"
                    else:
                        eligible = True
                        ignored_reason = ""
                    adjusted = self.timing_controller.update(
                        feedback,
                        now,
                        eligible=eligible,
                        ignored_reason=ignored_reason,
                    )
                    if adjusted is not None:
                        self.planner.set_timing_offset_ms(adjusted)
                        print(
                            "RealtimeTimingAdjust "
                            f"fast={self.timing_controller.fast_samples} "
                            f"slow={self.timing_controller.slow_samples} "
                            f"offset={adjusted}ms",
                            flush=True,
                        )
                diagnostics = self.planner.drain_diagnostics()
                if self.debug_recorder is not None:
                    timing_state = (
                        {
                            "initial_offset_ms": initial_timing_offset_ms,
                            "current_offset_ms": self.timing_controller.current_offset_ms,
                            "valid_samples": self.timing_controller.valid_samples,
                            "ignored_samples": self.timing_controller.ignored_samples,
                            "ignored_reasons": self.timing_controller.ignored_reasons,
                        }
                        if self.timing_controller is not None else {}
                    )
                    self.debug_recorder.record(
                        image, now, notes, actions, life_status,
                        diagnostics, timing_state,
                    )
                if actions:
                    self.touch.dispatch(actions)
                    actions_count += len(actions)
                frames += 1
            return EngineStats(
                frames, actions_count, was_stopped, aborted_for_life, completed,
                life_depleted,
                (
                    self.timing_controller.fast_samples
                    if self.timing_controller is not None else 0
                ),
                (
                    self.timing_controller.slow_samples
                    if self.timing_controller is not None else 0
                ),
                initial_timing_offset_ms,
                (
                    self.timing_controller.current_offset_ms
                    if self.timing_controller is not None
                    else int(getattr(self.planner, "timing_offset_ms", 0))
                ),
                (
                    self.timing_controller.valid_samples
                    if self.timing_controller is not None else 0
                ),
                (
                    self.timing_controller.ignored_samples
                    if self.timing_controller is not None else 0
                ),
                (
                    self.timing_controller.ignored_reasons
                    if self.timing_controller is not None else {}
                ),
                int(getattr(self.planner, "filtered_adjacent_artifacts", 0)),
                int(getattr(self.planner, "rejected_hold_candidates", 0)),
            )
        finally:
            cleanup = self.planner.reset(self.clock())
            try:
                if cleanup:
                    self.touch.dispatch(cleanup)
            finally:
                try:
                    self.touch.close()
                finally:
                    try:
                        if on_life_safety is not None and safety_reading is not None:
                            on_life_safety(safety_reading)
                    finally:
                        if self.debug_recorder is not None:
                            self.debug_recorder.close()
