from __future__ import annotations

from collections import deque
from enum import Enum

import cv2
import numpy as np


class TimingFeedback(str, Enum):
    FAST = "fast"
    SLOW = "slow"


class TimingFeedbackDetector:
    """Read the coloured FAST/SLOW bar below the centred judgement text."""

    ROI = (555, 525, 725, 570)
    MIN_COLOURED_PIXELS = 1000

    def detect(self, image: np.ndarray) -> TimingFeedback | None:
        if not isinstance(image, np.ndarray) or image.shape[:2] != (720, 1280):
            return None
        x1, y1, x2, y2 = self.ROI
        hsv = cv2.cvtColor(image[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
        slow = int(np.count_nonzero(cv2.inRange(
            hsv, (3, 140, 140), (25, 255, 255),
        )))
        fast = int(np.count_nonzero(cv2.inRange(
            hsv, (95, 100, 150), (115, 255, 255),
        )))
        if slow >= self.MIN_COLOURED_PIXELS and slow > fast:
            return TimingFeedback.SLOW
        if fast >= self.MIN_COLOURED_PIXELS and fast > slow:
            return TimingFeedback.FAST
        return None


class AdaptiveTimingController:
    """Apply bounded in-song timing correction from debounced feedback."""

    def __init__(
        self,
        initial_offset_ms: int,
        *,
        step_ms: int = 2,
        minimum_samples: int = 5,
        imbalance: int = 4,
        window_size: int = 7,
        maximum_live_adjustment_ms: int = 50,
        adjustment_cooldown_seconds: float = 1.0,
    ) -> None:
        self.initial_offset_ms = int(initial_offset_ms)
        self.current_offset_ms = int(initial_offset_ms)
        self.step_ms = int(step_ms)
        self.minimum_samples = int(minimum_samples)
        self.imbalance = int(imbalance)
        self.maximum_live_adjustment_ms = int(maximum_live_adjustment_ms)
        self.adjustment_cooldown_seconds = float(adjustment_cooldown_seconds)
        self.fast_samples = 0
        self.slow_samples = 0
        self._samples: deque[TimingFeedback] = deque(maxlen=int(window_size))
        self._visible: TimingFeedback | None = None
        self._last_adjusted_at = float("-inf")

    def update(
        self,
        feedback: TimingFeedback | None,
        now: float,
    ) -> int | None:
        if feedback is None:
            self._visible = None
            return None
        if feedback == self._visible:
            return None
        self._visible = feedback
        self._samples.append(feedback)
        if feedback is TimingFeedback.FAST:
            self.fast_samples += 1
        else:
            self.slow_samples += 1

        if len(self._samples) < self.minimum_samples:
            return None
        error = (
            sum(sample is TimingFeedback.SLOW for sample in self._samples)
            - sum(sample is TimingFeedback.FAST for sample in self._samples)
        )
        if abs(error) < self.imbalance:
            return None
        if now - self._last_adjusted_at < self.adjustment_cooldown_seconds:
            return None

        direction = 1 if error > 0 else -1
        lower = max(-250, self.initial_offset_ms - self.maximum_live_adjustment_ms)
        upper = min(250, self.initial_offset_ms + self.maximum_live_adjustment_ms)
        adjusted = max(
            lower,
            min(upper, self.current_offset_ms + direction * self.step_ms),
        )
        self._samples.clear()
        if adjusted == self.current_offset_ms:
            return None
        self.current_offset_ms = adjusted
        self._last_adjusted_at = float(now)
        return adjusted
