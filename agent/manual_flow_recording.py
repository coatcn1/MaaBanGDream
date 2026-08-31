from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    from .task_reporting import record_failure_reason
except ImportError:
    from task_reporting import record_failure_reason


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECORDING_ROOT = PROJECT_ROOT / "debug" / "recordings"
_SAFE_LABEL = re.compile(r"[^a-z0-9-]+")
_SETTINGS_LOCK = threading.Lock()
_MANUAL_FLOW_SETTINGS: dict[str, object] = {
    "fps": 10,
    "max_duration_seconds": 900,
    "show_touches": True,
}


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def configure_manual_flow_settings(params: dict[str, object]) -> dict[str, object]:
    allowed = {"fps", "max_duration_seconds", "show_touches"}
    with _SETTINGS_LOCK:
        if bool(params.get("reset", False)):
            _MANUAL_FLOW_SETTINGS.clear()
            _MANUAL_FLOW_SETTINGS.update(
                {
                    "fps": 10,
                    "max_duration_seconds": 900,
                    "show_touches": True,
                }
            )
        for key in allowed:
            if key in params:
                _MANUAL_FLOW_SETTINGS[key] = params[key]
        return dict(_MANUAL_FLOW_SETTINGS)


def current_manual_flow_settings() -> dict[str, object]:
    with _SETTINGS_LOCK:
        return dict(_MANUAL_FLOW_SETTINGS)


