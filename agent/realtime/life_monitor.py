from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import cv2
import numpy as np


class LifeStatus(str, Enum):
    UNKNOWN = "unknown"
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"
    DEAD = "dead"


@dataclass(frozen=True)
class LifeReading:
    visible: bool
    value: int = 0


class LifeDetector:
    ROI = (965, 24, 1190, 82)
    BAR = (970, 32, 1182, 53)

    def detect(self, image: np.ndarray) -> LifeReading:
        x1, y1, x2, y2 = self.ROI
        if image.shape[0] < y2 or image.shape[1] < x2:
            return LifeReading(False)
        roi = image[y1:y2, x1:x2]
        icon = image[29:58, 936:970]
        icon_hsv = cv2.cvtColor(icon, cv2.COLOR_BGR2HSV)
        if np.count_nonzero(cv2.inRange(icon_hsv, (35, 70, 55), (95, 255, 255))) < 30:
            return LifeReading(False)
        if np.count_nonzero(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) > 160) < 100:
            return LifeReading(False)
        bx1, by1, bx2, by2 = self.BAR
        bar = image[by1:by2, bx1:bx2]
        hsv = cv2.cvtColor(bar, cv2.COLOR_BGR2HSV)
        # The fill shifts from green to yellow-green as life falls. The
        # previous lower hue bound (35) turned a real 250/1000 bar (H≈25)
        # into zero and caused a false safety abort.
        green = cv2.inRange(hsv, (20, 40, 55), (95, 255, 255))
        columns = np.count_nonzero(green, axis=0) >= max(3, bar.shape[0] // 5)
        filled = 0
        for present in columns:
            if not present:
                break
            filled += 1
        return LifeReading(True, min(1000, max(0, round(1000 * filled / bar.shape[1]))))


class LifeGuard:
    """Require consecutive visible frames before declaring a dangerous state."""

    def __init__(self, warning=300, critical=100, confirm_frames=3) -> None:
        self.warning = warning
        self.critical = critical
        self.confirm_frames = confirm_frames
        self.visible_streak = 0
        self.alive_streak = 0
        self.alive_confirmed = False
        self.zero_streak = 0
        self.status = LifeStatus.UNKNOWN
        self.minimum: int | None = None

    def update(self, reading: LifeReading) -> LifeStatus:
        if not reading.visible:
            self.visible_streak = self.alive_streak = self.zero_streak = 0
            self.status = LifeStatus.UNKNOWN
            return self.status
        self.visible_streak += 1
        if reading.value >= 20:
            self.alive_streak += 1
            if self.alive_streak >= self.confirm_frames:
                self.alive_confirmed = True
        else:
            self.alive_streak = 0
        if self.visible_streak < self.confirm_frames:
            return self.status
        self.minimum = reading.value if self.minimum is None else min(self.minimum, reading.value)
        self.zero_streak = self.zero_streak + 1 if reading.value < 20 else 0
        if self.alive_confirmed and self.zero_streak >= self.confirm_frames:
            self.status = LifeStatus.DEAD
        elif reading.value < self.critical:
            self.status = LifeStatus.CRITICAL
        elif reading.value < self.warning:
            self.status = LifeStatus.WARNING
        else:
            self.status = LifeStatus.NORMAL
        return self.status


class PlayfieldCompletionGuard:
    """Confirm that an active playfield disappeared at the end of a song."""

    def __init__(self, missing_frames: int = 120) -> None:
        if not 3 <= missing_frames <= 600:
            raise ValueError("missing_frames 必须在 3..600 之间")
        self.missing_frames = int(missing_frames)
        self.streak = 0

    def update(self, reading: LifeReading, *, alive_confirmed: bool) -> bool:
        if not alive_confirmed:
            self.streak = 0
            return False
        self.streak = 0 if reading.visible else self.streak + 1
        return self.streak >= self.missing_frames
