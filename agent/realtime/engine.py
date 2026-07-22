from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from .controller_touch import ControllerTouchDispatcher
from .note_detector import NoteDetector
from .touch_planner import RealtimePlanner


@dataclass(frozen=True)
class EngineStats:
    processed_frames: int
    dispatched_actions: int
    stopped: bool


class RealtimeEngine:
    """Own the realtime lifecycle and guarantee touch cleanup on every exit."""

    def __init__(
        self,
        detector: NoteDetector,
        planner: RealtimePlanner,
        touch: ControllerTouchDispatcher,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.detector = detector
        self.planner = planner
        self.touch = touch
        self.clock = clock

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
                notes = self.detector.detect(image, now)
                actions = self.planner.update(notes, now)
                if actions:
                    self.touch.dispatch(actions)
                    actions_count += len(actions)
                frames += 1
            return EngineStats(frames, actions_count, was_stopped)
        finally:
            cleanup = self.planner.reset(self.clock())
            try:
                if cleanup:
                    self.touch.dispatch(cleanup)
            finally:
                self.touch.close()
