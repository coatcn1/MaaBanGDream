from __future__ import annotations

import numpy as np

from agent.realtime.frame_observer import LatestFrameObserver


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


def test_observer_counts_valid_frames_and_never_controls_device():
    clock = Clock()
    controls = []

    def capture():
        clock.value += 0.02
        return np.zeros((720, 1280, 3), dtype=np.uint8)

    stats = LatestFrameObserver(clock).run(
        capture, lambda: False, duration_seconds=0.1, frame_timeout_ms=150
    )

    assert stats.frames == 5
    assert round(stats.effective_fps) == 50
    assert controls == []
    assert stats.timed_out_frames == 0
    assert not stats.stopped


def test_observer_stops_before_requesting_another_frame():
    clock = Clock()
    captures = 0

    def capture():
        nonlocal captures
        captures += 1
        clock.value += 0.02
        return np.zeros((1, 1, 3), dtype=np.uint8)

    stats = LatestFrameObserver(clock).run(
        capture, lambda: captures == 2, duration_seconds=1, frame_timeout_ms=150
    )

    assert captures == 2
    assert stats.stopped


def test_observer_rejects_invalid_frames_without_retaining_them():
    clock = Clock()

    def capture():
        clock.value += 0.05
        return np.array([])

    stats = LatestFrameObserver(clock).run(
        capture, lambda: False, duration_seconds=0.1, frame_timeout_ms=150
    )

    assert stats.frames == 0
    assert stats.invalid_frames == 2


def test_observer_reports_capture_deadline_misses():
    clock = Clock()

    def capture():
        clock.value += 0.2
        return np.zeros((1, 1, 3), dtype=np.uint8)

    stats = LatestFrameObserver(clock).run(
        capture, lambda: False, duration_seconds=0.2, frame_timeout_ms=150
    )

    assert stats.frames == 1
    assert stats.timed_out_frames == 1
