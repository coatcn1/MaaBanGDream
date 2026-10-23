from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections import deque
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from .note_detector import ObservedNote
from .touch_planner import TouchAction
from .vision_io import imwrite_unicode


_SENTINEL = None
_RECORD_QUEUE_CAPACITY = 12
_VIDEO_QUEUE_CAPACITY = 12
_OPENING_EVENT_CAPACITY = 64
_LIFECYCLE_LOCK = threading.Lock()


@dataclass(frozen=True)
class _NativeLifeFrame:
    image: np.ndarray
    timestamp: float
    value: int | None
    visible: bool
    alive_confirmed: bool
    metadata: dict[str, object]


@dataclass(frozen=True)
class _NativeFeedbackFrame:
    image: np.ndarray
    timestamp: float
    metadata: dict[str, object]
    window_index: int
    index: int


def append_lifecycle_event(
    output_dir: Path,
    phase: str,
    status: str,
    *,
    details: Mapping[str, object] | None = None,
) -> Path:
    """向同一 run 的证据包追加低频生命周期决定。"""
    output_dir = Path(output_dir)
    if not output_dir.is_dir():
        raise FileNotFoundError(f"调试证据目录不存在: {output_dir}")
    path = output_dir / "lifecycle.jsonl"
    payload = {
        "phase": str(phase),
        "status": str(status),
        "wall_time": time.time(),
        "details": deepcopy(dict(details or {})),
    }
    with _LIFECYCLE_LOCK:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
    return path


