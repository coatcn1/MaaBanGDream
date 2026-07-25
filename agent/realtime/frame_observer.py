from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction


@dataclass(frozen=True)
class ObservationStats:
    frames: int
    elapsed_seconds: float
    effective_fps: float
    maximum_capture_ms: float
    timed_out_frames: int
    invalid_frames: int
    stopped: bool


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
        if not 0.1 <= duration_seconds <= 60:
            raise ValueError("duration_seconds 必须在 0.1..60 之间")
        if not 50 <= frame_timeout_ms <= 5000:
            raise ValueError("frame_timeout_ms 必须在 50..5000 之间")
        started = self.clock()
        deadline = started + duration_seconds
        frames = invalid = timed_out = 0
        maximum_capture = 0.0
        was_stopped = False
        while self.clock() < deadline:
            if stopping():
                was_stopped = True
                break
            before = self.clock()
            image = capture()
            capture_ms = (self.clock() - before) * 1000
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
            maximum_capture_ms=maximum_capture,
            timed_out_frames=timed_out,
            invalid_frames=invalid,
            stopped=was_stopped,
        )


def _parameters(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("RealtimeObserve 参数必须是 JSON 对象")
        return value
    return {}


@AgentServer.custom_action("RealtimeObserve")
class RealtimeObserve(CustomAction):
    """Short, screenshot-only controller benchmark. It never sends input."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        params = _parameters(argv.custom_action_param)
        observer = LatestFrameObserver()
        controller = context.tasker.controller
        stats = observer.run(
            lambda: controller.post_screencap().wait().get(),
            lambda: context.tasker.stopping,
            duration_seconds=float(params.get("duration_seconds", 5)),
            frame_timeout_ms=int(params.get("frame_timeout_ms", 150)),
        )
        print(
            "RealtimeObserve "
            f"frames={stats.frames} elapsed={stats.elapsed_seconds:.3f}s "
            f"fps={stats.effective_fps:.2f} max_capture={stats.maximum_capture_ms:.1f}ms "
            f"timeouts={stats.timed_out_frames} invalid={stats.invalid_frames} "
            f"stopped={stats.stopped}",
            flush=True,
        )
        return not stats.stopped and stats.frames > 0
