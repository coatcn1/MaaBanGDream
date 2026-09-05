"""C++ Native 演奏后端的设备编排、首拍门控与会话生命周期。"""

from __future__ import annotations

import time
import threading
import uuid
import queue
import math
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from . import native_engine
from .native_minitouch import NativeMinitouchDevice
from .playfield_monitor import LANE_CENTERS, PlayfieldDetector
from .prepare_popup import CooperativePreparePopupDetector


TOUCH_Y = 590.0
PHOTOGATE_LATENCY_MS = 190.0


@dataclass(frozen=True, slots=True)
class NativeStartGatePolicy:
    """首音门控只区分进入阶段，不引入按模式变化的歌曲偏移。"""

    mode: str
    stable_duration_ms: float
    grace_ms: float


def resolve_native_start_gate_policy(run_mode: str | None) -> NativeStartGatePolicy:
    """协力已确认生命条出现，因此只需要更短的基线稳定窗口。"""
    normalized = str(run_mode or "realtime").strip().lower()
    if normalized == "cooperative":
        return NativeStartGatePolicy(
            mode="cooperative-playfield-confirmed",
            stable_duration_ms=120.0,
            grace_ms=500.0,
        )
    return NativeStartGatePolicy(
        mode="single-playfield-first-note",
        stable_duration_ms=250.0,
        grace_ms=500.0,
    )


@dataclass(slots=True)
class _ExpectedCommand:
    """一条已发布命令及其可选高层动作回执。"""

    command: str
    chunk_sequence: int
    receipts: tuple[dict[str, object], ...] = ()
    last_in_chunk: bool = False
    used_offsets: Any | None = None