class RealtimeDebugRecorder:
    """Record every analysed frame's diagnostic trace and optional replay video.

    The realtime hot path only enqueues frame references. JSON serialisation,
    trace/event writes, event screenshots and the sampled video copy all run
    on a background worker, and MJPG encoding runs on a second thread, so
    debug recording must not compete with the 60 Hz detector.
    """
    supports_frame_metadata = True

    def __init__(
        self,
        root: Path,
        *,
        video_fps: int = 60,
        video_enabled: bool = True,
        session_metadata: Mapping[str, object] | None = None,
        close_timeout_seconds: float = 2.0,
        session_kind: str = "realtime",
        opening_seconds: float | None = None,
    ) -> None:
        # 目录名带演奏类型（单人正式/单人排练/协力/挑战/校准等），便于
        # 直接区分录像来源；重试可能在同一秒重新建包，微秒后缀避免诊断
        # 功能反过来导致任务失败。
        kind = str(session_kind or "realtime").strip() or "realtime"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.output_dir = root / f"{kind}-{stamp}"
        self.output_dir.mkdir(parents=True, exist_ok=False)
        self.video_fps = video_fps
        self.video_enabled = bool(video_enabled) and opening_seconds is None
        self.opening_seconds = None if opening_seconds is None else max(0.0,float(opening_seconds))
        self._opening_first_note_at = None
        self._opening_cutoff = None
        self._opening_window_reached = False
        self._opening_after_cutoff_frames = 0
        self._opening_duplicate_frames = 0
        self._last_enqueued_timestamp = None
        self._opening_pins = {}
        self._opening_before = deque(maxlen=24)
        self._opening_after = deque(maxlen=80)
        self._opening_next_after_at = None
        self._opening_seen_sequence = {}
        self._opening_capacity_evictions = 0
        self._opening_sampled_out = 0
        self._opening_written = 0
        self._opening_checkpoints = []
        # 整局异常图独立于开场128张预算；关闭清理后由工作线程统一编码。
        self._opening_events = []
        self._opening_event_flush_status = "pending" if opening_seconds is not None else "disabled"
        self._event_requested = 0
        self._event_dropped_reasons = {}
        self._opening_flush_allowed = threading.Event()
        self._opening_checkpoint_dropped = 0
        self._opening_flush_status = "pending" if opening_seconds is not None else "disabled"
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
        self._checkpoint_lock = threading.Lock()
        self._checkpoint_count = 0
        self._checkpoint_requested = 0
        self._event_count = 0
        self._native_life_history: deque[_NativeLifeFrame] = deque(maxlen=10)
        self._native_life_triggered_at: float | None = None
        self._native_life_evidence_count = 0
        self._native_life_written = 0
        self._dropped_native_life_frames = 0
        self._native_feedback_window = None
        self._native_feedback_window_index = -1
        self._native_feedback_count = 0
        self._native_feedback_total = 0
        self._native_feedback_written = 0
        self._dropped_native_feedback_frames = 0
        self._released_at: dict[int, float] = {}
        self._diagnostic_counts: dict[str, int] = {}
        self._phase_counts: dict[str, int] = {}
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

    def update_session_metadata(self, metadata: Mapping[str, object]) -> None:
        """在同一 run 内用已确认的后续证据刷新会话摘要。"""
        with self._metadata_lock:
            if not self._session_metadata_set:
                raise RuntimeError("session metadata has not been set")
            incoming = deepcopy(dict(metadata))
            previous_run_id = self._session_metadata.get("run_id")
            incoming_run_id = incoming.get("run_id")
            if previous_run_id != incoming_run_id:
                raise RuntimeError("session metadata run_id cannot change")
            self._session_metadata = incoming

    @property
    def recording_mode(self):
        return "opening-screenshots+trace" if self.opening_seconds is not None else "video" if self.video_enabled else "trace-only"

    def mark_first_note(self, timestamp: float) -> None:
        if self.opening_seconds is not None and self._opening_first_note_at is None:
            self._opening_first_note_at = float(timestamp)
            self._opening_cutoff = float(timestamp) + self.opening_seconds

    def record_phase(
        self,
        image: np.ndarray,
        timestamp: float,
        phase: str,
        *,
        diagnostics: list[dict[str, object]] | None = None,
        frame_metadata: dict[str, object] | None = None,
    ) -> None:
        """记录演奏热路径之外的封面、门控等阶段画面。"""
        self.record(
            image,
            timestamp,
            [],
            [],
            None,
            diagnostics=diagnostics,
            phase=phase,
            frame_metadata=frame_metadata,
        )

    def save_checkpoint(
        self,
        image: np.ndarray,
        phase: str,
        status: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> Path | None:
        """保存低频关键阶段现场；即使热路径录像已关闭也允许补写结算证据。"""
        safe_phase = "".join(
            character if character.isalnum() or character in "-_" else "-"
            for character in str(phase)
        ).strip("-") or "unknown"
        safe_status = "".join(
            character if character.isalnum() or character in "-_" else "-"
            for character in str(status)
        ).strip("-") or "unknown"
        with self._checkpoint_lock:
            index = self._checkpoint_requested
            self._checkpoint_requested += 1
            checkpoint_dir = self.output_dir / "checkpoints"
            if self.opening_seconds is None or self._closed:
                checkpoint_dir.mkdir(exist_ok=True)
            relative = Path("checkpoints") / (
                f"{index:03d}-{safe_phase}-{safe_status}.png"
            )
            payload = {
                "index": index,
                "phase": str(phase),
                "status": str(status),
                "wall_time": time.time(),
                "screenshot": relative.as_posix(),
                "details": deepcopy(dict(details or {})),
            }
            if self.opening_seconds is not None and not self._closed:
                if len(self._opening_checkpoints) < 8:
                    self._opening_checkpoints.append((relative, image.copy(), payload))
                else:
                    self._opening_checkpoint_dropped += 1
                    payload.update(screenshot=None, screenshot_status="dropped-capacity")
                    self._write_checkpoint_index(payload)
                    return None
                return self.output_dir / relative
            if not imwrite_unicode(self.output_dir / relative, image):
                raise OSError("无法保存实时演奏阶段证据截图")
            payload["screenshot_status"] = "saved"
            self._write_checkpoint_index(payload)
            self._checkpoint_count += 1
            self._refresh_checkpoint_summary()
            return self.output_dir / relative

    def _write_checkpoint_index(self, payload):
        with (self.output_dir / "checkpoints.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _refresh_checkpoint_summary(self) -> None:
        path = self.output_dir / "summary.json"
        if not path.is_file():
            return
        with self._summary_lock:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return
            payload["checkpoint_count"] = self._checkpoint_count
            payload["checkpoint_requested"] = self._checkpoint_requested
            payload["opening_checkpoint_dropped"] = self._opening_checkpoint_dropped
            payload["checkpoint_index"] = "checkpoints.jsonl"
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)

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
        phase: str = "engine",
        frame_metadata: dict[str, object] | None = None,
    ) -> None:
        if self._closed or self._error is not None:
            self._dropped_trace_frames += 1
            return
        # Anchor elapsed time to the first attempted engine frame, even if a
        # later queue overflow drops that frame before the worker serialises it.
        if self._first_timestamp is None:
            self._first_timestamp = timestamp
        metadata = dict(frame_metadata or {})
        if timestamp == self._last_enqueued_timestamp:
            metadata["duplicate_observation"] = True
        self._last_enqueued_timestamp = timestamp
        try:
            self._record_queue.put_nowait(
                (
                    image, timestamp, notes, actions, life_status,
                    diagnostics, timing_state, life_value, touch_state,
                    str(phase),
                    metadata,
                )
            )
        except queue.Full:
            # Trace fidelity is diagnostic-only. Never let a slow disk or
            # recorder worker exert backpressure on the realtime touch loop.
            self._dropped_trace_frames += 1

    def record_native_life(
        self, image: np.ndarray, timestamp: float, value: int | None,
        *, visible: bool, alive_confirmed: bool,
        frame_metadata: dict[str, object] | None = None,
    ) -> None:
        """只入队已有监控截图；磁盘和回溯缓存均不占用演奏热路径。"""
        if self._closed or self._error is not None:
            self._dropped_native_life_frames += 1
            return
        try:
            self._record_queue.put_nowait(
                _NativeLifeFrame(image, timestamp, value, visible, alive_confirmed,
                                 dict(frame_metadata or {}))
            )
        except queue.Full:
            self._dropped_native_life_frames += 1

    def record_native_feedback(self, image, timestamp, frame_metadata, window_start_s):
        """仅保存现有新帧的判定 ROI，不增加截图或扩大采样窗口。"""
        from .timing_feedback import TimingFeedbackDetector
        if self._closed or self._error is not None:
            self._dropped_native_feedback_frames += 1
            return
        if self._native_feedback_window != window_start_s:
            self._native_feedback_window = window_start_s
            self._native_feedback_window_index += 1
            self._native_feedback_count = 0
        # 常开观测只留候选/反向/混色 ROI；每窗和整局均有上限，不全程存图。
        if self._native_feedback_count >= 20 or self._native_feedback_total >= 256:
            self._dropped_native_feedback_frames += 1
            return
        index = self._native_feedback_count
        self._native_feedback_count += 1
        self._native_feedback_total += 1
        x1, y1, x2, y2 = TimingFeedbackDetector.ROI
        if not isinstance(image, np.ndarray) or image.shape != (720, 1280, 3):
            self._dropped_native_feedback_frames += 1
            return
        metadata = dict(frame_metadata, window_start_s=window_start_s,
                        roi=[x1, y1, x2, y2])
        try:
            self._record_queue.put_nowait(_NativeFeedbackFrame(
                image[y1:y2, x1:x2].copy(), timestamp, metadata,
                self._native_feedback_window_index, index))
        except queue.Full:
            self._dropped_native_feedback_frames += 1

    def _process_native_life(self, frame: _NativeLifeFrame) -> None:
        history = self._native_life_history
        triggered = self._native_life_triggered_at
        if triggered is not None:
            if frame.timestamp <= triggered + 2.0 and self._native_life_evidence_count < 21:
                self._write_native_life_frame(frame)
            return
        if not frame.visible or not frame.alive_confirmed or frame.value is None:
            history.clear()
            return
        while history and frame.timestamp - history[0].timestamp > 2.0:
            history.popleft()
        if history and max(item.value for item in history) - frame.value >= 100:
            # 每局仅保存首次两秒内掉血至少 100 的前后窗口，最多 21 张；
            # 历史帧只写事件，不倒插 trace，避免破坏重放时间顺序。
            self._native_life_triggered_at = frame.timestamp
            for previous in history:
                self._write_native_life_frame(previous)
            self._write_native_life_frame(frame)
            history.clear()
        else:
            history.append(frame)

    def _write_native_life_frame(self, frame: _NativeLifeFrame) -> None:
        index = self._native_life_evidence_count
        self._write_event(
            frame.image, frame.timestamp, -1, f"native-life-drop-{index:02d}",
            f"life={frame.value}; visible={frame.visible}; "
            f"trigger_timestamp={self._native_life_triggered_at}", 0.0,
            frame_metadata=frame.metadata,
        )
        self._native_life_evidence_count += 1

    def _record_worker(self) -> None:
        try:
            while True:
                item = self._record_queue.get()
                if item is _SENTINEL:
                    break
                if isinstance(item, _NativeLifeFrame):
                    self._process_native_life(item)
                    continue
                if isinstance(item, _NativeFeedbackFrame):
                    self._write_event(item.image, item.timestamp, -1,
                                      f"native-feedback-{item.window_index:02d}-{item.index:03d}",
                                      "existing-fresh-candidate-roi", 0.0,
                                      frame_metadata=item.metadata)
                    continue
                (
                    image, timestamp, notes, actions, life_status,
                    diagnostics, timing_state, life_value, touch_state,
                    phase,
                    frame_metadata,
                ) = item
                self._process_record(
                    image, timestamp, notes, actions, life_status,
                    diagnostics, timing_state, life_value, touch_state,
                    phase,
                    frame_metadata,
                )
        except BaseException as exc:
            self._error = exc
        finally:
            self._discard_pending_records()
            if self.opening_seconds is not None:
                self._opening_flush_allowed.wait()
                for flush in (self._flush_opening_frames, self._flush_opening_checkpoints,
                              self._flush_opening_events):
                    try:
                        flush()
                    except BaseException as exc:
                        self._opening_flush_status = "failed"
                        if self._error is None:
                            self._error = exc
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
            "recording_mode": self.recording_mode,
            "opening_seconds_after_first_note": self.opening_seconds,
            "opening_first_note_observed_at": self._opening_first_note_at,
            "opening_cutoff_timestamp": self._opening_cutoff,
            "opening_window_reached": self._opening_window_reached,
            "opening_after_cutoff_frames": self._opening_after_cutoff_frames,
            "opening_duplicate_frames": self._opening_duplicate_frames,
            "opening_truncated": bool(self.opening_seconds is not None and not self._opening_window_reached),
            "opening_screenshot_capacity": 128,
            "opening_screenshots_written": self._opening_written,
            "opening_capacity_evictions": self._opening_capacity_evictions,
            "opening_capacity_truncated": self._opening_capacity_evictions > 0,
            "opening_reused_skipped": self._opening_duplicate_frames,
            "opening_sampled_out": self._opening_sampled_out,
            "opening_flush_status": self._opening_flush_status,
            "opening_checkpoint_dropped": self._opening_checkpoint_dropped,
            "event_screenshot_capacity": _OPENING_EVENT_CAPACITY if self.opening_seconds is not None else None,
            "event_screenshots_requested": self._event_requested,
            "event_screenshots_dropped": sum(self._event_dropped_reasons.values()),
            "event_screenshot_drop_reasons": dict(self._event_dropped_reasons),
            "event_screenshot_flush_status": self._opening_event_flush_status,
            "opening_frame_index": "opening_frames.jsonl" if self.opening_seconds is not None else None,
            "opening_continuous_frame_evidence": False if self.opening_seconds is not None else None,
            "video_enabled": self.video_enabled,
            "record_worker_finalized": bool(record_worker_finalized),
            "encoder_finalized": bool(encoder_finalized),
            "recorder_error": (
                f"{type(error).__name__}: {error}" if error is not None else None
            ),
            "trace_frames": self._trace_frames,
            "dropped_trace_frames": self._dropped_trace_frames,
            "native_life_evidence_frames": self._native_life_written,
            "dropped_native_life_frames": self._dropped_native_life_frames,
            "native_feedback_evidence_frames": self._native_feedback_written,
            "dropped_native_feedback_frames": self._dropped_native_feedback_frames,
            "native_life_evidence_complete": bool(record_worker_finalized and error is None
                and self._dropped_native_life_frames == 0),
            "native_feedback_evidence_complete": bool(record_worker_finalized and error is None
                and self._dropped_native_feedback_frames == 0),
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
                and self.opening_seconds is None
                and (
                    not self.video_enabled
                    or (
                        self._dropped_video_frames == 0
                        and self._skipped_video_frames == 0
                    )
                )
            ),
            "event_screenshots": self._event_count,
            "checkpoint_count": self._checkpoint_count,
            "checkpoint_requested": self._checkpoint_requested,
            "checkpoint_index": (
                "checkpoints.jsonl" if self._checkpoint_requested else None
            ),
            "diagnostic_counts": dict(self._diagnostic_counts),
            "phase_counts": dict(self._phase_counts),
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
        with self._summary_lock:
            # 快照与替换同属一个临界区，避免后台旧快照覆盖关闭后的checkpoint补写。
            # 锁顺序为summary→metadata；元数据更新不持锁刷新summary。
            payload = self._summary_payload(
                record_worker_finalized=record_worker_finalized,
                encoder_finalized=encoder_finalized,
            )
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
            if isinstance(item, _NativeLifeFrame):
                self._dropped_native_life_frames += 1
            elif isinstance(item, _NativeFeedbackFrame):
                self._dropped_native_feedback_frames += 1
            elif item is not _SENTINEL:
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
        phase: str = "engine",
        frame_metadata: dict[str, object] | None = None,
    ) -> None:
        diagnostics = diagnostics or []
        timing_state = timing_state or {}
        elapsed_ms = (timestamp - self._first_timestamp) * 1000
        trace_frame = self._trace_frames
        self._trace.write(json.dumps({
            "frame": trace_frame,
            "timestamp": timestamp,
            "elapsed_ms": round(elapsed_ms, 3),
            "phase": phase,
            "life_status": life_status,
            "life_value": life_value,
            "notes": [self._serialise(note) for note in notes],
            "actions": [self._serialise(action) for action in actions],
            "diagnostics": diagnostics,
            "timing_feedback": timing_state,
            "touch_state": touch_state or {},
            "frame_metadata": frame_metadata or {},
        }, ensure_ascii=False, separators=(",", ":")) + "\n")
        for diagnostic in diagnostics:
            event = str(diagnostic.get("event", "unknown"))
            self._diagnostic_counts[event] = self._diagnostic_counts.get(event, 0) + 1
            if diagnostic.get("evidence_screenshot") is True:
                # 生命监控等热路径只把截图请求排入现有 recorder 队列；由此工作
                # 线程写入关键帧，不把磁盘 I/O 反压到触控与截图循环。
                self._write_event(
                    image,
                    timestamp,
                    -1,
                    event,
                    str(diagnostic.get("reason", event)),
                    0.0,
                    frame_metadata=frame_metadata,
                )
        self._phase_counts[phase] = self._phase_counts.get(phase, 0) + 1
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
        if self.opening_seconds is not None:
            self._retain_opening_frame(image, timestamp, trace_frame, phase, frame_metadata or {}, diagnostics, actions)
            return
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
                    phase,
                    dict(frame_metadata or {}),
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
        *, frame_metadata: dict[str, object] | None = None,
    ) -> None:
        relative = Path("events") / f"event-{self._event_requested:04d}-{kind}-lane-{lane}.png"
        self._event_requested += 1
        payload = {
            "event_index": self._event_requested - 1,
            "frame": self._trace_frames,
            "timestamp": timestamp,
            "kind": kind,
            "lane": lane,
            "reason": reason,
            "delay_seconds": round(delay, 3),
            "screenshot": None,
            "frame_metadata": frame_metadata or {},
        }
        if self.opening_seconds is not None:
            # close置位后工作线程可能仍在排空队列，也必须留到统一flush再写PNG。
            if len(self._opening_events) >= _OPENING_EVENT_CAPACITY:
                self._finish_event(payload, None, "dropped-capacity")
            else:
                self._opening_events.append((relative, image.copy(), payload))
            return
        (self.output_dir / "events").mkdir(exist_ok=True)
        if not imwrite_unicode(self.output_dir / relative, image):
            self._finish_event(payload, None, "write-failed")
            raise OSError("无法保存实时演奏异常截图")
        self._finish_event(payload, relative, "saved")

    def _finish_event(self, payload, relative, status):
        payload.update(screenshot=relative.as_posix() if relative is not None else None,
                       screenshot_status=status)
        saved = relative is not None
        if saved:
            self._event_count += 1
        else:
            self._event_dropped_reasons[status] = self._event_dropped_reasons.get(status, 0) + 1
        if payload["kind"].startswith("native-life-drop-"):
            if saved:
                self._native_life_written += 1
            else:
                self._dropped_native_life_frames += 1
        if payload["kind"].startswith("native-feedback-"):
            if saved:
                self._native_feedback_written += 1
            else:
                self._dropped_native_feedback_frames += 1
        self._events.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _flush_opening_events(self):
        self._opening_event_flush_status = "flushing"
        failure = None
        try:
            for relative, image, payload in self._opening_events:
                try:
                    (self.output_dir / "events").mkdir(exist_ok=True)
                    if not imwrite_unicode(self.output_dir / relative, image):
                        raise OSError("无法保存实时演奏异常截图")
                except Exception as exc:
                    failure = exc
                    self._finish_event(payload, None, "write-failed")
                else:
                    self._finish_event(payload, relative, "saved")
            if failure is not None:
                raise failure
        except BaseException:
            self._opening_event_flush_status = "failed"
            raise
        finally:
            self._opening_events.clear()
        self._opening_event_flush_status = "complete"

    def _retain_opening_frame(self, image, timestamp, trace_frame, phase, metadata, diagnostics, actions):
        if self._opening_first_note_at is None and actions:
            self.mark_first_note(timestamp)
        if self._opening_cutoff is not None and timestamp >= self._opening_cutoff:
            self._opening_window_reached = True
            self._opening_after_cutoff_frames += 1
            return
        sequence = metadata.get("sequence")
        source = metadata.get("source", "capture")
        repeated = (metadata.get("reused") or metadata.get("reused_frame")
                    or metadata.get("fresh") is False or metadata.get("duplicate_observation"))
        if sequence is not None:
            previous = self._opening_seen_sequence.get(source)
            repeated = repeated or (previous is not None and sequence <= previous)
            if not repeated:
                self._opening_seen_sequence[source] = sequence
        if repeated:
            self._opening_duplicate_frames += 1
            return
        reasons = []
        if phase != "engine" and "phase:"+phase not in self._opening_pins:
            reasons.append("phase:"+phase)
        for item in diagnostics:
            event = item.get("gate_event")
            event_observed_s = item.get("gate_event_observed_s")
            if (isinstance(event, dict) and event.get("event")
                and isinstance(event_observed_s, (int, float))
                and abs(event_observed_s-timestamp) <= 1e-6):
                reasons.append("gate-event:"+str(event["event"]))
        if metadata.get("black") is True:
            reasons.append("first-black")
        if any(item.get("opening_triggered") is True or item.get("triggered") is True for item in diagnostics):
            reasons.append("first-trigger")
        reasons = [reason for reason in reasons if reason not in self._opening_pins]
        before = self._opening_first_note_at is None or timestamp < self._opening_first_note_at
        if not before and self._opening_next_after_at is not None and timestamp < self._opening_next_after_at and not reasons:
            self._opening_sampled_out += 1
            return
        # 16张固定证据、首音前24张、首音后80张，另8张低频checkpoint，总计不超过128；整局事件另计。
        entry = {"image": image.copy(), "trace_frame": trace_frame, "phase": phase,
                 "observed_timestamp": float(timestamp), "frame_metadata": dict(metadata),
                 "diagnostics": diagnostics, "retention_reasons": list(reasons)}
        priority = {"phase:preparation-identity", "phase:ready-before-click", "phase:final-cover",
                    "phase:native-prearm", "phase:native-first-note-gate", "phase:first-note-gate", "first-trigger", "first-black", "gate-event:ignored-prelude", "gate-event:broad-shape-mismatch", "gate-event:broad-change-blocked"}
        for reason in reasons:
            if len(self._opening_pins) >= 16 and reason in priority:
                removable = next((key for key in self._opening_pins if key not in priority), None)
                if removable is not None:
                    del self._opening_pins[removable]
                    self._opening_capacity_evictions += 1
            if len(self._opening_pins) < 16:
                self._opening_pins[reason] = entry
        ring = self._opening_before if before else self._opening_after
        if len(ring) == ring.maxlen:
            self._opening_capacity_evictions += 1
        ring.append(entry)
        if not before:
            self._opening_next_after_at = timestamp + .25

    def _flush_opening_frames(self):
        self._opening_flush_status = "flushing"
        frames = {entry["trace_frame"]: entry for entry in (
            *self._opening_pins.values(), *self._opening_before, *self._opening_after)}
        directory = self.output_dir / "opening"
        directory.mkdir(exist_ok=True)
        with (self.output_dir / "opening_frames.jsonl").open("w", encoding="utf-8") as index:
            for number, entry in enumerate(sorted(frames.values(), key=lambda row: (row["observed_timestamp"], row["trace_frame"]))):
                relative = f"opening/frame-{number:03d}.png"
                if not imwrite_unicode(self.output_dir / relative, entry["image"]):
                    raise OSError("无法保存开场截图")
                payload = {key: value for key, value in entry.items() if key != "image"}
                payload["path"] = relative
                payload["first_note_relative_ms"] = (None if self._opening_first_note_at is None else
                    round((entry["observed_timestamp"]-self._opening_first_note_at)*1000, 3))
                index.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))+"\n")
                self._opening_written += 1
        self._opening_pins.clear()
        self._opening_before.clear()
        self._opening_after.clear()
        self._opening_flush_status = "complete"

    def _flush_opening_checkpoints(self):
        failure = None
        # 结算可能在close超时后补图，序号和索引写入由同一锁保护。
        with self._checkpoint_lock:
            try:
                for relative, image, payload in self._opening_checkpoints:
                    try:
                        (self.output_dir / relative).parent.mkdir(exist_ok=True)
                        if not imwrite_unicode(self.output_dir / relative, image):
                            raise OSError("无法保存开场阶段截图")
                    except Exception as exc:
                        failure = exc
                        self._opening_checkpoint_dropped += 1
                        payload.update(screenshot=None, screenshot_status="write-failed")
                    else:
                        payload["screenshot_status"] = "saved"
                        self._checkpoint_count += 1
                    self._write_checkpoint_index(payload)
            finally:
                self._opening_checkpoints.clear()
        if failure is not None:
            raise failure

    def _encode_video(self) -> None:
        if not self.video_enabled:
            return
        writer = None
        mapping = None
        partial_path = self.output_dir / "playfield.partial.mkv"
        final_path = self.output_dir / "playfield.mkv"
        first_timestamp = None
        last_timestamp = None
        try:
            mapping = (self.output_dir / "video_frames.jsonl").open("w", encoding="utf-8")
            while True:
                item = self._frames.get()
                if item is _SENTINEL:
                    break
                phase, metadata = None, {}
                if isinstance(item, tuple):
                    frame, trace_frame, timestamp, elapsed_ms = item[:4]
                    if len(item) > 4:
                        phase, metadata = item[4:6]
                else:
                    frame, trace_frame, timestamp, elapsed_ms = item, -1, float("nan"), None
                if writer is None:
                    height, width = frame.shape[:2]
                    writer = cv2.VideoWriter(str(partial_path), cv2.VideoWriter_fourcc(*"MJPG"),
                        float(self.video_fps), (width, height))
                    if not writer.isOpened():
                        raise OSError("无法创建实时调试录像")
                writer.write(frame)
                mapping.write(json.dumps({"encoded_frame": self._video_frames, "trace_frame": int(trace_frame),
                    "monotonic_timestamp": timestamp, "elapsed_ms": elapsed_ms,
                    "phase": phase, "frame_metadata": metadata}, ensure_ascii=False, separators=(",", ":"))+"\n")
                if timestamp == timestamp:
                    first_timestamp = timestamp if first_timestamp is None else first_timestamp
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
            self._verify_and_publish_video(partial_path, final_path,
                first_timestamp=first_timestamp, last_timestamp=last_timestamp)
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
        self._opening_flush_allowed.set()
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
