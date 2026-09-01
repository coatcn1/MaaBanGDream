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
    def sight(kind: TimingFeedback | None) -> TimingFeedback | None:
        detector = TimingFeedbackDetector()
        result = None
        for _ in range(TimingFeedbackDetector.PERSISTENCE_FRAMES):
            result = detector.detect(feedback_frame(kind))
        return result

    assert sight(TimingFeedback.FAST) is TimingFeedback.FAST
    assert sight(TimingFeedback.SLOW) is TimingFeedback.SLOW
    assert sight(None) is None


def test_detector_rejects_transient_note_flicker():
    # 过线音符在判定条区域只停留 1-3 帧；必须连续存在才上报。
    detector = TimingFeedbackDetector()
    detector.detect(feedback_frame(TimingFeedback.FAST))
    detector.detect(feedback_frame(None))
    detector.detect(feedback_frame(TimingFeedback.FAST))
    detector.detect(feedback_frame(None))
    assert detector.detect(feedback_frame(TimingFeedback.FAST)) is None
    assert detector.detect(feedback_frame(None)) is None


def test_controller_counts_one_visible_label_once_and_adjusts_after_a_streak():
    controller = AdaptiveTimingController(
        10, minimum_samples=5, imbalance=4, adjustment_cooldown_seconds=0,
    )

    assert controller.update(TimingFeedback.SLOW, 0.0) is None
    assert controller.update(TimingFeedback.SLOW, 0.1) is None
    for index in range(1, 5):
        controller.update(None, index)
        changed = controller.update(TimingFeedback.SLOW, index + .1)

    assert changed == 11
    assert controller.current_offset_ms == 11
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


def test_controller_unanimous_window_uses_larger_step():
    controller = AdaptiveTimingController(
        0,
        step_ms=4,
        unanimous_step_ms=10,
        minimum_samples=3,
        imbalance=2,
        window_size=5,
        adjustment_cooldown_seconds=0,
    )
    for sample in range(3):
        controller.update(None, sample)
        controller.update(TimingFeedback.SLOW, sample + .1)
    assert controller.current_offset_ms == 10


def test_controller_mixed_window_uses_small_step():
    controller = AdaptiveTimingController(
        0,
        step_ms=4,
        unanimous_step_ms=10,
        minimum_samples=3,
        imbalance=1,
        window_size=5,
        adjustment_cooldown_seconds=0,
    )
    controller.update(None, 0)
    controller.update(TimingFeedback.FAST, 0.1)
    controller.update(None, 1)
    controller.update(TimingFeedback.SLOW, 1.1)
    controller.update(None, 2)
    controller.update(TimingFeedback.SLOW, 2.1)
    assert controller.current_offset_ms == 4


def test_default_controller_never_moves_more_than_twelve_ms():
    controller = AdaptiveTimingController(20)

    for index in range(200):
        controller.update(None, index * .25)
        controller.update(TimingFeedback.SLOW, index * .25 + .01)

    assert controller.current_offset_ms == 32


def test_ineligible_feedback_is_recorded_but_never_adjusts():
    controller = AdaptiveTimingController(
        0, minimum_samples=3, imbalance=3, adjustment_cooldown_seconds=0,
    )

    for index in range(6):
        controller.update(None, index)
        controller.update(
            TimingFeedback.SLOW,
            index + .1,
            eligible=False,
            ignored_reason="active_hold",
        )

    assert controller.current_offset_ms == 0
    assert controller.valid_samples == 0
    assert controller.ignored_samples == 6
    assert controller.ignored_reasons == {"active_hold": 6}


def test_frozen_controller_keeps_calibrated_offset_under_heavy_feedback():
    controller = AdaptiveTimingController(
        -20,
        maximum_live_adjustment_ms=0,
        minimum_samples=3,
        imbalance=2,
        adjustment_cooldown_seconds=0,
    )

    for index in range(20):
        controller.update(None, index)
        controller.update(TimingFeedback.FAST, index + .01)

    assert controller.current_offset_ms == -20
    assert controller.fast_samples == 20