class NativeStartPhotogate:
    """用判定线附近的整行颜色变化定位第一颗音符。"""

    # 协力弹窗及其背景变暗会让判定带大面积同向变化；首颗音符只影响少数
    # 相邻轨道列。逐列变化超过该分量的列数占比过大时视为弹窗转场。
    _BROAD_COLUMN_MIN = 45.0
    _BROAD_COLUMN_FRACTION = 0.35

    def __init__(
        self,
        *,
        from_row: int = 510,
        to_row: int = 535,
        reference_height: int = 720,
        stable_duration_ms: float = 250.0,
        grace_ms: float = 500.0,
        change_threshold: float = 3.0,
        latency_ms: float = PHOTOGATE_LATENCY_MS,
        mode: str = "single-playfield-first-note",
        playfield_detector: Callable[[Any], bool] | None = None,
        popup_detector: Callable[[Any], bool] | None = None,
        suppress_prepare_popup: bool | None = None,
    ) -> None:
        if not 0 <= from_row <= to_row < reference_height:
            raise ValueError("photogate 行范围无效")
        if stable_duration_ms < 0:
            raise ValueError("stable_duration_ms 不能为负数")
        if grace_ms < 0:
            raise ValueError("grace_ms 不能为负数")
        if change_threshold <= 0:
            raise ValueError("change_threshold 必须大于 0")
        if latency_ms < 0:
            raise ValueError("latency_ms 不能为负数")
        self._from_row = int(from_row)
        self._to_row = int(to_row)
        self._reference_height = int(reference_height)
        self._stable_duration_s = float(stable_duration_ms) / 1000.0
        self._grace_s = float(grace_ms) / 1000.0
        self._change_threshold = float(change_threshold)
        self._latency_s = float(latency_ms) / 1000.0
        if playfield_detector is None:
            self._playfield_detector = PlayfieldDetector()
        else:
            self._playfield_detector = playfield_detector
        self.mode = str(mode)
        # 协力进入演奏场后可能出现“其他成员正在准备中”弹窗；其出现/消失
        # 会让整行颜色发生大变化，被首拍门控误判成第一颗音符。默认仅对
        # 协力策略启用弹窗门控，单人/校准/挑战保持原有行为。
        if suppress_prepare_popup is None:
            suppress_prepare_popup = str(mode).startswith("cooperative")
        self._popup_gate_enabled = bool(suppress_prepare_popup)
        self._popup_detector = (
            popup_detector
            if popup_detector is not None
            else (
                CooperativePreparePopupDetector()
                if self._popup_gate_enabled
                else None
            )
        )
        self._last_color: Any | None = None
        # 冻结时保存判定带逐列基线，用于区分“整行变暗/弹窗”与“窄列音符”。
        self._frozen_columns: np.ndarray | None = None
        self._previous_change: float | None = None
        self._previous_frame_s: float | None = None
        self._observed_since_s: float | None = None
        self.stable_since_s: float | None = None
        self.frozen_at_s: float | None = None
        self.waited_frames = 0
        self.frozen = False
        self.triggered = False
        self.last_change_score: float | None = None
        self.trigger_score: float | None = None
        self.trigger_source: str | None = None
        self.ignored_prelude_events = 0
        self.triggered_at_s: float | None = None
        self.playfield_seen_at_s: float | None = None
        self.playfield_waited_frames = 0
        self.playfield_loss_events = 0
        self._playfield_active = False
        self._prepare_popup_active = False
        self.prepare_popup_frames = 0
        self.prepare_popup_blocked_events = 0
        self._significant_events: deque[dict[str, object]] = deque(maxlen=32)

    def _reset_band_state(self) -> None:
        """演奏场尚未成立或短暂消失时，丢弃此前加载页颜色基线。"""
        self._last_color = None
        self._frozen_columns = None
        self._previous_change = None
        self._previous_frame_s = None
        self.stable_since_s = None
        self.frozen_at_s = None
        self.waited_frames = 0
        self.frozen = False

    def _record_event(
        self,
        event: str,
        frame_s: float,
        change_score: float,
    ) -> None:
        """只保留有界关键事件，避免逐帧日志反过来干扰实时路径。"""
        elapsed_ms = (
            (frame_s - self._observed_since_s) * 1000.0
            if self._observed_since_s is not None
            else 0.0
        )
        self._significant_events.append({
            "event": event,
            "elapsed_ms": elapsed_ms,
            "change_score": change_score,
        })

    def report(self) -> dict[str, object]:
        """输出足够复盘首音误触发或漏触发的状态。"""
        wait_ms = (
            (self.triggered_at_s - self._observed_since_s) * 1000.0
            if self.triggered_at_s is not None
            and self._observed_since_s is not None
            else None
        )
        stable_ms = (
            (self.frozen_at_s - self.stable_since_s) * 1000.0
            if self.frozen_at_s is not None and self.stable_since_s is not None
            else None
        )
        playfield_wait_ms = (
            (self.playfield_seen_at_s - self._observed_since_s) * 1000.0
            if self.playfield_seen_at_s is not None
            and self._observed_since_s is not None
            else None
        )
        return {
            "photogate_mode": self.mode,
            "photogate_playfield_evidence": "life-and-judgement-line",
            "photogate_latency_ms": self._latency_s * 1000.0,
            "photogate_wait_ms": wait_ms,
            "photogate_playfield_wait_ms": playfield_wait_ms,
            "photogate_playfield_waited_frames": self.playfield_waited_frames,
            "photogate_playfield_loss_events": self.playfield_loss_events,
            "photogate_stable_ms": stable_ms,
            "photogate_grace_ms": self._grace_s * 1000.0,
            "photogate_waited_frames": self.waited_frames,
            "photogate_ignored_prelude_events": self.ignored_prelude_events,
            "photogate_trigger_score": self.trigger_score,
            "photogate_trigger_source": self.trigger_source,
            "photogate_last_change_score": self.last_change_score,
            "photogate_prepare_popup_enabled": self._popup_gate_enabled,
            "photogate_prepare_popup_frames": self.prepare_popup_frames,
            "photogate_prepare_popup_blocked_events": (
                self.prepare_popup_blocked_events
            ),
            "photogate_events": list(self._significant_events),
        }

    def observe(self, image: Any, now: float) -> float | None:
        """返回第一颗音符的绝对执行时刻；未触发时返回 ``None``。"""
        if self.triggered:
            return None
        if getattr(image, "ndim", 0) != 3 or image.shape[2] < 3:
            raise ValueError("photogate 需要 HxWx3 图像")
        height = int(image.shape[0])
        if height < 1:
            raise ValueError("photogate 图像高度无效")
        scale = height / self._reference_height
        from_row = max(0, min(height - 1, round(self._from_row * scale)))
        to_row = max(
            from_row,
            min(height - 1, round(self._to_row * scale)),
        )
        # 先转浮点，避免 uint8 相减在颜色下降时发生无符号回绕。
        current = image[from_row : to_row + 1, :, :3].astype("float64").mean(
            axis=(0, 1)
        )
        frame_s = float(now)
        if self._observed_since_s is None:
            self._observed_since_s = frame_s
        if not bool(self._playfield_detector(image)):
            self.playfield_waited_frames += 1
            if self._playfield_active:
                self.playfield_loss_events += 1
                self._record_event("playfield-lost", frame_s, 0.0)
            self._playfield_active = False
            self._prepare_popup_active = False
            self._reset_band_state()
            return None
        if not self._playfield_active:
            self._playfield_active = True
            self.playfield_seen_at_s = frame_s
            self._reset_band_state()
            self._record_event("playfield-visible", frame_s, 0.0)
        if self._popup_gate_enabled:
            # 弹窗存在时不允许建立颜色基线，也不允许首拍触发；弹窗消失
            # 的那一帧同样只重置基线，避免把弹窗淡出当成第一颗音符。
            if self._popup_detector is None:
                raise RuntimeError("协力弹窗门控缺少检测器")
            popup_visible = bool(self._popup_detector(image))
            if popup_visible:
                self.prepare_popup_frames += 1
                if not self._prepare_popup_active:
                    self._prepare_popup_active = True
                    self.prepare_popup_blocked_events += 1
                    self._record_event(
                        "prepare-popup-visible",
                        frame_s,
                        0.0,
                    )
                self._reset_band_state()
                return None
            if self._prepare_popup_active:
                self._prepare_popup_active = False
                self.prepare_popup_blocked_events += 1
                self._record_event("prepare-popup-gone", frame_s, 0.0)
                self._reset_band_state()
                return None
        if self._last_color is None:
            self._last_color = current
            self._previous_frame_s = frame_s
            return None
        change_score = float(abs(current - self._last_color).sum())
        self.last_change_score = change_score

        if not self.frozen:
            if change_score <= self._change_threshold:
                if self.stable_since_s is None:
                    self.stable_since_s = (
                        self._previous_frame_s
                        if self._previous_frame_s is not None
                        else frame_s
                    )
                    self.waited_frames = 1
                else:
                    self.waited_frames += 1
                if frame_s - self.stable_since_s >= self._stable_duration_s:
                    self.frozen = True
                    self.frozen_at_s = frame_s
                    self._frozen_columns = image[
                        from_row : to_row + 1, :, :3
                    ].astype("float64").mean(axis=0)
                    self._record_event("stable", frame_s, change_score)
            else:
                # 必须连续稳定；开场动画的任一显著变化都会重新计时。
                self._record_event("stability-reset", frame_s, change_score)
                self.stable_since_s = None
                self.waited_frames = 0
            self._previous_change = change_score
            self._previous_frame_s = frame_s
            self._last_color = current
            return None

        assert self.frozen_at_s is not None
        if frame_s - self.frozen_at_s < self._grace_s:
            if change_score >= self._change_threshold:
                self.ignored_prelude_events += 1
                self._record_event("ignored-prelude", frame_s, change_score)
            self._previous_change = change_score
            self._previous_frame_s = frame_s
            self._last_color = current
            return None

        if (
            change_score >= self._change_threshold
            and self._popup_gate_enabled
            and self._frozen_columns is not None
        ):
            # 弹窗缩放出现/消失或背景变暗时，判定带会发生大面积变化；首颗
            # 音符只改变少数相邻轨道列。逐列比较冻结基线，变化列占比过高
            # 就判定为弹窗转场，重置基线后继续等待真正的首音。
            current_columns = image[
                from_row : to_row + 1, :, :3
            ].astype("float64").mean(axis=0)
            column_change = np.abs(
                current_columns - self._frozen_columns
            ).sum(axis=1)
            broad_columns = int(
                (column_change >= self._BROAD_COLUMN_MIN).sum()
            )
            if broad_columns >= self._BROAD_COLUMN_FRACTION * image.shape[1]:
                self._record_event(
                    "broad-change-blocked",
                    frame_s,
                    change_score,
                )
                self._reset_band_state()
                return None

        trigger_s: float | None = None
        trigger_source: str | None = None
        if (
            self._previous_change is not None
            and self._previous_change < self._change_threshold <= change_score
            and self._previous_frame_s is not None
        ):
            fraction = (
                (self._change_threshold - self._previous_change)
                / max(change_score - self._previous_change, 1e-9)
            )
            trigger_s = self._previous_frame_s + fraction * (
                frame_s - self._previous_frame_s
            )
            trigger_source = "interpolated-threshold-crossing"
        elif change_score >= self._change_threshold:
            trigger_s = frame_s
            trigger_source = "direct-threshold"

        if trigger_s is not None:
            self.triggered = True
            self.triggered_at_s = frame_s
            self.trigger_score = change_score
            self.trigger_source = trigger_source
            self._record_event("trigger", frame_s, change_score)
            self._last_color = current
            return trigger_s + self._latency_s

        self._previous_change = change_score
        self._previous_frame_s = frame_s
        self._last_color = current
        return None


