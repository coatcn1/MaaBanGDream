from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class ObservationStats:
    frames: int
    elapsed_seconds: float
    effective_fps: float
    capture_mean_ms: float
    capture_p50_ms: float
    capture_p95_ms: float
    maximum_capture_ms: float
    over_100ms_frames: int
    over_150ms_frames: int
    timed_out_frames: int
    invalid_frames: int
    stopped: bool


def write_observation_report(
    root: Path,
    stats: ObservationStats,
    *,
    method_label: str,
    started_at: datetime,
) -> Path:
    label = str(method_label).strip()
    if not label or len(label) > 80:
        raise ValueError("method_label 必须是 1..80 个字符")
    root.mkdir(parents=True, exist_ok=True)
    utc_started = started_at.astimezone(timezone.utc)
    stamp = utc_started.strftime("%Y%m%d-%H%M%S-%f")
    path = root / f"screencap-benchmark-{stamp}.json"
    payload = {
        "schema_version": 1,
        "started_at": utc_started.isoformat().replace("+00:00", "Z"),
        "method_label": label,
        "metrics": asdict(stats),
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


class LatestFrameObserver:
    """Measure controller screenshot delivery without retaining or touching frames."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock

    def run(
        self,
        capture: Callable[[], np.ndarray],
        stopping: Callable[[], bool],
        *,
        duration_seconds: float,
        frame_timeout_ms: int,
    ) -> ObservationStats:
        if not 0.1 <= duration_seconds <= 900:
            raise ValueError("duration_seconds 必须在 0.1..900 之间")
        if not 50 <= frame_timeout_ms <= 5000:
            raise ValueError("frame_timeout_ms 必须在 50..5000 之间")
        started = self.clock()
        deadline = started + duration_seconds
        frames = invalid = timed_out = 0
        maximum_capture = 0.0
        capture_samples_ms: list[float] = []
        was_stopped = False
        while self.clock() < deadline:
            if stopping():
                was_stopped = True
                break
            before = self.clock()
            image = capture()
            capture_ms = (self.clock() - before) * 1000
            capture_samples_ms.append(capture_ms)
            maximum_capture = max(maximum_capture, capture_ms)
            if capture_ms > frame_timeout_ms:
                timed_out += 1
            if stopping():
                was_stopped = True
                break
            if not isinstance(image, np.ndarray) or image.ndim < 2 or image.size == 0:
                invalid += 1
                continue
            frames += 1
        elapsed = max(0.0, self.clock() - started)
        return ObservationStats(
            frames=frames,
            elapsed_seconds=elapsed,
            effective_fps=frames / elapsed if elapsed else 0.0,
            capture_mean_ms=(
                float(np.mean(capture_samples_ms))
                if capture_samples_ms else 0.0
            ),
            capture_p50_ms=(
                float(np.percentile(capture_samples_ms, 50))
                if capture_samples_ms else 0.0
            ),
            capture_p95_ms=(
                float(np.percentile(capture_samples_ms, 95))
                if capture_samples_ms else 0.0
            ),
            maximum_capture_ms=maximum_capture,
            over_100ms_frames=sum(value > 100 for value in capture_samples_ms),
            over_150ms_frames=sum(value > 150 for value in capture_samples_ms),
            timed_out_frames=timed_out,
            invalid_frames=invalid,
            stopped=was_stopped,
        )
