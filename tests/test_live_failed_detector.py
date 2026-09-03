from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from agent.realtime.live_failed_detector import (
    LiveFailedPopupDetector,
    PROJECT_ROOT,
)


TEMPLATE_PATH = PROJECT_ROOT / "resource" / "image" / "live_failed_continue.png"


def _frame_with_button_at(x: int, y: int) -> np.ndarray:
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    template = cv2.imread(str(TEMPLATE_PATH), cv2.IMREAD_COLOR)
    assert template is not None
    height, width = template.shape[:2]
    frame[y:y + height, x:x + width] = template
    return frame


def test_detector_confirms_popup_inside_roi_after_debounce():
    detector = LiveFailedPopupDetector()
    frame = _frame_with_button_at(788, 422)
    assert detector.observe(frame, 0.0) is False
    assert detector.observe(frame, 0.2) is False
    assert detector.observe(frame, 0.4) is True
    assert detector.triggered is True


def test_detector_ignores_button_outside_roi():
    detector = LiveFailedPopupDetector()
    frame = _frame_with_button_at(400, 422)
    for offset in (0.0, 0.2, 0.4, 0.6, 0.8):
        assert detector.observe(frame, offset) is False
    assert detector.triggered is False


def test_detector_ignores_plain_playfield():
    detector = LiveFailedPopupDetector()
    frame = np.full((720, 1280, 3), 24, dtype=np.uint8)
    for offset in (0.0, 0.2, 0.4, 0.6, 0.8):
        assert detector.observe(frame, offset) is False
    assert detector.triggered is False
