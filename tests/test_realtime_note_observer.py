from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from agent.realtime.note_detector import NoteKind, ObservedNote
from agent.realtime.note_observer import NoteObserver


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


@dataclass
class Detector:
    calls: int = 0

    def detect(self, image, timestamp):
        self.calls += 1
        return [ObservedNote(NoteKind.TAP, 3, 640, 300, 40, 20, timestamp)]


def test_note_observer_throttles_detection_and_never_controls_device():
    clock = Clock()
    detector = Detector()
    controls = []

    def capture():
        clock.value += 0.005
        return np.zeros((720, 1280, 3), dtype=np.uint8)

    stats = NoteObserver(detector, clock).run(
        capture, lambda: False, duration_seconds=1, target_fps=60
    )

    assert 59 <= stats.processed_frames <= 61
    assert stats.captured_frames > stats.processed_frames
    assert stats.detections == {"tap": stats.processed_frames}
    assert stats.lanes == {3: stats.processed_frames}
    assert controls == []


def test_note_observer_stops_before_processing_next_frame():
    clock = Clock()
    detector = Detector()

    def capture():
        clock.value += 0.02
        return np.zeros((1, 1, 3), dtype=np.uint8)

    stats = NoteObserver(detector, clock).run(
        capture, lambda: detector.calls == 2, duration_seconds=10, target_fps=60
    )

    assert stats.stopped
    assert detector.calls == 2
