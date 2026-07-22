from __future__ import annotations

import json
import queue
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from .note_detector import ObservedNote
from .touch_planner import TouchAction


class RealtimeDebugRecorder:
    """Record every analysed frame's notes plus a replay video.

    JSONL is written synchronously so the training timeline is lossless. Video
    encoding is isolated on a worker thread; if the encoder cannot keep up, the
    summary reports dropped video frames without losing note/action records.
    """

    def __init__(self, root: Path, *, video_fps: int = 30) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.output_dir = root / f"realtime-{stamp}"
        self.output_dir.mkdir(parents=True, exist_ok=False)
        self.video_fps = video_fps
        self._trace = (self.output_dir / "trace.jsonl").open("w", encoding="utf-8")
        self._events = (self.output_dir / "events.jsonl").open("w", encoding="utf-8")
        self._frames: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=180)
        self._video_frames = 0
        self._dropped_video_frames = 0
        self._trace_frames = 0
        self._error: BaseException | None = None
        self._event_count = 0
        self._released_at: dict[int, float] = {}
        self._thread = threading.Thread(target=self._encode_video, daemon=True)
        self._thread.start()

    @staticmethod
    def _serialise(value) -> dict:
        data = asdict(value)
        for key, item in tuple(data.items()):
            if hasattr(item, "value"):
                data[key] = item.value
        return data

    def record(
        self,
        image: np.ndarray,
        timestamp: float,
        notes: list[ObservedNote],
        actions: list[TouchAction],
        life_status: str | None,
    ) -> None:
        self._trace.write(json.dumps({
            "frame": self._trace_frames,
            "timestamp": timestamp,
            "life_status": life_status,
            "notes": [self._serialise(note) for note in notes],
            "actions": [self._serialise(action) for action in actions],
        }, ensure_ascii=False, separators=(",", ":")) + "\n")
        for action in actions:
            if action.kind.value == "up":
                self._released_at[action.lane] = timestamp
                if action.reason == "hold-failsafe":
                    self._write_event(
                        image, timestamp, action.lane,
                        "hold-failsafe", action.reason, 0.0,
                    )
                continue
            released_at = self._released_at.get(action.lane)
            delay = timestamp - released_at if released_at is not None else float("inf")
            if (
                action.reason == "rescue"
                and action.kind.value in {"tap", "down", "flick"}
                and 0 <= delay <= 0.5
            ):
                self._write_event(
                    image, timestamp, action.lane,
                    "post-release-rescue", action.reason, delay,
                )
        self._trace_frames += 1
        try:
            self._frames.put_nowait(image.copy())
        except queue.Full:
            self._dropped_video_frames += 1

    def _write_event(
        self,
        image: np.ndarray,
        timestamp: float,
        lane: int,
        kind: str,
        reason: str,
        delay: float,
    ) -> None:
        event_dir = self.output_dir / "events"
        event_dir.mkdir(exist_ok=True)
        relative = Path("events") / (
            f"frame-{self._trace_frames:06d}-{kind}-lane-{lane}.png"
        )
        if not cv2.imwrite(str(self.output_dir / relative), image):
            raise OSError("无法保存实时演奏异常截图")
        self._events.write(json.dumps({
            "frame": self._trace_frames,
            "timestamp": timestamp,
            "kind": kind,
            "lane": lane,
            "reason": reason,
            "delay_seconds": round(delay, 3),
            "screenshot": relative.as_posix(),
        }, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._event_count += 1

    def _encode_video(self) -> None:
        writer = None
        try:
            while True:
                frame = self._frames.get()
                if frame is None:
                    break
                if writer is None:
                    height, width = frame.shape[:2]
                    writer = cv2.VideoWriter(
                        str(self.output_dir / "playfield.avi"),
                        cv2.VideoWriter_fourcc(*"MJPG"),
                        self.video_fps,
                        (width, height),
                    )
                    if not writer.isOpened():
                        raise OSError("无法创建实时调试录像")
                writer.write(frame)
                self._video_frames += 1
        except BaseException as exc:
            self._error = exc
        finally:
            if writer is not None:
                writer.release()

    def close(self) -> None:
        self._trace.flush()
        self._trace.close()
        self._events.flush()
        self._events.close()
        self._frames.put(None)
        self._thread.join()
        (self.output_dir / "summary.json").write_text(json.dumps({
            "trace_frames": self._trace_frames,
            "video_frames": self._video_frames,
            "dropped_video_frames": self._dropped_video_frames,
            "video_fps": self.video_fps,
            "event_screenshots": self._event_count,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if self._error is not None:
            raise RuntimeError("实时调试录像写入失败") from self._error