class ManualFlowVideoWriter:
    """Write a seekable, timestamp-indexed UI recording without device data."""

    def __init__(
        self,
        root: Path,
        *,
        fps: int,
        label: str = "general",
    ) -> None:
        if fps not in {5, 10, 15}:
            raise ValueError("手动流程录像帧率仅支持5、10或15 FPS")
        safe_label = _SAFE_LABEL.sub("-", label.strip().lower()).strip("-")
        safe_label = safe_label or "general"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.output_dir = root / f"manual-flow-{safe_label}-{stamp}"
        self.output_dir.mkdir(parents=True, exist_ok=False)
        self.fps = fps
        self._partial_path = self.output_dir / "screen.partial.mkv"
        self.video_path = self.output_dir / "screen.mkv"
        self._mapping_path = self.output_dir / "frames.jsonl"
        self._mapping = self._mapping_path.open("w", encoding="utf-8")
        self._writer: cv2.VideoWriter | None = None
        self._timestamps: list[float] = []
        self._first_frame: np.ndarray | None = None
        self._last_frame: np.ndarray | None = None
        self._resolution: tuple[int, int] | None = None
        self._closed = False

    @property
    def frame_count(self) -> int:
        return len(self._timestamps)

    def record(self, image: np.ndarray, timestamp: float) -> None:
        if self._closed:
            raise RuntimeError("不能向已关闭的手动流程录像写入画面")
        if image is None or image.ndim != 3 or image.shape[2] != 3:
            raise ValueError("模拟器截图不是有效的BGR三通道画面")
        height, width = image.shape[:2]
        resolution = (width, height)
        if self._writer is None:
            self._resolution = resolution
            self._writer = cv2.VideoWriter(
                str(self._partial_path),
                cv2.VideoWriter_fourcc(*"MJPG"),
                float(self.fps),
                resolution,
            )
            if not self._writer.isOpened():
                raise OSError("无法创建手动流程录像")
            self._first_frame = image.copy()
        elif resolution != self._resolution:
            raise ValueError(
                f"录像期间截图分辨率发生变化：{self._resolution} -> {resolution}"
            )
        self._writer.write(image)
        self._last_frame = image.copy()
        frame_index = len(self._timestamps)
        first_timestamp = self._timestamps[0] if self._timestamps else timestamp
        self._timestamps.append(float(timestamp))
        self._mapping.write(
            json.dumps(
                {
                    "frame": frame_index,
                    "monotonic_timestamp": float(timestamp),
                    "elapsed_ms": round((timestamp - first_timestamp) * 1000, 3),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )

    def _verify_video(self) -> dict[str, object]:
        if not self._partial_path.is_file() or self._partial_path.stat().st_size <= 0:
            raise OSError("手动流程录像文件没有生成")
        capture = cv2.VideoCapture(str(self._partial_path))
        try:
            if not capture.isOpened():
                raise OSError("手动流程录像无法重新打开验证")
            actual_fps = float(capture.get(cv2.CAP_PROP_FPS))
            actual_frames = int(round(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
            if actual_frames != self.frame_count:
                raise OSError(
                    f"录像帧数不一致：{actual_frames} != {self.frame_count}"
                )
            if abs(actual_fps - float(self.fps)) > 0.1:
                raise OSError(
                    f"录像帧率不一致：{actual_fps:.3f} != {self.fps}"
                )
            for index in sorted({0, actual_frames // 2, actual_frames - 1}):
                if not capture.set(cv2.CAP_PROP_POS_FRAMES, index):
                    raise OSError(f"录像无法定位到第{index}帧")
                ok, frame = capture.read()
                if not ok or frame is None:
                    raise OSError(f"录像第{index}帧无法解码")
        finally:
            capture.release()
        return {
            "video_actual_fps": round(actual_fps, 3),
            "video_seek_verified": True,
        }

    def close(self, *, stop_reason: str, touch_indicators: bool) -> Path:
        if self._closed:
            return self.video_path
        self._closed = True
        if self._writer is not None:
            self._writer.release()
        self._mapping.flush()
        self._mapping.close()
        if self.frame_count == 0:
            raise RuntimeError("手动流程录像没有获得任何画面")
        verification = self._verify_video()
        os.replace(self._partial_path, self.video_path)
        if self._first_frame is not None:
            if not cv2.imwrite(str(self.output_dir / "first-frame.png"), self._first_frame):
                raise OSError("无法保存录像首帧")
        if self._last_frame is not None:
            if not cv2.imwrite(str(self.output_dir / "last-frame.png"), self._last_frame):
                raise OSError("无法保存录像末帧")

        intervals_ms = [
            (current - previous) * 1000
            for previous, current in zip(self._timestamps, self._timestamps[1:])
        ]
        timestamp_duration = max(0.0, self._timestamps[-1] - self._timestamps[0])
        effective_fps = (
            (self.frame_count - 1) / timestamp_duration
            if self.frame_count > 1 and timestamp_duration > 0
            else 0.0
        )
        width, height = self._resolution or (0, 0)
        summary = {
            "schema_version": 1,
            "kind": "manual-ui-flow",
            "created_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "stop_reason": stop_reason,
            "touch_indicators": bool(touch_indicators),
            "video": "screen.mkv",
            "frame_index": "frames.jsonl",
            "first_frame": "first-frame.png",
            "last_frame": "last-frame.png",
            "codec": "MJPG",
            "container": "matroska",
            "width": width,
            "height": height,
            "configured_fps": self.fps,
            "effective_capture_fps": round(effective_fps, 3),
            "frame_count": self.frame_count,
            "timestamp_duration_seconds": round(timestamp_duration, 3),
            "video_size_bytes": self.video_path.stat().st_size,
            "capture_interval_ms": {
                "median": round(statistics.median(intervals_ms), 3)
                if intervals_ms
                else None,
                "p95": round(_percentile(intervals_ms, 0.95), 3)
                if intervals_ms
                else None,
                "maximum": round(max(intervals_ms), 3) if intervals_ms else None,
            },
            **verification,
        }
        (self.output_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return self.video_path

    def close_without_video(self, *, stop_reason: str) -> None:
        """Close an empty recording after an early stop or capture failure."""
        if self._closed:
            return
        self._closed = True
        if self._writer is not None:
            self._writer.release()
        self._mapping.flush()
        self._mapping.close()
        (self.output_dir / "summary.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "manual-ui-flow",
                    "created_at": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "stop_reason": stop_reason,
                    "frame_count": 0,
                    "video": None,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


def capture_manual_flow(
    capture: Callable[[], np.ndarray],
    stopping: Callable[[], bool],
    recorder: ManualFlowVideoWriter,
    *,
    fps: int,
    max_duration_seconds: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> str:
    interval = 1.0 / fps
    started_at = monotonic()
    next_capture_at = started_at
    while True:
        if stopping():
            return "user_stop"
        now = monotonic()
        if now - started_at >= max_duration_seconds:
            return "duration_limit"
        if now < next_capture_at:
            sleeper(min(0.05, next_capture_at - now))
            continue
        image = capture()
        captured_at = monotonic()
        recorder.record(image, captured_at)
        next_capture_at += interval
        if next_capture_at <= captured_at:
            missed = int((captured_at - next_capture_at) / interval) + 1
            next_capture_at += missed * interval


@dataclass(frozen=True, repr=False)
class RuntimeAdbDevice:
    adb_path: Path
    serial: str

    def __repr__(self) -> str:
        return "RuntimeAdbDevice(<redacted>)"


def _load_runtime_adb_device() -> RuntimeAdbDevice:
    roots: list[Path] = []
    configured_root = os.environ.get("MAABANGDREAM_MFA_ROOT", "").strip()
    if configured_root:
        roots.append(Path(configured_root))
    roots.extend(
        [
            Path.cwd(),
            PROJECT_ROOT.parent / ".tools" / "MFAAvalonia-profile-v3",
        ]
    )
    seen: set[Path] = set()
    for root in roots:
        resolved = root.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        instance_root = resolved / "config" / "instances"
        candidates = sorted(instance_root.glob("*.json"))
        default = instance_root / "default.json"
        ordered = [default, *(item for item in candidates if item != default)]
        for config_path in ordered:
            if not config_path.is_file():
                continue
            try:
                payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            device = payload.get("AdbDevice")
            if not isinstance(device, dict):
                continue
            adb_path = Path(str(device.get("AdbPath", "")))
            serial = str(device.get("AdbSerial", "")).strip()
            if adb_path.is_file() and serial:
                return RuntimeAdbDevice(adb_path=adb_path, serial=serial)
    raise FileNotFoundError("没有找到当前MFA实例的ADB设备配置")


def _build_adb_shell(device: RuntimeAdbDevice) -> Callable[[tuple[str, ...]], str]:
    def run(arguments: tuple[str, ...]) -> str:
        completed = subprocess.run(
            [
                str(device.adb_path),
                "-s",
                device.serial,
                "shell",
                *arguments,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"ADB Shell执行失败：{detail or '无错误详情'}")
        return completed.stdout.strip()

    return run


class TouchIndicatorGuard:
    def __init__(
        self,
        shell: Callable[[tuple[str, ...]], str],
        *,
        enabled: bool,
    ) -> None:
        self._shell = shell
        self._enabled = enabled
        self._original: str | None = None
        self.active = False

    def __enter__(self) -> "TouchIndicatorGuard":
        if not self._enabled:
            return self
        try:
            original = self._shell(
                ("settings", "get", "system", "show_touches"),
            )
            self._original = original if original in {"0", "1"} else "0"
            self._shell(("settings", "put", "system", "show_touches", "1"))
            self.active = True
        except Exception as exc:
            print(f"ManualFlowRecording touch indicators unavailable: {exc}", flush=True)
        return self

    def __exit__(self, exc_type, exc, traceback_object) -> None:
        if self._original is None:
            return
        try:
            self._shell(
                ("settings", "put", "system", "show_touches", self._original),
            )
        except Exception as restore_exc:
            print(
                "ManualFlowRecording failed to restore touch indicators: "
                f"{restore_exc}",
                flush=True,
            )


@AgentServer.custom_action("ManualFlowRecording")
class ManualFlowRecording(CustomAction):
    """Record any manually operated emulator UI flow until MFA is stopped."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            return self._run(context, argv)
        except Exception as exc:
            if context.tasker.stopping:
                return True
            reason = f"{type(exc).__name__}: {exc}"
            record_failure_reason(reason)
            traceback.print_exc()
            print(f"ManualFlowRecording failed={reason}", flush=True)
            return False

    def _run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        params = current_manual_flow_settings()
        fps = int(params.get("fps", 10))
        max_duration_seconds = float(params.get("max_duration_seconds", 900))
        show_touches = bool(params.get("show_touches", True))
        if fps not in {5, 10, 15}:
            raise ValueError("手动流程录像帧率仅支持5、10或15 FPS")
        if not 60 <= max_duration_seconds <= 1800:
            raise ValueError("手动流程录像最长时间必须在1到30分钟之间")
        if context.tasker.stopping:
            return True

        recorder = ManualFlowVideoWriter(
            DEFAULT_RECORDING_ROOT,
            fps=fps,
            label="general",
        )
        stop_reason = "failed"
        indicator_active = False
        adb_shell: Callable[[tuple[str, ...]], str] | None = None
        if show_touches:
            try:
                adb_shell = _build_adb_shell(_load_runtime_adb_device())
            except Exception as exc:
                print(
                    f"ManualFlowRecording touch indicators unavailable: {exc}",
                    flush=True,
                )
        try:
            with TouchIndicatorGuard(
                adb_shell or (lambda arguments: ""),
                enabled=show_touches and adb_shell is not None,
            ) as indicators:
                indicator_active = indicators.active
                print(
                    "ManualFlowRecording started "
                    f"fps={fps} max_seconds={max_duration_seconds:g} "
                    f"touch_indicators={indicator_active} "
                    f"output={recorder.output_dir}",
                    flush=True,
                )
                try:
                    stop_reason = capture_manual_flow(
                        lambda: context.tasker.controller.post_screencap()
                        .wait()
                        .get(),
                        lambda: context.tasker.stopping,
                        recorder,
                        fps=fps,
                        max_duration_seconds=max_duration_seconds,
                    )
                except Exception:
                    if context.tasker.stopping:
                        stop_reason = "user_stop"
                    else:
                        raise
        finally:
            if recorder.frame_count:
                video_path = recorder.close(
                    stop_reason=stop_reason,
                    touch_indicators=indicator_active,
                )
                print(
                    "ManualFlowRecording saved "
                    f"frames={recorder.frame_count} video={video_path}",
                    flush=True,
                )
            else:
                recorder.close_without_video(stop_reason=stop_reason)

        return True


@AgentServer.custom_action("ManualFlowRecordingConfigure")
class ManualFlowRecordingConfigure(CustomAction):
    """Merge one independent MFA option into the next recording's settings."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        if context.tasker.stopping:
            return True
        try:
            params = json.loads(argv.custom_action_param or "{}")
            settings = configure_manual_flow_settings(params)
            print(
                "ManualFlowRecording configured "
                f"fps={settings['fps']} "
                f"max_seconds={settings['max_duration_seconds']} "
                f"show_touches={settings['show_touches']}",
                flush=True,
            )
            return True
        except Exception as exc:
            record_failure_reason(f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
            return False
