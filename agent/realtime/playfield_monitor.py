"""演奏场存在性与低频终态监控。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import cv2
import numpy as np

from .life_monitor import LifePresenceDetector


LANE_CENTERS = (190, 340, 490, 640, 790, 940, 1090)


class PlayfieldDetector:
    """同时确认生命条控件与至少六轨白色判定标记。"""

    _REFERENCE_WIDTH = 1280
    _REFERENCE_HEIGHT = 720
    _LINE_TOP = 570
    _LINE_BOTTOM = 611
    _LANE_HALF_WIDTH = 40
    _MIN_WHITE_PIXELS = 200

    def __init__(self) -> None:
        self._life_presence = LifePresenceDetector()

    def __call__(self, image: Any) -> bool:
        if (
            not isinstance(image, np.ndarray)
            or image.ndim != 3
            or image.shape[2] < 3
            or not self._life_presence.detect(image)
        ):
            return False
        height, width = image.shape[:2]
        x_scale = width / self._REFERENCE_WIDTH
        y_scale = height / self._REFERENCE_HEIGHT
        top = max(0, min(height - 1, round(self._LINE_TOP * y_scale)))
        bottom = max(top + 1, min(height, round(self._LINE_BOTTOM * y_scale)))
        minimum = max(
            50,
            round(self._MIN_WHITE_PIXELS * x_scale * y_scale),
        )
        visible_lanes = 0
        for center in LANE_CENTERS:
            lane_x = round(center * x_scale)
            half_width = max(1, round(self._LANE_HALF_WIDTH * x_scale))
            left = max(0, lane_x - half_width)
            right = min(width, lane_x + half_width + 1)
            hsv = cv2.cvtColor(
                image[top:bottom, left:right, :3],
                cv2.COLOR_BGR2HSV,
            )
            white_pixels = int(
                ((hsv[:, :, 1] < 70) & (hsv[:, :, 2] > 170)).sum()
            )
            if white_pixels >= minimum:
                visible_lanes += 1
        return visible_lanes >= 6


class PlayfieldLifecycleMonitor:
    """启动阶段逐帧确认，开演后约 5Hz 检查演奏场是否结束。"""

    def __init__(
        self,
        *,
        detector: Callable[[Any], bool] | None = None,
        confirm_checks: int = 2,
        missing_checks: int | None = 10,
        active_check_interval_seconds: float = 0.2,
    ) -> None:
        if confirm_checks < 1:
            raise ValueError("confirm_checks 必须大于 0")
        if missing_checks is not None and missing_checks < 1:
            raise ValueError("missing_checks 必须大于 0 或为 None")
        if active_check_interval_seconds < 0:
            raise ValueError("active_check_interval_seconds 不能为负数")
        self.detector = detector or PlayfieldDetector()
        self.confirm_checks = int(confirm_checks)
        self.missing_checks = (
            None if missing_checks is None else int(missing_checks)
        )
        self.active_check_interval_seconds = float(
            active_check_interval_seconds
        )
        self.active = False
        self.completed = False
        self.visible_streak = 0
        self.missing_streak = 0
        self.next_active_check_at = float("-inf")
        self.checks = 0

    def mark_active(self, now: float) -> None:
        """Native 首音门控已证明演奏场时，直接接管低频终态检查。"""
        self.active = True
        self.visible_streak = self.confirm_checks
        self.missing_streak = 0
        self.next_active_check_at = (
            float(now) + self.active_check_interval_seconds
        )

    def observe(self, image: Any, now: float) -> str:
        if self.completed:
            return "completed"
        timestamp = float(now)
        if self.active and timestamp < self.next_active_check_at:
            return "missing" if self.missing_streak > 0 else "active"
        visible = bool(self.detector(image))
        self.checks += 1
        if not self.active:
            self.visible_streak = self.visible_streak + 1 if visible else 0
            if self.visible_streak < self.confirm_checks:
                return "waiting"
            self.active = True
            self.missing_streak = 0
            self.next_active_check_at = (
                timestamp + self.active_check_interval_seconds
            )
            return "active"
        self.next_active_check_at = (
            timestamp + self.active_check_interval_seconds
        )
        if visible:
            self.missing_streak = 0
            return "active"
        if self.missing_checks is None:
            return "active"
        self.missing_streak += 1
        if self.missing_streak >= self.missing_checks:
            self.completed = True
            return "completed"
        return "missing"
