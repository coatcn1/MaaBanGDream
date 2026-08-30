from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from .note_detector import ObservedNote
from .touch_planner import TouchAction


_SENTINEL = None
_RECORD_QUEUE_CAPACITY = 12
_VIDEO_QUEUE_CAPACITY = 12


class RealtimeDebugRecorder:
    """Record every analysed frame's diagnostic trace and optional replay video.

    The realtime hot path only enqueues frame references. JSON serialisation,
    trace/event writes, event screenshots and the sampled video copy all run
    on a background worker, and MJPG encoding runs on a second thread, so
    debug recording must not compete with the 60 Hz detector.
    """

    def __init__(
        self,
        root: Path,
        *,
        video_fps: int = 60,
        video_enabled: bool = True,
        session_metadata: Mapping[str, object] | None = None,
        close_timeout_seconds: float = 2.0,
    ) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.output_dir = root / f"realtime-{stamp}"
        self.output_dir.mkdir(parents=True, exist_ok=False)
        self.video_fps = video_fps
        self.video_enabled = bool(video_enabled)
        self.close_timeout_seconds = max(0.01, float(close_timeout_seconds))
        self._trace = (self.output_dir / "trace.jsonl").open("w", encoding="utf-8")
        self._events = (self.output_dir / "events.jsonl").open("w", encoding="utf-8")
        self._record_queue: queue.Queue = queue.Queue(
            maxsize=_RECORD_QUEUE_CAPACITY
        )
        self._frames: queue.Queue[tuple | None] = queue.Queue(
            maxsize=_VIDEO_QUEUE_CAPACITY
        )
        self._video_frames = 0
        self._skipped_video_frames = 0
        self._dropped_video_frames = 0
        self._next_video_at: float | None = None
        self._trace_frames = 0
        self._dropped_trace_frames = 0
        self._first_timestamp: float | None = None
        self._session_metadata = (
            deepcopy(dict(session_metadata)) if session_metadata is not None else {}
        )
        self._session_metadata_set = session_metadata is not None
        self._metadata_lock = threading.Lock()
        self._summary_lock = threading.Lock()
        self._summary_finalizer_lock = threading.Lock()
        self._closed = False
        self._error: BaseException | None = None
        self._summary_finalizer_thread: threading.Thread | None = None
        self._event_count = 0
        self._released_at: dict[int, float] = {}
        self._diagnostic_counts: dict[str, int] = {}
        self._last_timing_state: dict[str, object] = {}
        self._video_actual_fps: float | None = None
        self._video_duration_seconds: float | None = None
        self._timestamp_duration_seconds: float | None = None
        self._video_duration_difference_seconds: float | None = None
        self._video_seek_verified = False
        self._video_finalize_status = (
            "pending" if self.video_enabled else "disabled"
        )
        self._record_thread = threading.Thread(
            target=self._record_worker, daemon=True
        )
        self._encode_thread = threading.Thread(
            target=self._encode_video, daemon=True
        )
        self._record_thread.start()
        self._encode_thread.start()

    @staticmethod
    def _serialise(value) -> dict:
        data = asdict(value)
        for key, item in tuple(data.items()):
            if hasattr(item, "value"):
                data[key] = item.value
        return data

    def set_session_metadata(self, metadata: Mapping[str, object]) -> None:
        """Attach immutable run metadata without putting it on every trace row."""
        with self._metadata_lock:
            if self._session_metadata_set:
                raise RuntimeError("session metadata can only be set once")
            self._session_metadata = deepcopy(dict(metadata))
            self._session_metadata_set = True

    def record(
        self,
        image: np.ndarray,
        timestamp: float,
        notes: list[ObservedNote],
        actions: list[TouchAction],
        life_status: str | None,
        diagnostics: list[dict[str, object]] | None = None,
        timing_state: dict[str, object] | None = None,
        life_value: int | None = None,
        touch_state: dict[str, object] | None = None,
    ) -> None:
        if self._closed or self._error is not None:
            self._dropped_trace_frames += 1
            return
        # Anchor elapsed time to the first attempted engine frame, even if a
        # later queue overflow drops that frame before the worker serialises it.
        if self._first_timestamp is None:
            self._first_timestamp = timestamp
        try:
            self._record_queue.put_nowait(
                (
                    image, timestamp, notes, actions, life_status,
                    diagnostics, timing_state, life_value, touch_state,
                )
            )
        except queue.Full:
            # Trace fidelity is diagnostic-only. Never let a slow disk or
            # recorder worker exert backpressure on the realtime touch loop.
            self._dropped_trace_frames += 1

    def _record_worker(self) -> None:
        try:
            while True:
                item = self._record_queue.get()
                if item is _SENTINEL:
                    break
                (
                    image, timestamp, notes, actions, life_status,
                    diagnostics, timing_state, life_value, touch_state,
                ) = item
                self._process_record(
                    image, timestamp, notes, actions, life_status,
                    diagnostics, timing_state, life_value, touch_state,
                )
        except BaseException as exc:
            self._error = exc
        finally:
            self._discard_pending_records()
            try:
                self._trace.flush()
                self._trace.close()
                self._events.flush()
                self._events.close()
            except BaseException as exc:
                if self._error is None:
                    self._error = exc
            if self._encode_thread.is_alive():
                try:
                    # The sentinel is FIFO, so a small accepted batch is
                    # encoded before shutdown without making record() wait.
                    self._frames.put_nowait(_SENTINEL)
                except queue.Full:
                    # A saturated encoder queue is diagnostic backlog, not a
                    # reason to block realtime shutdown while it drains.
                    self._discard_pending_video_frames()
                    self._frames.put_nowait(_SENTINEL)
            else:
                self._discard_pending_video_frames()

    def _summary_payload(
        self,
        *,
        record_worker_finalized: bool,
        encoder_finalized: bool,
    ) -> dict:
        with self._metadata_lock:
            session = deepcopy(self._session_metadata)
        error = self._error
        return {
            "schema_version": 2,
            "recording_mode": "video" if self.video_enabled else "trace-only",
            "video_enabled": self.video_enabled,
            "record_worker_finalized": bool(record_worker_finalized),
            "encoder_finalized": bool(encoder_finalized),
            "recorder_error": (
                f"{type(error).__name__}: {error}" if error is not None else None
            ),
            "trace_frames": self._trace_frames,
            "dropped_trace_frames": self._dropped_trace_frames,
            "video_frames": self._video_frames,
            "skipped_video_frames": self._skipped_video_frames,
            "dropped_video_frames": self._dropped_video_frames,
            "video_fps": self.video_fps,
            "video_actual_fps": self._video_actual_fps,
            "video_container": "matroska" if self.video_enabled else None,
            "video_codec": "MJPG" if self.video_enabled else None,
            "video_duration_seconds": self._video_duration_seconds,
            "timestamp_duration_seconds": self._timestamp_duration_seconds,
            "video_duration_difference_seconds": (
                self._video_duration_difference_seconds
            ),
            "video_seek_verified": self._video_seek_verified,
            "video_finalize_status": self._video_finalize_status,
            "complete_frame_evidence": bool(
                self._dropped_trace_frames == 0
                and (
                    not self.video_enabled
                    or (
                        self._dropped_video_frames == 0
                        and self._skipped_video_frames == 0
                    )
                )
            ),
            "event_screenshots": self._event_count,
            "diagnostic_counts": dict(self._diagnostic_counts),
            "timing_feedback": deepcopy(self._last_timing_state),
            "session": session,
        }

    def _write_summary(
        self,
        *,
        record_worker_finalized: bool,
        encoder_finalized: bool,
    ) -> None:
        path = self.output_dir / "summary.json"
        temporary = path.with_suffix(".json.tmp")
        payload = self._summary_payload(
            record_worker_finalized=record_worker_finalized,
            encoder_finalized=encoder_finalized,
        )
        with self._summary_lock:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)

    def _finalize_summary_after_workers(self) -> None:
        self._record_thread.join()
        self._encode_thread.join()
        try:
            self._write_summary(
                record_worker_finalized=True,
                encoder_finalized=True,
            )
        except BaseException as exc:
            if self._error is None:
                self._error = exc

    def _start_summary_finalizer(self) -> None:
        with self._summary_finalizer_lock:
            if self._summary_finalizer_thread is not None:
                return
            self._summary_finalizer_thread = threading.Thread(
                target=self._finalize_summary_after_workers,
                daemon=True,
            )
            self._summary_finalizer_thread.start()

    def _discard_pending_records(self) -> None:
        """Release queued frame references after a worker failure or close."""
        while True:
            try:
                item = self._record_queue.get_nowait()
            except queue.Empty:
                return
            if item is not _SENTINEL:
                self._dropped_trace_frames += 1

    def _discard_pending_video_frames(self) -> None:
        """Release sampled frame copies before stopping the encoder."""
        while True:
            try:
                frame = self._frames.get_nowait()
            except queue.Empty:
                return
            if frame is not _SENTINEL:
                self._dropped_video_frames += 1

    def _process_record(
        self,
        image: np.ndarray,
        timestamp: float,
        notes: list[ObservedNote],
        actions: list[TouchAction],
        life_status: str | None,
        diagnostics: list[dict[str, object]] | None,
        timing_state: dict[str, object] | None,
        life_value: int | None = None,
        touch_state: dict[str, object] | None = None,
    ) -> None:
        diagnostics = diagnostics or []
        timing_state = timing_state or {}
        elapsed_ms = (timestamp - self._first_timestamp) * 1000
        trace_frame = self._trace_frames
        self._trace.write(json.dumps({
            "frame": trace_frame,
            "timestamp": timestamp,
            "elapsed_ms": round(elapsed_ms, 3),
            "life_status": life_status,
            "life_value": life_value,
            "notes": [self._serialise(note) for note in notes],
            "actions": [self._serialise(action) for action in actions],
            "diagnostics": diagnostics,
            "timing_feedback": timing_state,
            "touch_state": touch_state or {},
        }, ensure_ascii=False, separators=(",", ":")) + "\n")
        for diagnostic in diagnostics:
            event = str(diagnostic.get("event", "unknown"))
            self._diagnostic_counts[event] = self._diagnostic_counts.get(event, 0) + 1
        if timing_state:
            self._last_timing_state = dict(timing_state)
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
                and 0 <= delay <= 0.65
            ):
                self._write_event(
                    image, timestamp, action.lane,
                    "post-release-rescue", action.reason, delay,
                )
        self._trace_frames += 1
        if not self.video_enabled:
            return
        if self._next_video_at is None:
            self._next_video_at = timestamp
        if timestamp + 1e-9 >= self._next_video_at:
            try:
                self._frames.put_nowait((
                    image.copy(),
                    trace_frame,
                    float(timestamp),
                    round(elapsed_ms, 3),
                ))
            except queue.Full:
                self._dropped_video_frames += 1
            interval = 1.0 / self.video_fps
            while self._next_video_at <= timestamp + 1e-9:
                self._next_video_at += interval
        else:
            self._skipped_video_frames += 1

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
        if not self.video_enabled:
            return
        writer = None
        mapping = None
        partial_path = self.output_dir / "playfield.partial.mkv"
        final_path = self.output_dir / "playfield.mkv"
        first_timestamp: float | None = None
        last_timestamp: float | None = None
        try:
            mapping = (self.output_dir / "video_frames.jsonl").open(
                "w", encoding="utf-8",
            )
            while True:
                item = self._frames.get()
                if item is _SENTINEL:
                    break
                if isinstance(item, tuple):
                    frame, trace_frame, timestamp, elapsed_ms = item
                else:
                    # Backward-compatible with tests/tools that put a raw
                    # frame directly into the internal queue.
                    frame = item
                    trace_frame = -1
                    timestamp = float("nan")
                    elapsed_ms = None
                if writer is None:
                    height, width = frame.shape[:2]
                    writer = cv2.VideoWriter(
                        str(partial_path),
                        cv2.VideoWriter_fourcc(*"MJPG"),
                        float(self.video_fps),
                        (width, height),
                    )
                    if not writer.isOpened():
                        raise OSError("无法创建实时调试录像")
                writer.write(frame)
                mapping.write(json.dumps({
                    "encoded_frame": self._video_frames,
                    "trace_frame": int(trace_frame),
                    "monotonic_timestamp": timestamp,
                    "elapsed_ms": elapsed_ms,
                }, ensure_ascii=False, separators=(",", ":")) + "\n")
                if timestamp == timestamp:
                    first_timestamp = (
                        timestamp if first_timestamp is None else first_timestamp
                    )
                    last_timestamp = timestamp
                self._video_frames += 1
        except BaseException as exc:
            self._error = exc
            self._video_finalize_status = "encoder-error"
        finally:
            if writer is not None:
                writer.release()
            if mapping is not None:
                mapping.flush()
                mapping.close()
        if self._error is not None:
            return
        if self._video_frames == 0:
            self._video_finalize_status = "no-frames"
            return
        try:
            self._verify_and_publish_video(
                partial_path,
                final_path,
                first_timestamp=first_timestamp,
                last_timestamp=last_timestamp,
            )
        except BaseException as exc:
            self._video_finalize_status = "verification-failed"
            if self._error is None:
                self._error = exc

    def _verify_and_publish_video(
        self,
        partial_path: Path,
        final_path: Path,
        *,
        first_timestamp: float | None,
        last_timestamp: float | None,
    ) -> None:
        if not partial_path.is_file() or partial_path.stat().st_size <= 0:
            raise OSError("MJPG/MKV partial video was not created")
        capture = cv2.VideoCapture(str(partial_path))
        try:
            if not capture.isOpened():
                raise OSError("cannot reopen MJPG/MKV partial video")
            actual_fps = float(capture.get(cv2.CAP_PROP_FPS))
            frame_count = int(round(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
            if frame_count != self._video_frames:
                raise OSError(
                    f"video frame count mismatch: {frame_count} != {self._video_frames}"
                )
            if abs(actual_fps - float(self.video_fps)) > 0.1:
                raise OSError(
                    f"video FPS mismatch: {actual_fps:.3f} != {self.video_fps}"
                )
            for index in sorted({0, frame_count // 2, frame_count - 1}):
                if not capture.set(cv2.CAP_PROP_POS_FRAMES, index):
                    raise OSError(f"video random seek rejected frame {index}")
                ok, frame = capture.read()
                if not ok or frame is None:
                    raise OSError(f"video random seek failed at frame {index}")
        finally:
            capture.release()
        self._video_actual_fps = actual_fps
        self._video_duration_seconds = round(
            max(0, frame_count - 1) / actual_fps, 6,
        )
        if first_timestamp is not None and last_timestamp is not None:
            self._timestamp_duration_seconds = round(
                max(0.0, last_timestamp - first_timestamp), 6,
            )
            self._video_duration_difference_seconds = round(
                self._video_duration_seconds
                - self._timestamp_duration_seconds,
                6,
            )
        self._video_seek_verified = True
        os.replace(partial_path, final_path)
        self._video_finalize_status = "verified"

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._record_thread.is_alive():
            # Give the normal fast path a small bounded grace period so a
            # caller that closes immediately after record() retains its trace.
            grace_deadline = time.monotonic() + min(
                0.1, self.close_timeout_seconds / 2
            )
            while (
                not self._record_queue.empty()
                and self._record_thread.is_alive()
                and time.monotonic() < grace_deadline
            ):
                time.sleep(0.001)
            self._discard_pending_records()
            self._record_queue.put_nowait(_SENTINEL)
            self._record_thread.join(timeout=self.close_timeout_seconds)
            if self._record_thread.is_alive() and self._error is None:
                self._error = TimeoutError(
                    "recorder worker did not stop within "
                    f"{self.close_timeout_seconds:.2f}s"
                )
        else:
            self._discard_pending_records()
        record_worker_finalized = not self._record_thread.is_alive()
        if record_worker_finalized:
            if self._encode_thread.is_alive():
                self._encode_thread.join(timeout=self.close_timeout_seconds)
        encoder_finalized = not self._encode_thread.is_alive()
        if (
            record_worker_finalized
            and not encoder_finalized
            and self._error is None
        ):
            self._error = TimeoutError(
                "video encoder did not stop within "
                f"{self.close_timeout_seconds:.2f}s"
            )
        try:
            self._write_summary(
                record_worker_finalized=record_worker_finalized,
                encoder_finalized=encoder_finalized,
            )
        except BaseException as exc:
            if self._error is None:
                self._error = exc
        if not record_worker_finalized or not encoder_finalized:
            self._start_summary_finalizer()
        if self._error is not None:
            raise RuntimeError("实时调试录像写入失败") from self._error
