from __future__ import annotations

import cv2
import numpy as np

from agent.realtime.life_monitor import LifeDetector, LifeGuard, LifeReading, LifeStatus


def life_frame(value: int | None):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    if value is None:
        return image
    cv2.rectangle(image, (942, 35), (964, 51), (80, 220, 40), -1)
    cv2.rectangle(image, (968, 29), (1184, 55), (210, 210, 210), 2)
    width = round(212 * value / 1000)
    if width:
        cv2.rectangle(image, (970, 32), (970 + width - 1, 52), (80, 220, 40), -1)
    return image


def test_life_detector_reads_maa_bgr_frames():
    detector = LifeDetector()
    for expected in (1000, 800, 300, 100, 0):
        reading = detector.detect(life_frame(expected))
        assert reading.visible
        assert abs(reading.value - expected) <= 30


def test_life_guard_requires_three_visible_zero_frames():
    guard = LifeGuard()
    guard.update(LifeReading(True, 1000))
    guard.update(LifeReading(True, 1000))
    assert guard.update(LifeReading(True, 1000)) is LifeStatus.NORMAL
    guard.update(LifeReading(True, 0))
    guard.update(LifeReading(True, 0))
    assert guard.status is not LifeStatus.DEAD
    assert guard.update(LifeReading(True, 0)) is LifeStatus.DEAD


def test_missing_life_bar_is_unknown_not_dead():
    guard = LifeGuard()
    for _ in range(5):
        guard.update(LifeReading(False))
    assert guard.status is LifeStatus.UNKNOWN