class NativeMinitouchBackend:
    """显式启用后独占输入，并以 C++ PlaybackSession 滚动发布。"""

    exclusive = True

    def __init__(
        self,
        chart_path: str | Path,
        *,
        adb_path: str,
        serial: str,
        judgement_y: float = TOUCH_Y,
        lane_centers: tuple[float, ...] = LANE_CENTERS,
        press_bias_ms: int = 0,
        max_wait_ms: int = 250,
        clock: Callable[[], float] = time.perf_counter,
        initial_offsets: dict[str, float] | None = None,
        photogate: NativeStartPhotogate | None = None,
        start_gate_mode: str = "realtime",
        device: NativeMinitouchDevice | None = None,
        session_factory: Callable[..., Any] | None = None,
        run_id: str | None = None,
        jlog_path: str | Path | None = None,
        publisher_poll_ms: float = 20.0,
        require_probe: bool | None = None,
    ) -> None:
        if not native_engine.available():
            raise RuntimeError(
                "Native 模块不可用："
                f"{native_engine.unavailable_reason() or 'unknown'}"
            )
        self._timeline = native_engine.compile_chart(chart_path)
        self._actions: list[dict[str, object]] = list(
            self._timeline.compile_actions({})
        )
        if not self._actions:
            raise RuntimeError("谱面没有可编译的动作")
        self._clock = clock
        self._config = {
            "judgement_y": float(judgement_y),
            "press_bias_ms": int(press_bias_ms),
            "max_wait_ms": int(max_wait_ms),
            "lane_centers": list(lane_centers),
        }
        self._frozen_timing_offset_ms = int(press_bias_ms)
        self._frozen_offsets = dict(initial_offsets or {})
        self._compiler = native_engine.touch_script_compiler(
            offsets=self._frozen_offsets
        )
        self._receipt_reader = getattr(
            self._compiler, "execution_receipts", None
        )
        if self._receipt_reader is None:
            self._receipt_reader = getattr(
                self._compiler, "last_execution_receipts", None
            )
        if self._receipt_reader is None:
            raise RuntimeError("Native 模块缺少动作执行回执接口")
        self._calibrator = native_engine.latency_calibrator()
        self._run_id = run_id or str(uuid.uuid4())
        self._jlog_path = Path(jlog_path) if jlog_path is not None else None
        self._device = device or NativeMinitouchDevice(
            adb_path,
            serial,
            jlog_path=self._jlog_path,
        )
        self._device_start_cancel = threading.Event()
        self._device_start_commit_lock = threading.Lock()
        self._device_cleanup_lock = threading.Lock()
        self._require_probe = (
            device is None if require_probe is None else bool(require_probe)
        )
        start_policy = resolve_native_start_gate_policy(start_gate_mode)
        self._photogate = photogate or NativeStartPhotogate(
            stable_duration_ms=start_policy.stable_duration_ms,
            grace_ms=start_policy.grace_ms,
            mode=start_policy.mode,
        )
        self._session_factory = session_factory or native_engine.playback_session
        self._session = self._session_factory(
            publish=self._publish_chunk,
            request_reset=self._device.request_reset,
            fallback_stop=lambda: self._emergency_stop_device_with_budget(0.08),
            clock=self._clock,
            config={
                "lookahead_s": 0.500,
                "low_water_s": 0.200,
                "max_queue_s": 0.750,
                "reset_timeout_s": 0.100,
                "cancel_deadline_s": 0.500,
            },
        )
        self._state = "idle"
        self._first_read_delay_s = 0.004
        self._first_action_anchor_s: float | None = None
        self._log_cursor = 0
        self._probe_published_at: float | None = None
        self._probe_expected: deque[str] = deque()
        self._probe_complete = threading.Event()
        self._playback_observation_started = False
        self._observation_cancelled = False
        self._expected_commands: deque[_ExpectedCommand] = deque()
        self._published_commands = 0
        self._observed_commands = 0
        self._published_action_tokens: set[int] = set()
        self._observed_action_tokens: set[int] = set()
        self._device_clock_offset_s: float | None = None
        self._clock_uncertainty_ms: float | None = None
        self._clock_basis = "unavailable"
        self._last_device_start_ms: float | None = None
        self._last_device_end_ms: float | None = None
        self._observation_error: str | None = None
        self._observation_complete = threading.Event()
        self._calibration_chunks = 0
        self._calibration_correction_ms = 0.0
        self._last_observed_offsets = dict(self._frozen_offsets)
        self._final_window_end_s: float | None = None
        self._game_terminal_reason: str | None = None
        self._cancelled_pending_commands = 0
        self._cancelled_pending_actions = 0
        self._release_latency_ms: float | None = None
        self._release_confirmed: bool | None = None
        self._release_error: str | None = None
        self._cleanup_warning: str | None = None
        self._publish_error: str | None = None
        self._device_thread: threading.Thread | None = None
        self._device_error: str | None = None
        self._publisher_thread: threading.Thread | None = None
        self._publisher_error: str | None = None
        self._publisher_poll_s = max(0.005, float(publisher_poll_ms) / 1000.0)
        self._owner_commands: queue.Queue[
            tuple[str, tuple[object, ...], queue.Queue[object]]
        ] = queue.Queue()
        self._session_state = "idle"
        self._session_report: dict[str, object] = {}
        self._session_terminal = threading.Event()
        self._final_chunk_published = False

    @property
    def active(self) -> bool:
        return self._state == "running"

    @property
    def takeover(self) -> bool:
        """后端一旦被选择就独占，触发失败也不得切回 Legacy。"""
        return True

    @property
    def finished(self) -> bool:
        return self._state in {"finished", "cancelled", "failed"}

    @property
    def first_action_anchor_s(self) -> float | None:
        return self._first_action_anchor_s

    @property
    def first_read_delay_ms(self) -> float:
        return self._first_read_delay_s * 1000.0

    @property
    def observed_offsets(self) -> dict[str, float]:
        """返回最近一个完整已执行切片的设备延迟观测。"""
        if int(getattr(self._calibrator, "event_count", 0)) <= 0:
            return dict(self._last_observed_offsets)
        return self._offsets_to_dict(self._calibrator.offsets)

    @staticmethod
    def _offsets_to_dict(offsets: Any) -> dict[str, float]:
        return {
            "down_ms": float(getattr(offsets, "down_ms", 0.0)),
            "up_ms": float(getattr(offsets, "up_ms", 0.0)),
            "move_ms": float(getattr(offsets, "move_ms", 0.0)),
            "wait_ms": float(getattr(offsets, "wait_ms", 0.0)),
            "interval_ms": float(getattr(offsets, "interval_ms", 0.0)),
        }

    def _execution_receipts(self) -> list[dict[str, object]]:
        value = (
            self._receipt_reader()
            if callable(self._receipt_reader)
            else self._receipt_reader
        )
        return [dict(item) for item in list(value)]

    def _fail_observation(self, reason: str) -> None:
        self._observation_error = reason
        raise RuntimeError(f"Native jlog 执行证据无效：{reason}")

    def _complete_observed_chunk(self, expected: _ExpectedCommand) -> None:
        """只用完整执行完的切片校准尚未编译的未来切片。"""
        if expected.used_offsets is None:
            self._fail_observation(
                f"chunk={expected.chunk_sequence} 缺少编译时 offset 快照"
            )
        if int(getattr(self._calibrator, "event_count", 0)) <= 0:
            self._fail_observation(
                f"chunk={expected.chunk_sequence} 没有可校准的命令"
            )
        correction_ms = float(
            self._calibrator.correction_ms(expected.used_offsets)
        )
        offsets = self._calibrator.offsets
        sample_counts_value = getattr(
            self._calibrator, "sample_counts", None
        )
        if callable(sample_counts_value):
            sample_counts_value = sample_counts_value()
        sample_counts = dict(sample_counts_value or {})
        for sample_key, offset_field in {
            "down": "down_ms",
            "up": "up_ms",
            "move": "move_ms",
            "wait": "wait_ms",
            "interval": "interval_ms",
        }.items():
            if int(sample_counts.get(sample_key, 1)) <= 0:
                setattr(
                    offsets,
                    offset_field,
                    float(getattr(expected.used_offsets, offset_field)),
                )
        self._compiler.add_residual_ms(correction_ms)
        self._compiler.set_offsets(offsets)
        self._last_observed_offsets = self._offsets_to_dict(offsets)
        self._calibration_correction_ms += correction_ms
        self._calibration_chunks += 1
        self._calibrator.reset()
        reset_session = getattr(self._session, "reset_calibration", None)
        if reset_session is not None:
            reset_session()

    def _observe_new_logs(self) -> None:
        try:
            timestamped_reader = getattr(
                self._device, "log_records_since", None
            )
            if timestamped_reader is not None:
                cursor, records = timestamped_reader(self._log_cursor)
            else:
                cursor, lines = self._device.logs_since(self._log_cursor)
                records = [(line, self._clock()) for line in lines]
        except Exception as exc:  # noqa: BLE001 - 游标丢失必须终止 Native
            self._observation_error = f"读取 jlog 失败：{type(exc).__name__}: {exc}"
            raise
        self._log_cursor = cursor
        for line, received_s in records:
            if not line.startswith("jlog "):
                continue
            event = native_engine.parse_minitouch_log(line)
            if event is None:
                if self._playback_observation_started:
                    self._fail_observation("无法解析设备返回的 jlog 行")
                continue
            start_ms = float(event["start_ms"])
            end_ms = float(event["end_ms"])
            cost_ms = float(event["cost_ms"])
            if not all(math.isfinite(value) for value in (start_ms, end_ms, cost_ms)):
                self._fail_observation("jlog 含有非有限时间值")
            if end_ms < start_ms or cost_ms < 0.0:
                self._fail_observation(
                    "jlog 时间范围无效："
                    f"start={start_ms} end={end_ms} cost={cost_ms}"
                )
            if (
                self._last_device_start_ms is not None
                and start_ms < self._last_device_start_ms
            ):
                self._fail_observation(
                    "设备 jlog 时钟倒退："
                    f"previous={self._last_device_start_ms} current={start_ms}"
                )
            if (
                self._last_device_end_ms is not None
                and start_ms + 1e-6 < self._last_device_end_ms
            ):
                self._fail_observation(
                    "设备 jlog 命令发生重叠："
                    f"previous_end={self._last_device_end_ms} "
                    f"current_start={start_ms}"
                )
            self._last_device_start_ms = start_ms
            self._last_device_end_ms = end_ms
            command = str(event["command"]).strip()
            if not command:
                self._fail_observation("jlog 命令为空")
            if not self._playback_observation_started:
                if not self._probe_expected:
                    continue
                expected_probe = self._probe_expected[0]
                if command != expected_probe:
                    self._fail_observation(
                        "启动探测命令失配："
                        f"expected={expected_probe!r} actual={command!r}"
                    )
                self._probe_expected.popleft()
                if command.startswith("w "):
                    nominal = float(command.split()[1])
                    if nominal <= 1 and self._probe_published_at is not None:
                        # 以日志接收线程的时间戳截断 owner 轮询抖动；两端
                        # 单调时钟差按 NTP midpoint 近似，并记录误差上界。
                        round_trip = max(
                            0.0,
                            float(received_s) - self._probe_published_at,
                        )
                        midpoint_s = self._probe_published_at + round_trip / 2
                        device_start_s = start_ms / 1000.0
                        self._device_clock_offset_s = (
                            midpoint_s - device_start_s
                        )
                        self._clock_uncertainty_ms = round_trip * 500.0
                        self._clock_basis = "probe-midpoint"
                        estimate = min(0.015, max(0.001, round_trip / 2))
                        self._first_read_delay_s = estimate
                        self._probe_published_at = None
                if not self._probe_expected:
                    self._probe_complete.set()
                continue
            if self._observation_cancelled:
                continue
            if not self._expected_commands:
                self._fail_observation(
                    f"收到未发布的设备命令 actual={command!r}"
                )
            expected = self._expected_commands[0]
            if command != expected.command:
                self._fail_observation(
                    f"chunk={expected.chunk_sequence} 命令失配："
                    f"expected={expected.command!r} actual={command!r}"
                )
            self._expected_commands.popleft()
            self._observed_commands += 1
            self._calibrator.observe(event)
            observe = getattr(self._session, "observe_minitouch_log", None)
            if observe is not None:
                observe(event)
            if expected.receipts:
                # d/m/u 只是排入当前事务；其后的 c 完成 input_sync 后触控
                # 才对系统可见，因此用 commit 的 end 作为保守执行时刻。
                device_commit_s = end_ms / 1000.0
                if self._device_clock_offset_s is None:
                    if self._require_probe:
                        self._fail_observation("正式动作开始前缺少探测时钟映射")
                    first_planned_s = float(
                        expected.receipts[0]["planned_engine_s"]
                    )
                    self._device_clock_offset_s = (
                        first_planned_s - device_commit_s
                    )
                    self._clock_basis = "first-action-relative"
                actual_s = device_commit_s + self._device_clock_offset_s
                for receipt in expected.receipts:
                    token = int(receipt["action_token"])
                    if token in self._observed_action_tokens:
                        self._fail_observation(f"重复动作回执 token={token}")
                    planned_s = float(receipt["planned_engine_s"])
                    observed = self._session.observe_execution(
                        planned_s, actual_s, 1
                    )
                    if not bool(observed):
                        self._fail_observation(
                            f"PlaybackSession 拒绝动作回执 token={token}"
                        )
                    self._observed_action_tokens.add(token)
            if expected.last_in_chunk:
                self._complete_observed_chunk(expected)
        if self._playback_observation_started:
            self._session_report = dict(self._session.report())
            if self._final_chunk_published and not self._expected_commands:
                self._observation_complete.set()

    def _cancel_device_start(self, timeout_s: float) -> bool:
        """线性化取消与 probe 提交；无法取得锁时先封闭设备发布。"""
        budget = max(0.0, float(timeout_s))
        if self._device_start_commit_lock.acquire(timeout=budget):
            try:
                self._device_start_cancel.set()
            finally:
                self._device_start_commit_lock.release()
            return True

        # worker 可能已经通过锁内检查但尚未真正 publish。先把生产设备置为
        # closed，再提交取消标志；这样迟到调用会在设备边界被拒绝。
        self._emergency_stop_device_with_budget(min(0.02, budget))
        self._device_start_cancel.set()
        return False

    def _emergency_stop_device_with_budget(self, timeout_s: float) -> bool:
        """串行执行本地紧急断开，避免多个失败路径并发 close/kill。"""
        budget = max(0.0, float(timeout_s))
        if budget <= 0 or not self._device_cleanup_lock.acquire(timeout=budget):
            return False
        started = time.monotonic()
        try:
            bounded_stop = getattr(
                self._device, "emergency_stop_with_deadline", None
            )
            if callable(bounded_stop):
                result = bounded_stop(
                    max(0.0, budget - (time.monotonic() - started))
                )
            else:
                result = self._device.emergency_stop()
            return result is True
        except Exception:  # noqa: BLE001 - 调用方继续走设备端 kill
            return False
        finally:
            self._device_cleanup_lock.release()

    def _stop_device_with_budget(self, timeout_s: float) -> bool:
        """串行执行设备清理；没有显式 True 就不能视为释放成功。"""
        budget = max(0.0, float(timeout_s))
        if budget <= 0 or not self._device_cleanup_lock.acquire(timeout=budget):
            return False
        started = time.monotonic()
        try:
            bounded_stop = getattr(self._device, "stop_with_deadline", None)
            if callable(bounded_stop):
                result = bounded_stop(
                    max(0.0, budget - (time.monotonic() - started))
                )
            else:
                result = self._device.stop()
            return result is True
        except Exception:  # noqa: BLE001 - 调用方统一记录 fail-closed 证据
            return False
        finally:
            self._device_cleanup_lock.release()

    def _start_device(self) -> None:
        try:
            self._device.start(cancel_event=self._device_start_cancel)
            # 与 stop 的取消提交共用一把锁：锁内检查通过后可能先完成一次
            # probe，但 stop 必须随后取得锁并清理；反过来则绝不允许迟到发布。
            with self._device_start_commit_lock:
                if self._device_start_cancel.is_set():
                    raise RuntimeError("minitouch 准备已取消")
                # 首读延迟探测：发一条 w 0，回读 jlog 估计 publish->设备读取的
                # 单程延迟，用它对齐整曲脚本的绝对起点。
                self._probe_expected = deque(("c", "w 0", "c"))
                self._probe_complete.clear()
                self._probe_published_at = self._clock()
                self._device.publish("c\nw 0\nc\n")
            if self._device_start_cancel.is_set():
                raise RuntimeError("minitouch probe 完成时准备已取消")
            print(
                "NativeMinitouch device_ready "
                f"max={self._device.max_x}x{self._device.max_y} "
                f"contacts={self._device.max_contacts}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 - 后续由主循环 fail-closed
            cancelled = self._device_start_cancel.is_set()
            cleanup_ok = self._stop_device_with_budget(0.5)
            if not cancelled:
                self._device_error = f"{type(exc).__name__}: {exc}"
            print(
                "NativeMinitouch device_start_cancelled "
                if cancelled
                else "NativeMinitouch device_start_failed ",
                f"reason={type(exc).__name__}: {exc} "
                f"cleanup_confirmed={str(cleanup_ok).lower()}",
                flush=True,
            )

    def arm(self) -> None:
        """在 photogate 前异步准备设备和 C++ 会话。"""
        if self._state in {"arming", "armed", "ready"}:
            if self._publisher_error is not None:
                raise RuntimeError(self._publisher_error)
            return
        if self._state != "idle":
            raise RuntimeError(f"Native 会话无法从 {self._state} 状态 arm")
        self._publisher_thread = threading.Thread(
            target=self._publisher_loop,
            name="native-playback-owner",
            daemon=True,
        )
        self._publisher_thread.start()
        if not bool(
            self._submit_session("arm", self._actions, dict(self._config))
        ):
            raise RuntimeError("C++ PlaybackSession arm 失败")
        self._state = "arming"
        self._device_start_cancel.clear()
        self._device_thread = threading.Thread(
            target=self._start_device,
            name="native-minitouch-start",
            daemon=True,
        )
        self._device_thread.start()

    def wait_until_ready(self, timeout_s: float) -> bool:
        """等待设备握手完成，但不观察 photogate，也不启动演奏。"""
        if timeout_s <= 0:
            raise ValueError("timeout_s 必须大于 0")
        if self._state not in {"arming", "armed", "ready"}:
            raise RuntimeError("必须先 arm Native 会话")
        device_thread = self._device_thread
        if device_thread is None:
            raise RuntimeError("minitouch 准备线程未启动")
        deadline = time.monotonic() + float(timeout_s)
        device_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if device_thread.is_alive():
            self._state = "failed"
            self._cancel_device_start(0.05)
            self._emergency_stop_device_with_budget(0.05)
            raise RuntimeError(
                f"minitouch 在 {float(timeout_s):g} 秒内未 ready"
            )
        if self._device_error is not None:
            self._state = "failed"
            raise RuntimeError(f"minitouch 准备失败：{self._device_error}")
        if not self._device.connected:
            self._state = "failed"
            raise RuntimeError("minitouch 准备结束但未建立连接")
        if self._require_probe and not self._probe_complete.wait(
            timeout=max(0.0, deadline - time.monotonic())
        ):
            self._state = "failed"
            self._cancel_device_start(0.05)
            self._emergency_stop_device_with_budget(0.05)
            raise RuntimeError(
                f"minitouch 启动探测在 {float(timeout_s):g} 秒内未完成"
            )
        if self._state == "arming":
            self._state = "armed"
        return True

    def configure_timing_offset(self, offset_ms: int) -> None:
        """只在首拍启动前替换本局冻结的 Profile 时延。"""
        if self._state not in {"idle", "arming", "armed", "ready"}:
            raise RuntimeError("Native timing offset 只允许在启动前设置")
        value = int(offset_ms)
        self._frozen_timing_offset_ms = value
        self._config["press_bias_ms"] = value

    def observe_start_frame(self, image: Any, now: float) -> float | None:
        """只观察首拍门控；触发时设备未就绪则立即失败。"""
        anchor = self._photogate.observe(image, now)
        if anchor is None:
            return None
        if self._device_error is not None:
            self._state = "failed"
            raise RuntimeError(f"minitouch 准备失败：{self._device_error}")
        if (
            self._device_thread is None
            or self._device_thread.is_alive()
            or not self._device.connected
        ):
            self._state = "failed"
            self._cancel_device_start(0.05)
            self._emergency_stop_device_with_budget(0.05)
            raise RuntimeError("首拍已到达，但 minitouch 尚未 ready")
        self._state = "ready"
        # 沿用 Profile 既有语义：正值表示提前输入。该值只在
        # 首拍映射时折入绝对锚点，本局内不再学习或改写。
        return (
            float(anchor)
            - self._frozen_timing_offset_ms / 1000.0
            - self._first_read_delay_s
        )

    def _publish_chunk(self, chunk: dict[str, object]) -> bool:
        """把 C++ 会话给出的绝对时刻切片编译后原样追加到设备队列。"""
        try:
            actions: list[dict[str, object]] = []
            for item in list(chunk.get("actions", [])):
                entry = dict(item)
                action = dict(entry["action"])
                action["due_s"] = float(entry["engine_due_s"])
                actions.append(action)
            future_down_reservations: list[dict[str, object]] = []
            for item in list(chunk.get("future_down_reservations", [])):
                entry = dict(item)
                action = dict(entry["action"])
                action["due_s"] = float(entry["engine_due_s"])
                future_down_reservations.append(action)
            used_offsets = self._compiler.offsets
            script = list(self._compiler.compile(
                actions,
                dict(self._config, song_offset_s=0.0, press_bias_ms=0),
                float(chunk["window_start_s"]),
                bool(chunk.get("final_chunk", False)),
                float(chunk["window_end_s"]),
                future_down_reservations,
            ))
            commands = [str(line).strip() for line in script]
            if not commands or any(not command for command in commands):
                raise RuntimeError("TouchScriptCompiler 生成了空命令行")
            receipt_by_commit: dict[int, list[dict[str, object]]] = {}
            receipt_source_lines: set[int] = set()
            new_tokens: set[int] = set()
            for receipt in self._execution_receipts():
                line_index = int(receipt["line_index"])
                token = int(receipt["action_token"])
                if line_index < 0 or line_index >= len(commands):
                    raise RuntimeError(
                        f"动作回执行号越界：line={line_index} "
                        f"size={len(commands)}"
                    )
                if line_index in receipt_source_lines:
                    raise RuntimeError(f"动作回执行号重复：line={line_index}")
                if token in self._published_action_tokens or token in new_tokens:
                    raise RuntimeError(f"动作回执 token 重复：token={token}")
                receipt_command = str(receipt["command"])
                if commands[line_index].split(" ", 1)[0] != receipt_command:
                    raise RuntimeError(
                        "动作回执命令不匹配："
                        f"line={line_index} receipt={receipt_command!r} "
                        f"script={commands[line_index]!r}"
                    )
                commit_index: int | None = None
                for candidate in range(line_index + 1, len(commands)):
                    candidate_kind = commands[candidate].split(" ", 1)[0]
                    if candidate_kind == "c":
                        commit_index = candidate
                        break
                    if candidate_kind == "w":
                        break
                if commit_index is None:
                    raise RuntimeError(
                        f"动作回执后缺少同相位 commit：line={line_index}"
                    )
                receipt_source_lines.add(line_index)
                receipt_by_commit.setdefault(commit_index, []).append(receipt)
                new_tokens.add(token)
            sequence = int(chunk["sequence"])
            records = [
                _ExpectedCommand(
                    command=command,
                    chunk_sequence=sequence,
                    receipts=tuple(receipt_by_commit.get(index, ())),
                    last_in_chunk=index == len(commands) - 1,
                    used_offsets=(
                        used_offsets if index == len(commands) - 1 else None
                    ),
                )
                for index, command in enumerate(commands)
            ]
            final_chunk = bool(chunk.get("final_chunk", False))
            if (
                final_chunk
                and len(self._published_action_tokens) + len(new_tokens)
                != len(self._actions)
            ):
                raise RuntimeError(
                    "最终切片的动作回执不完整："
                    f"receipts="
                    f"{len(self._published_action_tokens) + len(new_tokens)} "
                    f"planned={len(self._actions)}"
                )
            self._device.publish("".join(script))
            self._expected_commands.extend(records)
            self._published_commands += len(records)
            self._published_action_tokens.update(new_tokens)
            if final_chunk:
                self._final_chunk_published = True
                self._final_window_end_s = float(chunk["window_end_s"])
            return True
        except Exception as exc:  # noqa: BLE001 - worker 将错误传播给主循环
            self._publish_error = f"{type(exc).__name__}: {exc}"
            return False

    @staticmethod
    def _state_name(state: Any) -> str:
        value = getattr(state, "value", state)
        return str(value).rsplit(".", 1)[-1].lower()

    def _refresh_session_snapshot(self, state: Any | None = None) -> str:
        """只能由 owner 线程调用，统一发布会话快照。"""
        if state is None:
            state = self._session.poll()
        self._session_state = self._state_name(state)
        self._session_report = dict(self._session.report())
        if self._session_state in {"finished", "cancelled", "failed"}:
            self._session_terminal.set()
        return self._session_state

    def _finish_when_fully_published(self) -> bool:
        """最后一块已由设备完整回读后才标记 finish。"""
        report = dict(self._session.report())
        planned = int(report.get("planned", 0))
        sent = int(report.get("sent", 0))
        if (
            planned <= 0
            or sent < planned
            or not self._final_chunk_published
        ):
            self._session_report = report
            return False
        if self._expected_commands:
            self._session_report = report
            return False
        executed = int(report.get("executed", 0))
        if executed != planned:
            self._fail_observation(
                f"动作执行回执不完整：executed={executed} planned={planned}"
            )
        finish = getattr(self._session, "finish", None)
        if finish is None or not bool(finish("all-actions-executed")):
            self._refresh_session_snapshot()
            return False
        self._refresh_session_snapshot("finished")
        return True

    def _handle_owner_command(
        self, operation: str, arguments: tuple[object, ...]
    ) -> object:
        if operation == "arm":
            self._final_chunk_published = False
            result = bool(self._session.arm(*arguments))
            self._refresh_session_snapshot()
            return result
        if operation == "start":
            # 先吸收全部探测日志，再由 start 重置 C++ 校准窗口，
            # 避免迟到的 w 0 被计入正式演奏。
            self._observe_new_logs()
            if self._require_probe and not self._probe_complete.is_set():
                raise RuntimeError("minitouch 启动探测尚未完整回读")
            self._probe_expected.clear()
            self._playback_observation_started = True
            self._observation_cancelled = False
            self._observation_complete.clear()
            self._calibrator = native_engine.latency_calibrator()
            if not bool(self._session.start(*arguments)):
                self._refresh_session_snapshot()
                return False
            published = bool(self._session.publish())
            state = self._refresh_session_snapshot()
            if not published:
                if state == "failed":
                    return False
                raise RuntimeError("首个 Native 切片未发布")
            self._finish_when_fully_published()
            return True
        if operation == "cancel":
            # reset 后的 r/jlog 不属于谱面命令；取消只保留“不完整”证据，
            # 不把预期中断误报为命令失配。
            self._observation_cancelled = True
            self._cancelled_pending_commands = len(self._expected_commands)
            self._cancelled_pending_actions = len(
                self._published_action_tokens - self._observed_action_tokens
            )
            self._expected_commands.clear()
            self._observation_complete.clear()
            result = bool(self._session.cancel(*arguments))
            self._refresh_session_snapshot()
            return result
        raise RuntimeError(f"未知 PlaybackSession 操作：{operation}")

    def _submit_session(
        self,
        operation: str,
        *arguments: object,
        timeout_s: float = 2.0,
    ) -> object:
        """向唯一 owner 提交会话操作，主线程不直接触碰 C++ 对象。"""
        owner = self._publisher_thread
        if owner is None or not owner.is_alive():
            raise RuntimeError("PlaybackSession owner 线程未运行")
        response: queue.Queue[object] = queue.Queue(maxsize=1)
        self._owner_commands.put((operation, arguments, response))
        try:
            outcome = response.get(timeout=timeout_s)
        except queue.Empty as exc:
            raise RuntimeError(
                f"PlaybackSession {operation} 操作超时"
            ) from exc
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def _drive_session(self) -> None:
        """由 owner 线程高频补充切片并推进取消超时。"""
        self._observe_new_logs()
        if (
            self._final_chunk_published
            and self._expected_commands
            and self._final_window_end_s is not None
            and self._clock() > self._final_window_end_s + 1.0
        ):
            self._fail_observation(
                "最终设备命令回读超时："
                f"pending={len(self._expected_commands)}"
            )
        if self._session_state == "running":
            self._session.publish()
            if self._finish_when_fully_published():
                return
        state = self._refresh_session_snapshot()
        if state == "failed":
            reason = str(
                self._session_report.get("reason")
                or "C++ PlaybackSession failed"
            )
            raise RuntimeError(reason)

    def start(self, anchor_s: float) -> None:
        """把谱面第一动作映射到 photogate 给出的绝对单调时刻。"""
        if self._state != "ready" or not self._device.connected:
            raise RuntimeError("Native 会话未 ready，拒绝启动")
        self._first_action_anchor_s = float(anchor_s)
        if not bool(self._submit_session("start", self._first_action_anchor_s)):
            self._state = "failed"
            raise RuntimeError("C++ PlaybackSession start 失败")
        self._state = (
            "finished" if self._session_state == "finished" else "running"
        )
        print(
            "NativeMinitouch started "
            f"run_id={self._run_id} anchor_s={self._first_action_anchor_s:.6f} "
            f"planned={len(self._actions)} first_read_delay_ms="
            f"{self.first_read_delay_ms:.3f} "
            f"photogate={self._photogate.report()}",
            flush=True,
        )

    def _publisher_loop(self) -> None:
        """过渡期高频 owner；5Hz 页面监控不接触会话或队列。"""
        try:
            while True:
                command = None
                try:
                    command = self._owner_commands.get(
                        timeout=self._publisher_poll_s
                    )
                except queue.Empty:
                    pass
                if command is not None:
                    operation, arguments, response = command
                    if operation == "shutdown":
                        response.put(True)
                        return
                    try:
                        response.put(
                            self._handle_owner_command(operation, arguments)
                        )
                    except BaseException as exc:  # noqa: BLE001
                        response.put(exc)
                if self._session_state in {
                    "armed", "running", "cancelling"
                }:
                    self._drive_session()
        except Exception as exc:  # noqa: BLE001 - poll() 会在主线程重抛
            self._publisher_error = f"{type(exc).__name__}: {exc}"
            self._state = "failed"
            self._session_state = "failed"
            try:
                self._session_report = dict(self._session.report())
            except Exception:  # noqa: BLE001 - 保留原始 worker 错误
                pass
            self._session_terminal.set()
            try:
                self._device.request_reset()
            finally:
                self._emergency_stop_device_with_budget(0.08)

    def poll(self, now: float) -> None:
        """5Hz 主循环只读取状态；滚动发布由独立 worker 驱动。"""
        del now
        if self._publisher_error is not None:
            raise RuntimeError(self._publisher_error)
        if self._device_error is not None:
            raise RuntimeError(self._device_error)
        if self._session_state == "failed":
            raise RuntimeError(
                str(
                    self._session_report.get("reason")
                    or "C++ PlaybackSession failed"
                )
            )
        if self._session_state in {"finished", "cancelled"}:
            self._state = self._session_state

    def set_terminal_reason(self, reason: str) -> None:
        """保存游戏层终态，避免与 Native 传输终态混成一个原因。"""
        self._game_terminal_reason = str(reason) if reason else None

    def stop(self) -> None:
        """在 500ms 硬预算内取消生产并取得设备端释放证据。"""
        stop_started = time.monotonic()
        release_deadline = stop_started + 0.500
        release_errors: list[str] = []

        def remaining() -> float:
            return max(0.0, release_deadline - time.monotonic())

        # 先封闭启动提交窗口。即使准备线程卡在 ADB，恢复后也只能自清理，
        # 不能在本方法返回后重新连接或发布 probe。
        start_cancel_synchronized = self._cancel_device_start(
            min(0.02, remaining())
        )
        if not start_cancel_synchronized:
            release_errors.append("minitouch 启动提交未在释放预算内同步")
        owner = self._publisher_thread
        if (
            owner is not None
            and owner.is_alive()
            and self._final_chunk_published
            and self._expected_commands
        ):
            # 正常结算时给异步日志线程一个很短的排空窗口；超过窗口仍按
            # 取消路径释放，整条 stop 路径继续受 500ms 门槛约束。
            self._observation_complete.wait(timeout=min(0.10, remaining()))
        if (
            owner is not None
            and owner.is_alive()
            and self._session_state not in {"finished", "cancelled", "failed"}
        ):
            try:
                timeout_s = min(0.08, remaining())
                if timeout_s <= 0:
                    raise RuntimeError("stop 释放预算在 cancel 前已耗尽")
                self._submit_session(
                    "cancel", "engine-stop", timeout_s=timeout_s
                )
            except Exception as exc:  # noqa: BLE001 - 仍需继续释放设备
                self._publish_error = f"cancel {type(exc).__name__}: {exc}"
            # minitouch 没有 reset ACK；只给 C++ 状态机约 100ms 走到
            # fallback，之后立即转入设备端 PID kill 证据路径。
            self._session_terminal.wait(timeout=min(0.12, remaining()))

        # 先请求 owner 停产，不在这里长等；设备断开后任何迟到 publish 都会失败。
        if owner is not None and owner.is_alive():
            response: queue.Queue[object] = queue.Queue(maxsize=1)
            self._owner_commands.put(("shutdown", (), response))
            try:
                timeout_s = min(0.02, remaining())
                if timeout_s > 0:
                    response.get(timeout=timeout_s)
            except queue.Empty:
                pass

        device_thread = self._device_thread
        if device_thread is not None and device_thread.is_alive():
            # close/kill 本地句柄可打断握手读取；线程退出后仍由下方设备
            # stop 对设备端 PID 做独立确认。
            self._emergency_stop_device_with_budget(min(0.08, remaining()))
            device_thread.join(timeout=min(0.08, remaining()))

        device_release_ok = self._stop_device_with_budget(remaining())
        device_cleanup_detail = str(
            getattr(self._device, "last_release_error", None) or ""
        ) or None
        if not device_release_ok:
            release_errors.append(
                device_cleanup_detail or "设备端 minitouch 释放未确认"
            )
        elif device_cleanup_detail is not None:
            # forward 泄漏不推翻触点释放证据，但必须进入报告供下一局诊断。
            self._cleanup_warning = device_cleanup_detail
        if device_thread is not None and device_thread.is_alive():
            device_thread.join(timeout=min(0.05, remaining()))
        if device_thread is not None and device_thread.is_alive():
            release_errors.append("minitouch 准备线程未在释放预算内退出")

        self._release_latency_ms = (time.monotonic() - stop_started) * 1000.0
        if self._release_latency_ms > 500.0:
            release_errors.append(
                f"释放耗时 {self._release_latency_ms:.3f}ms 超过 500ms"
            )
        self._release_confirmed = bool(
            device_release_ok
            and start_cancel_synchronized
            and not (device_thread is not None and device_thread.is_alive())
            and self._release_latency_ms <= 500.0
        )
        self._release_error = "; ".join(release_errors) or None

        if owner is not None and owner.is_alive():
            owner.join(timeout=remaining())
        if owner is not None and owner.is_alive():
            self._publish_error = (
                self._publish_error
                or "publisher owner 未在 500ms 停止预算内退出"
            )

        fatal_error = bool(
            self._publisher_error
            or self._publish_error
            or self._device_error
            or self._observation_error
            or not self._release_confirmed
            or self._session_state == "failed"
        )
        if self._session_state == "finished" and not fatal_error:
            self._state = "finished"
        elif fatal_error:
            self._state = "failed"
        elif self._state != "failed":
            self._state = "cancelled"
        report = self.report()
        print(
            "NativeMinitouch session_stats "
            f"run_id={self._run_id} state={self._state} "
            f"planned={report['planned']} sent={report['sent']} "
            f"executed={report['executed']} chunks={report['chunks']} "
            f"executed_observed={str(bool(report['executed_observation_complete'])).lower()} "
            f"underflows={report['underflows']} "
            f"release_confirmed={str(bool(report['release_confirmed'])).lower()} "
            f"publish_error={self._publish_error or self._publisher_error}",
            flush=True,
        )

    def report(self) -> dict[str, object]:
        """返回可直接写入结果 JSON 的统一 Native 会话统计。"""
        # PlaybackSession 是单 owner 对象；这里只读 worker 发布的快照。
        report = dict(self._session_report)
        photogate = getattr(self, "_photogate", None)
        photogate_report = (
            photogate.report() if photogate is not None else {}
        )
        for source, target in {
            "planned_actions": "planned",
            "sent_actions": "sent",
            "executed_actions": "executed",
            "queue_underflows": "underflows",
            "terminal_reason": "reason",
        }.items():
            if target not in report and source in report:
                report[target] = report[source]
        defaults: dict[str, object] = {
            "planned": len(self._actions),
            "sent": 0,
            "executed": 0,
            "chunks": 0,
            "underflows": 0,
            "drift_p50_ms": None,
            "drift_p95_ms": None,
            "drift_max_ms": None,
            "stop_latency_ms": 0.0,
            "reason": self._publisher_error or self._publish_error or self._state,
        }
        defaults.update(report)
        transport_reason = str(defaults.get("reason") or "") or None
        failure_reason = (
            self._publisher_error
            or self._publish_error
            or self._device_error
            or self._observation_error
            or self._release_error
        )
        if failure_reason is not None:
            defaults["reason"] = failure_reason
        elif transport_reason is None:
            defaults["reason"] = self._state
        defaults["transport_reason"] = transport_reason
        if self._release_latency_ms is not None:
            defaults["stop_latency_ms"] = self._release_latency_ms
        if int(defaults.get("executed", 0)) <= 0:
            defaults["drift_p50_ms"] = None
            defaults["drift_p95_ms"] = None
            defaults["drift_max_ms"] = None
        if "action_counts" not in defaults:
            defaults["action_counts"] = {
                "tap": int(defaults.get("tap_actions", 0)),
                "flick": int(defaults.get("flick_actions", 0)),
                "down": int(defaults.get("hold_starts", 0)),
                "move": int(defaults.get("hold_moves", 0)),
                "up": int(defaults.get("hold_releases", 0)),
            }
        observation_complete = bool(
            self._state == "finished"
            and self._session_state == "finished"
            and self._final_chunk_published
            and not self._expected_commands
            and not self._observation_cancelled
            and self._observation_error is None
            and int(defaults.get("executed", 0))
            == int(defaults.get("planned", len(self._actions)))
        )
        if observation_complete:
            observation_reason = None
        elif self._observation_error is not None:
            observation_reason = self._observation_error
        elif self._observation_cancelled:
            observation_reason = "会话在完整设备回读前取消"
        elif self._state != "finished" or self._session_state != "finished":
            observation_reason = (
                "会话终态不是 finished："
                f"backend={self._state} session={self._session_state}"
            )
        elif not self._playback_observation_started:
            observation_reason = "会话尚未启动"
        else:
            observation_reason = (
                "等待设备命令回读："
                f"pending={len(self._expected_commands)}"
            )
        absolute_drift_valid = bool(
            observation_complete
            and int(defaults.get("executed", 0)) > 0
            and defaults.get("drift_p95_ms") is not None
            and defaults.get("drift_max_ms") is not None
            and self._clock_basis == "probe-midpoint"
            and self._clock_uncertainty_ms is not None
            and self._clock_uncertainty_ms <= 1.0
        )
        drift_p95 = defaults.get("drift_p95_ms")
        drift_max = defaults.get("drift_max_ms")
        conservative_p95 = (
            float(drift_p95) + float(self._clock_uncertainty_ms)
            if absolute_drift_valid and drift_p95 is not None
            else None
        )
        conservative_max = (
            float(drift_max) + float(self._clock_uncertainty_ms)
            if absolute_drift_valid and drift_max is not None
            else None
        )
        timing_gate_passed = bool(
            conservative_p95 is not None
            and conservative_p95 <= 3.0
            and conservative_max is not None
            and conservative_max <= 8.0
            and int(defaults.get("underflows", 0)) == 0
        )
        defaults.update({
            "run_id": self._run_id,
            "state": self._state,
            "session_state": self._session_state,
            "first_action_anchor_s": self._first_action_anchor_s,
            "touch_y": float(
                getattr(self, "_config", {}).get("judgement_y", TOUCH_Y)
            ),
            "jlog_path": str(self._jlog_path) if self._jlog_path is not None else None,
            "frozen_offsets": dict(self._frozen_offsets),
            "frozen_timing_offset_ms": self._frozen_timing_offset_ms,
            "executed_observation_supported": True,
            "executed_observation_complete": observation_complete,
            "executed_observation_reason": observation_reason,
            "expected_commands_pending": len(self._expected_commands),
            "published_commands": self._published_commands,
            "observed_commands": self._observed_commands,
            "calibration_chunks": self._calibration_chunks,
            "executed_chunks": self._calibration_chunks,
            "calibration_correction_ms": self._calibration_correction_ms,
            "clock_offset_ms": (
                self._device_clock_offset_s * 1000.0
                if self._device_clock_offset_s is not None
                else None
            ),
            "clock_basis": self._clock_basis,
            "clock_uncertainty_ms": self._clock_uncertainty_ms,
            "execution_timestamp": "commit-end",
            "drift_scope": (
                "absolute-host-estimate"
                if self._clock_basis == "probe-midpoint"
                else "relative-device-timeline"
            ),
            "absolute_drift_valid": absolute_drift_valid,
            "conservative_drift_p95_ms": conservative_p95,
            "conservative_drift_max_ms": conservative_max,
            "timing_gate_passed": timing_gate_passed,
            "device_offsets": dict(self._last_observed_offsets),
            "game_terminal_reason": self._game_terminal_reason,
            "cancelled_pending_commands": self._cancelled_pending_commands,
            "cancelled_pending_actions": self._cancelled_pending_actions,
            "release_confirmed": self._release_confirmed is True,
            "release_error": self._release_error,
            "cleanup_warning": getattr(self, "_cleanup_warning", None),
            "publish_error": self._publish_error,
            "publisher_error": self._publisher_error,
            "device_error": self._device_error,
            "observation_error": self._observation_error,
            **photogate_report,
        })
        return defaults
