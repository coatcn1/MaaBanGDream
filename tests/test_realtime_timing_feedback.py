from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from agent.realtime.timing_feedback import (
    AdaptiveTimingController,
    TimingFeedback,
    TimingFeedbackDetector,
)

FIXTURES = Path(__file__).parent / "fixtures"


def feedback_frame(kind: TimingFeedback | None) -> np.ndarray:
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    if kind is None:
        return image
    name = "timing_feedback_slow.png" if kind is TimingFeedback.SLOW else (
        "timing_feedback_fast.png"
    )
    crop = cv2.imread(str(FIXTURES / name))
    assert crop is not None
    image[525:570, 555:725] = crop
    return image


def test_detector_distinguishes_fast_slow_and_no_feedback():
    detector = TimingFeedbackDetector()

    assert detector.detect(feedback_frame(TimingFeedback.FAST)) is TimingFeedback.FAST
    assert detector.detect(feedback_frame(TimingFeedback.SLOW)) is TimingFeedback.SLOW
    assert detector.detect(feedback_frame(None)) is None


def test_controller_counts_one_visible_label_once_and_adjusts_after_a_streak():
    controller = AdaptiveTimingController(10, minimum_samples=5, imbalance=4)

    assert controller.update(TimingFeedback.SLOW, 0.0) is None
    assert controller.update(TimingFeedback.SLOW, 0.1) is None
    for index in range(1, 5):
        controller.update(None, index)
        changed = controller.update(TimingFeedback.SLOW, index + .1)

    assert changed == 12
    assert controller.current_offset_ms == 12
    assert controller.slow_samples == 5
    assert controller.fast_samples == 0


def test_controller_reverses_for_fast_and_clamps_live_adjustment():
    controller = AdaptiveTimingController(
        0,
        step_ms=5,
        minimum_samples=3,
        imbalance=3,
        maximum_live_adjustment_ms=10,
        adjustment_cooldown_seconds=0,
    )

    for group in range(4):
        for sample in range(3):
            controller.update(None, group * 10 + sample)
            controller.update(TimingFeedback.FAST, group * 10 + sample + .1)

    assert controller.current_offset_ms == -10
    assert controller.fast_samples == 12
