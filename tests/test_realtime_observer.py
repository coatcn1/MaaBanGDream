from __future__ import annotations

import numpy as np
import pytest
import json
from datetime import datetime, timezone

from agent.realtime.frame_observer import (
    LatestFrameObserver,
    ObservationStats,
    write_observation_report,
)


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


def test_observer_reports_capture_distribution_and_stall_thresholds():
    clock = Clock()
    durations = iter([0.010, 0.020, 0.110, 0.200])
    captures = 0

    def capture():
        nonlocal captures
        captures += 1
        clock.value += next(durations)
        return np.zeros((1, 1, 3), dtype=np.uint8)

    stats = LatestFrameObserver(clock).run(
        capture,
        lambda: captures == 4,
        duration_seconds=1,
        frame_timeout_ms=150,
    )

    assert stats.capture_p50_ms == 65.0
    assert stats.capture_mean_ms == 85.0
    assert stats.capture_p95_ms == pytest.approx(186.5)
    assert stats.maximum_capture_ms == 200.0
    assert stats.over_100ms_frames == 2
    assert stats.over_150ms_frames == 1


def test_observer_writes_structured_benchmark_artifact(tmp_path):
    stats = ObservationStats(
        frames=100,
        elapsed_seconds=5.0,
        effective_fps=20.0,
        capture_mean_ms=5.0,
        capture_p50_ms=4.0,
        capture_p95_ms=7.0,
        maximum_capture_ms=469.0,
        over_100ms_frames=1,
        over_150ms_frames=1,
        timed_out_frames=1,
        invalid_frames=0,
        stopped=False,
    )

    path = write_observation_report(
        tmp_path,
        stats,
        method_label="EmulatorExtras",
        started_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["method_label"] == "EmulatorExtras"
    assert payload["metrics"]["maximum_capture_ms"] == 469.0
    assert payload["metrics"]["capture_mean_ms"] == 5.0
    assert payload["metrics"]["over_150ms_frames"] == 1
