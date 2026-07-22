from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from .controller_touch import ControllerTouchDispatcher
from .note_detector import NoteDetector
from .life_monitor import LifeDetector, LifeGuard, LifeStatus, PlayfieldCompletionGuard
from .touch_planner import RealtimePlanner


class DebugRecorder:
    def record(self, image, timestamp, notes, actions, life_status): ...
    def close(self): ...


@dataclass(frozen=True)
class EngineStats:
    processed_frames: int
    dispatched_actions: int
    stopped: bool
    aborted_for_life: bool = False
    completed: bool = False


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
    ) -> None:
        self.detector = detector
        self.planner = planner
        self.touch = touch
        self.clock = clock
        self.life_detector = life_detector
        self.life_guard = life_guard
        self.completion_guard = completion_guard
        self.debug_recorder = debug_recorder

    def run(
        self,
        capture: Callable[[], np.ndarray],
        stopping: Callable[[], bool],
        *,
        duration_seconds: float,
        target_fps: int,
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
                    if status is LifeStatus.DEAD:
                        aborted_for_life = True
                        break
                    if (
                        self.completion_guard is not None
                        and self.completion_guard.update(
                            reading, alive_confirmed=self.life_guard.alive_confirmed
                        )
                    ):
                        completed = True
                        break
                    # A custom action can start during a transition or on a
                    # non-playfield screen. Never interpret those pixels as
                    # notes until a non-zero life bar has been confirmed.
                    if not self.life_guard.alive_confirmed:
                        frames += 1
                        continue
                notes = self.detector.detect(image, now)
                actions = self.planner.update(notes, now)
                if self.debug_recorder is not None:
                    self.debug_recorder.record(image, now, notes, actions, life_status)
                if actions:
                    self.touch.dispatch(actions)
                    actions_count += len(actions)
                frames += 1
            return EngineStats(
                frames, actions_count, was_stopped, aborted_for_life, completed
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
                    if self.debug_recorder is not None:
                        self.debug_recorder.close()
