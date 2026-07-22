from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass
from typing import Callable

import numpy as np
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from .note_detector import NoteDetector


@dataclass(frozen=True)
class NoteObservationStats:
    captured_frames: int
    processed_frames: int
    detections: dict[str, int]
    lanes: dict[int, int]
    stopped: bool


class NoteObserver:
    """Run note detection at a bounded rate without issuing controller input."""

    def __init__(self, detector: NoteDetector, clock: Callable[[], float] = time.monotonic):
        self.detector = detector
        self.clock = clock

    def run(
        self,
        capture: Callable[[], np.ndarray],
        stopping: Callable[[], bool],
        *,
        duration_seconds: float,
        target_fps: int,
    ) -> NoteObservationStats:
        if not 1 <= duration_seconds <= 60:
            raise ValueError("duration_seconds 必须在 1..60 之间")
        if not 15 <= target_fps <= 120:
            raise ValueError("target_fps 必须在 15..120 之间")
        deadline = self.clock() + duration_seconds
        next_process = self.clock()
        frame_interval = 1 / target_fps
        captured = processed = 0
        kinds: Counter[str] = Counter()
        lanes: Counter[int] = Counter()
        stopped = False
        while self.clock() < deadline:
            if stopping():
                stopped = True
                break
            image = capture()
            captured += 1
            now = self.clock()
            if stopping():
                stopped = True
                break
            if now < next_process:
                continue
            next_process += frame_interval
            if now - next_process > frame_interval:
                next_process = now + frame_interval
            if not isinstance(image, np.ndarray) or image.ndim != 3 or image.size == 0:
                continue
            notes = self.detector.detect(image, now)
            processed += 1
            kinds.update(note.kind.value for note in notes)
            lanes.update(note.lane for note in notes)
        return NoteObservationStats(
            captured_frames=captured,
            processed_frames=processed,
            detections=dict(sorted(kinds.items())),
            lanes=dict(sorted(lanes.items())),
            stopped=stopped,
        )


@AgentServer.custom_action("RealtimeNoteObserve")
class RealtimeNoteObserve(CustomAction):
    """Observe rehearsal notes for ten seconds. This action has no input API calls."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        params = json.loads(argv.custom_action_param or "{}")
        observer = NoteObserver(NoteDetector())
        stats = observer.run(
            lambda: context.tasker.controller.post_screencap().wait().get(),
            lambda: context.tasker.stopping,
            duration_seconds=float(params.get("duration_seconds", 10)),
            target_fps=int(params.get("target_fps", 60)),
        )
        print(
            "RealtimeNoteObserve "
            f"captured={stats.captured_frames} processed={stats.processed_frames} "
            f"detections={json.dumps(stats.detections, sort_keys=True)} "
            f"lanes={json.dumps(stats.lanes, sort_keys=True)} stopped={stats.stopped}",
            flush=True,
        )
        return not stats.stopped and stats.processed_frames > 0
