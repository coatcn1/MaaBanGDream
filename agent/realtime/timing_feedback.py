from __future__ import annotations

from collections import Counter, deque
from enum import Enum

import cv2
import numpy as np


class TimingFeedback(str, Enum):
    FAST = "fast"
    SLOW = "slow"


class TimingFeedbackDetector:
    """Read the coloured FAST/SLOW bar below the centred judgement text."""

    # 判定条实测几何：位于判定文字正下方（GREAT 下方约 y 514-556），
    # FAST 为亮天蓝（H≈102-110, S≈220），SLOW 为亮橙（H≈11, S≈247）。
    # 旧 ROI 过宽且 FAST 饱和度上限 150，把过线蓝音符当成判定条。
    ROI = (570, 514, 710, 556)
    MIN_COLOURED_PIXELS = 600
    # 过线音符在条区域只停留 1-3 帧，判定条则持续约 10-20 帧。
    # 要求信号连续存在足够帧数，避免把运动音符误报为判定条。
    PERSISTENCE_FRAMES = 3

    def __init__(self) -> None:
        self._streak_kind: TimingFeedback | None = None
        self._streak = 0
        # 观测计数：sightings = 任一帧出现过判定条信号；reports = 通过
        # 持续帧数门禁后实际上报的次数。用于诊断实机检测覆盖率。
        self.sightings = 0
        self.reports = 0

    def detect(self, image: np.ndarray) -> TimingFeedback | None:
        if not isinstance(image, np.ndarray) or image.shape[:2] != (720, 1280):
            return None
        x1, y1, x2, y2 = self.ROI
        hsv = cv2.cvtColor(image[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
        slow = int(np.count_nonzero(cv2.inRange(
            hsv, (0, 140, 160), (25, 255, 255),
        )))
        fast = int(np.count_nonzero(cv2.inRange(
            hsv, (95, 160, 160), (120, 255, 255),
        )))
        kind: TimingFeedback | None = None
        if slow >= self.MIN_COLOURED_PIXELS and slow >= fast * 2:
            kind = TimingFeedback.SLOW
        elif fast >= self.MIN_COLOURED_PIXELS and fast >= slow * 2:
            kind = TimingFeedback.FAST
        if kind != self._streak_kind:
            self._streak_kind = kind
            self._streak = 1
        else:
            self._streak += 1
        if kind is not None:
            self.sightings += 1
        # 只在信号首次达到持续帧数时返回一次，避免同一判定条重复计数。
        if kind is not None and self._streak == self.PERSISTENCE_FRAMES:
            self.reports += 1
            return kind
        return None


class AdaptiveTimingController:
    """Apply bounded in-song timing correction from debounced feedback."""

    def __init__(
        self,
        initial_offset_ms: int,
        *,
        step_ms: int = 1,
        unanimous_step_ms: int | None = None,
        minimum_samples: int = 12,
        imbalance: int = 8,
        window_size: int = 16,
        maximum_live_adjustment_ms: int = 12,
        adjustment_cooldown_seconds: float = 2.0,
    ) -> None:
        self.initial_offset_ms = int(initial_offset_ms)
        self.current_offset_ms = int(initial_offset_ms)
        self.step_ms = int(step_ms)
        self.unanimous_step_ms = (
            None if unanimous_step_ms is None else int(unanimous_step_ms)
        )
        self.minimum_samples = int(minimum_samples)
        self.imbalance = int(imbalance)
        self.maximum_live_adjustment_ms = int(maximum_live_adjustment_ms)
        self.adjustment_cooldown_seconds = float(adjustment_cooldown_seconds)
        self.fast_samples = 0
        self.slow_samples = 0
        self.valid_samples = 0
        self.ignored_samples = 0
        self._ignored_reasons: Counter[str] = Counter()
        self._samples: deque[TimingFeedback] = deque(maxlen=int(window_size))
        self._visible: TimingFeedback | None = None
        self._last_adjusted_at = float("-inf")

    def update(
        self,
        feedback: TimingFeedback | None,
        now: float,
        *,
        eligible: bool = True,
        ignored_reason: str = "ineligible",
    ) -> int | None:
        if feedback is None:
            self._visible = None
            return None
        if feedback == self._visible:
            return None
        self._visible = feedback
        if not eligible:
            self.ignored_samples += 1
            self._ignored_reasons[ignored_reason] += 1
            return None
        self.valid_samples += 1
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
        # 窗口内全部同向说明信号一致，可放大步长；混合窗口用小步长，
        # 降低个别误检把偏移推反的风险。
        step = self.step_ms
        if (
            self.unanimous_step_ms is not None
            and abs(error) == len(self._samples)
        ):
            step = self.unanimous_step_ms
        lower = max(-250, self.initial_offset_ms - self.maximum_live_adjustment_ms)
        upper = min(250, self.initial_offset_ms + self.maximum_live_adjustment_ms)
        adjusted = max(
            lower,
            min(upper, self.current_offset_ms + direction * step),
        )
        self._samples.clear()
        if adjusted == self.current_offset_ms:
            return None
        self.current_offset_ms = adjusted
        self._last_adjusted_at = float(now)
        return adjusted

    @property
    def ignored_reasons(self) -> dict[str, int]:
        return dict(self._ignored_reasons)
