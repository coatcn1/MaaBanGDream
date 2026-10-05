"""协力开演前证据：只记录已经取得的帧，不增加截图或游戏输入。"""

from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2

from .vision_io import imwrite_unicode
from .final_cover import MEMBER_LOADING_ICON_THRESHOLD


_ACTIVE = {}


def startup_recorder(run_id):
    return _ACTIVE.get(run_id)


def cover_diagnostic(resolver):
    return {
        "loading_guard_enabled": bool(getattr(resolver, "reject_member_loading", False)),
        "member_loading": bool(getattr(resolver, "last_member_loading_detected", False)),
        "member_loading_score": getattr(resolver, "last_member_loading_score", None),
        "member_loading_threshold": MEMBER_LOADING_ICON_THRESHOLD,
        "reason": getattr(resolver, "last_reason", "missing resolver"),
        "cover_frames": getattr(resolver, "frames", 0),
        "stable_frames": getattr(resolver, "_candidate_frames", 0),
        "candidate_song_id": getattr(resolver, "_candidate_song_id", None),
        "final_title_confirmed": bool(getattr(resolver, "_final_title_confirmed", False)),
        "observed_title": getattr(resolver, "observed_title", None),
        "title_confidence": getattr(resolver, "observed_title_confidence", None),
        "title_observation": getattr(resolver, "last_title_diagnostic", {}),
    }


class CooperativeStartupRecorder:
    def __init__(self, root: Path, run_id, *, queue_capacity=8, max_frames=600,
                 max_events=10000, max_bytes=128 * 1024 * 1024, close_timeout=0.2,
                 loading_guard_enabled=None):
        self.output_dir = Path(root) / (
            "cooperative-startup-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        )
        self.output_dir.mkdir(parents=True)
        self.run_id = run_id
        self.loading_guard_enabled = loading_guard_enabled
        self.max_frames = max_frames
        self.max_events = max_events
        self.max_bytes = max_bytes
        self.close_timeout = close_timeout
        self._queue = queue.Queue(maxsize=queue_capacity)
        self._events = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self._closed = False
        self._submitted = 0
        self._saved = 0
        self._dropped = 0
        self._errors = 0
        self._event_drops = 0
        self._bytes = 0
        self._finalized = False
        self._last_image = None
        self._last_event = None
        self._last_frame_bytes = 0
        self._previous_at = None
        self._outcome = "observing"
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        if run_id is not None:
            _ACTIVE[run_id] = self

    def record(self, image, timestamp, details):
        if self._closed:
            return
        with self._lock:
            event = {
                "frame_index": len(self._events) + self._event_drops, "monotonic": float(timestamp),
                "interval_ms": (None if self._previous_at is None
                                else (timestamp - self._previous_at) * 1000),
                **details, "image_status": "not-requested",
            }
            self._previous_at = timestamp
            # 额外只留一张末帧；即使普通 PNG 达到限额，失败决定仍有原图对应。
            if image is not None:
                copied = image.copy()
                self._last_image = copied
                self._last_event = {**event, "image": "last-frame.png", "image_status": "pending"}
            if len(self._events) >= self.max_events:
                self._event_drops += 1
                return
            self._events.append(event)
            if image is None:
                return
            # PNG 丢帧仍保留对应判定，不能把写盘队列满误当成没有采样。
            if self._submitted >= self.max_frames or self._queue.full():
                self._dropped += 1
                event["image_status"] = "dropped"
                return
            event["image_status"] = "queued"
            event["image"] = f"frame-{event['frame_index']:05d}.png"
            self._queue.put_nowait((copied, event))
            self._submitted += 1

    def _worker(self):
        last_flush = time.monotonic()
        try:
            while not self._stop.is_set() or not self._queue.empty():
                if time.monotonic() - last_flush >= 1.0:
                    # 低频保存判定，进程意外退出也能把已写 PNG 与原始判定关联。
                    self._persist(finalized=False)
                    last_flush = time.monotonic()
                try:
                    image, event = self._queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    if self._bytes + image.nbytes > self.max_bytes:
                        with self._lock:
                            event["image_status"] = "dropped-storage-limit"
                            self._dropped += 1
                        continue
                    if not imwrite_unicode(self.output_dir / event["image"], image,
                                           [cv2.IMWRITE_PNG_COMPRESSION, 1]):
                        raise OSError("无法保存协力启动帧")
                    with self._lock:
                        event["image_status"] = "saved"
                        self._saved += 1
                        self._bytes += (self.output_dir / event["image"]).stat().st_size
                except Exception as exc:
                    with self._lock:
                        event["image_status"] = "write-error"
                        event["write_error"] = f"{type(exc).__name__}: {exc}"
                        self._errors += 1
                finally:
                    self._queue.task_done()
        finally:
            if self._last_image is not None:
                try:
                    if not imwrite_unicode(self.output_dir / "last-frame.png", self._last_image,
                                           [cv2.IMWRITE_PNG_COMPRESSION, 1]):
                        raise OSError("无法保存协力启动末帧")
                    self._last_event["image_status"] = "saved"
                    self._last_frame_bytes = (self.output_dir / "last-frame.png").stat().st_size
                except Exception as exc:
                    self._last_event["image_status"] = "write-error"
                    self._last_event["write_error"] = f"{type(exc).__name__}: {exc}"
                    self._errors += 1
            self._persist(finalized=True)

    def _persist(self, *, finalized):
        try:
            with self._write_lock:
                if self._finalized and not finalized:
                    return
                self._finalized = finalized
                with self._lock:
                    events = [dict(event) for event in self._events]
                    summary = {
                        "run_id": self.run_id, "outcome": self._outcome,
                        "startup_policy": "final-cover-first",
                        "loading_guard_enabled": self.loading_guard_enabled,
                        "frames_observed": len(events), "frames_saved": self._saved,
                        "frames_dropped": self._dropped, "write_errors": self._errors,
                        "events_dropped": self._event_drops,
                        "finalized": finalized, "close_pending": self._closed and not finalized,
                        "recording": not self._closed,
                        "max_frames": self.max_frames, "max_events": self.max_events,
                        "bytes_saved": self._bytes, "max_bytes": self.max_bytes,
                        "last_frame": self._last_event,
                        "last_frame_additional_bytes": self._last_frame_bytes,
                    }
                (self.output_dir / "observations.jsonl").write_text(
                    "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
                    encoding="utf-8",
                )
                (self.output_dir / "summary.json").write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
                )
        except Exception as exc:
            print(f"CooperativeStartup diagnostics_warning={type(exc).__name__}: {exc}", flush=True)

    def close(self, outcome="finished"):
        if self._closed:
            return
        self._closed = True
        self._outcome = str(outcome)
        if _ACTIVE.get(self.run_id) is self:
            _ACTIVE.pop(self.run_id)
        self._stop.set()
        self._thread.join(timeout=self.close_timeout)
        if self._thread.is_alive():
            # 有界等待后明确标记仍在写盘；工作线程完成时会补齐最终摘要。
            threading.Thread(target=lambda: self._persist(finalized=False), daemon=True).start()
