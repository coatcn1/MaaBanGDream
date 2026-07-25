from __future__ import annotations

import cv2
import numpy as np

from agent.realtime.life_monitor import (
    LifeDetector,
    LifeGuard,
    LifeReading,
    LifeStatus,
    PlayfieldCompletionGuard,
)


def life_frame(value: int | None, *, fill_hsv=(64, 209, 220)):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    if value is None:
        return image
    cv2.rectangle(image, (942, 35), (964, 51), (80, 220, 40), -1)
    cv2.rectangle(image, (968, 29), (1184, 55), (210, 210, 210), 2)
    width = round(212 * value / 1000)
    if width:
        fill = cv2.cvtColor(
            np.uint8([[fill_hsv]]), cv2.COLOR_HSV2BGR
        )[0, 0].tolist()
        cv2.rectangle(image, (970, 32), (970 + width - 1, 52), fill, -1)
    return image


def test_life_detector_reads_maa_bgr_frames():
    detector = LifeDetector()
    for expected in (1000, 800, 300, 100, 0):
        reading = detector.detect(life_frame(expected))
        assert reading.visible
        assert abs(reading.value - expected) <= 30


def test_life_detector_reads_low_life_yellow_green_fill():
    reading = LifeDetector().detect(
        life_frame(250, fill_hsv=(25, 161, 188))
    )

    assert reading.visible
    assert 220 <= reading.value <= 280


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


def test_zero_readings_cannot_kill_before_alive_life_was_confirmed():
    guard = LifeGuard(confirm_frames=3)

    for _ in range(10):
        assert guard.update(LifeReading(True, 0)) is not LifeStatus.DEAD


def test_zero_readings_kill_after_alive_life_was_confirmed():
    guard = LifeGuard(confirm_frames=3)
    for _ in range(3):
        guard.update(LifeReading(True, 800))
    for _ in range(2):
        assert guard.update(LifeReading(True, 0)) is not LifeStatus.DEAD
    assert guard.update(LifeReading(True, 0)) is LifeStatus.DEAD


def test_playfield_completion_requires_alive_then_sustained_disappearance():
    guard = PlayfieldCompletionGuard(missing_frames=3)
    for _ in range(5):
        assert not guard.update(LifeReading(False), alive_confirmed=False)
    assert not guard.update(LifeReading(False), alive_confirmed=True)
    assert not guard.update(LifeReading(False), alive_confirmed=True)
    assert guard.update(LifeReading(False), alive_confirmed=True)


def test_visible_life_resets_playfield_completion_streak():
    guard = PlayfieldCompletionGuard(missing_frames=3)
    guard.update(LifeReading(False), alive_confirmed=True)
    guard.update(LifeReading(False), alive_confirmed=True)
    assert not guard.update(LifeReading(True, 500), alive_confirmed=True)
    assert not guard.update(LifeReading(False), alive_confirmed=True)
