from __future__ import annotations

import ctypes
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace

import numpy as np

from .controller_touch import ControllerTouchDispatcher
from .note_detector import NoteDetector
from .life_monitor import LifeDetector, LifeGuard, LifeStatus, PlayfieldCompletionGuard
from .live_failed_detector import LiveFailedPopupDetector
from .playfield_monitor import PlayfieldLifecycleMonitor
from .timing_feedback import AdaptiveTimingController, TimingFeedbackDetector
from .touch_planner import ActionKind, RealtimePlanner


_HOT_PATH_STAGES = (
    "capture",
    "touch_advance",
    "life",
    "playfield_monitor",
    "detector",
    "planner",
    "native_backend",
    "timing_feedback",
    "dispatch",
    "recorder_enqueue",
)
_STAGE_SAMPLE_CAPACITY = 120 * 600
_FRAME_INTERVAL_SAMPLE_CAPACITY = 120 * 600


class DebugRecorder:
    def record(
        self, image, timestamp, notes, actions, life_status,
        diagnostics=None, timing_state=None, life_value=None, touch_state=None,
    ): ...
    def close(self): ...


@dataclass(frozen=True)
class EngineStats:
    processed_frames: int
    dispatched_actions: int
    stopped: bool
    aborted_for_life: bool = False
    completed: bool = False
    life_depleted: bool = False
    life_failed: bool = False
    timing_feedback_fast: int = 0
    timing_feedback_slow: int = 0
    initial_timing_offset_ms: int = 0
    final_timing_offset_ms: int = 0
    timing_feedback_valid: int = 0
    timing_feedback_ignored: int = 0
    timing_feedback_ignored_reasons: dict[str, int] = field(default_factory=dict)
    timing_feedback_sightings: int = 0
    timing_feedback_reports: int = 0
    filtered_adjacent_artifacts: int = 0
    rejected_hold_candidates: int = 0
    terminal_reason: str = ""
    action_counts: dict[str, int] = field(default_factory=dict)
    frame_interval_p50_ms: float = 0.0
    frame_interval_p95_ms: float = 0.0
    frame_interval_max_ms: float = 0.0
    effective_fps: float = 0.0
    recovered_contacts: int = 0
    down_recoveries: int = 0
    stale_move_recoveries: int = 0
    touch_resets: int = 0
    input_wait_count: int = 0
    input_wait_total_ms: float = 0.0
    input_wait_max_ms: float = 0.0
    stage_timings_ms: dict[str, dict[str, object]] = field(default_factory=dict)
    frame_interval_outliers: tuple[dict[str, object], ...] = ()
    cleanup_failed: bool = False
    cleanup_errors: tuple[str, ...] = ()
    recorder_error: str | None = None
    startup_timed_out: bool = False
    engine_mode: str = "legacy"
    native_report: dict[str, object] = field(default_factory=dict)


class RealtimeEngine:
    """Own the realtime lifecycle and guarantee touch cleanup on every exit."""

    def __init__(
        self,
        detector: NoteDetector,
        planner: RealtimePlanner,
        touch: ControllerTouchDispatcher,
        clock: Callable[[], float] = time.perf_counter,
        life_detector: LifeDetector | None = None,
        life_guard: LifeGuard | None = None,
        completion_guard: PlayfieldCompletionGuard | None = None,
        debug_recorder: DebugRecorder | None = None,
        timing_feedback_detector: TimingFeedbackDetector | None = None,
        timing_controller: AdaptiveTimingController | None = None,
        native_backend: object | None = None,
        playfield_monitor: PlayfieldLifecycleMonitor | None = None,
        live_failed_detector: LiveFailedPopupDetector | None = None,
    ) -> None:
        self.detector = detector
        self.planner = planner
        self.touch = touch
        # 注意：默认时钟必须是 perf_counter（QPC）。本机 CPython 的
        # time.monotonic 是 GetTickCount64，分辨率只有 15.625ms，会把
        # 帧定速与谱面按压重新量化到 15.6ms 网格上。
        self.clock = clock
        self.life_detector = life_detector
        self.life_guard = life_guard
        self.completion_guard = completion_guard
        self.debug_recorder = debug_recorder
        self.timing_feedback_detector = timing_feedback_detector
        self.timing_controller = timing_controller
        # Native minitouch 整曲后端（默认关闭）。激活期间谱面触控全部由
        # 设备端脚本接管，本引擎只保留视觉检测、生命/结算与收尾职责。
        self.native_backend = native_backend
        self.playfield_monitor = playfield_monitor
        self.live_failed_detector = live_failed_detector
        # 谱面按压（reason=chart-predicted）按到期时刻派发，避免绑定到 60fps
        # 截图帧造成 ±8ms 的量化抖动；见 run() 中的等待间隙派发。
        self._scheduled_actions: list = []

    def native_backend_takeover(self) -> bool:
        """Native 后端是否独占谱面触控。"""
        backend = self.native_backend
        return bool(
            backend is not None
            and (
                getattr(backend, "exclusive", False)
                or getattr(backend, "takeover", False)
            )
        )

    def run(
        self,
        capture: Callable[[], np.ndarray],
        stopping: Callable[[], bool],
        *,
        duration_seconds: float | None,
        target_fps: int,
        continue_after_life_depleted: bool = False,
        life_exit_threshold: int | None = None,
        on_life_safety: Callable[[object], None] | None = None,
        touch_reset_life_threshold: int = 300,
        touch_reset_cooldown_seconds: float = 5.0,
        touch_reset_recent_action_seconds: float = 0.35,
        touch_reset_drop_threshold: int = 180,
        touch_reset_drop_window_seconds: float = 2.0,
        startup_timeout_seconds: float = 20.0,
    ) -> EngineStats:
        if duration_seconds is not None and not 1 <= duration_seconds <= 600:
            raise ValueError("duration_seconds 必须在 1..600 之间")
        if not 15 <= target_fps <= 120:
            raise ValueError("target_fps 必须在 15..120 之间")
        if not 1 <= startup_timeout_seconds <= 120:
            raise ValueError("startup_timeout_seconds 必须在 1..120 之间")
        # Windows 默认计时器粒度约 15.6ms，会吞掉到期派发需要的 1~2ms 精度。
        # 提升到 1ms 只影响本进程，是节奏类实时循环的标准做法。
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass
        interval = 1 / target_fps
        started_at = self.clock()
        deadline = (
            float("inf")
            if duration_seconds is None
            else started_at + duration_seconds
        )
        next_frame = started_at
        frames = actions_count = 0
        was_stopped = False
        aborted_for_life = False
        completed = False
        life_depleted = False
        life_failed = False
        startup_timed_out = False
        below_threshold_streak = 0
        touch_resets = 0
        last_touch_reset_at = float("-inf")
        touch_reset_life_samples: deque[tuple[float, int]] = deque()
        reading = None
        safety_reading = None
        base_stats: EngineStats | None = None
        run_error: Exception | None = None
        cleanup_errors: list[str] = []
        recorder_error: str | None = None
        action_counts: dict[str, int] = {}
        frame_interval_samples_ms: deque[float] = deque(
            maxlen=_FRAME_INTERVAL_SAMPLE_CAPACITY
        )
        frame_interval_sample_count = 0
        frame_interval_max_ms = 0.0
        first_processed_at: float | None = None
        previous_processed_at: float | None = None
        last_processed_at: float | None = None
        stage_samples_ms: dict[str, deque[float]] = {
            stage: deque(maxlen=_STAGE_SAMPLE_CAPACITY)
            for stage in _HOT_PATH_STAGES
        }
        stage_sample_counts = {stage: 0 for stage in _HOT_PATH_STAGES}
        stage_max_ms = {stage: 0.0 for stage in _HOT_PATH_STAGES}
        frame_interval_outliers: list[dict[str, object]] = []
        previous_tail_stage_ms: dict[str, float] = {}
        previous_frame_context: dict[str, int] | None = None
        initial_timing_offset_ms = int(
            getattr(self.planner, "timing_offset_ms", 0)
        )
        last_transient_action_at = float("-inf")
        hold_feedback_block_until = float("-inf")
        scheduled_actions = self._scheduled_actions
        scheduled_actions.clear()
        native_exclusive = self.native_backend_takeover()
        native_started = False
        startup_marker = (
            "演奏场"
            if self.playfield_monitor is not None
            else "生命条"
        )

        def record_stage_sample(stage: str, elapsed_ms: float) -> None:
            stage_samples_ms[stage].append(elapsed_ms)
            stage_sample_counts[stage] += 1
            stage_max_ms[stage] = max(stage_max_ms[stage], elapsed_ms)

        def record_terminal_life_frame(
            image: np.ndarray,
            now: float,
            *,
            life_status: str,
            life_value: int,
            reason: str,
        ) -> None:
            """Persist the fatal reading before the life guard exits the loop."""
            if self.debug_recorder is None:
                return
            trace_state = getattr(self.touch, "trace_state", None)
            touch_state = trace_state() if trace_state is not None else {
                "active_contacts": sorted(
                    getattr(self.touch, "active_contacts", ())
                ),
            }
            timing_state = (
                {
                    "initial_offset_ms": initial_timing_offset_ms,
                    "current_offset_ms": self.timing_controller.current_offset_ms,
                    "valid_samples": self.timing_controller.valid_samples,
                    "ignored_samples": self.timing_controller.ignored_samples,
                    "ignored_reasons": self.timing_controller.ignored_reasons,
                }
                if self.timing_controller is not None else {}
            )
            self.debug_recorder.record(
                image,
                now,
                [],
                [],
                life_status,
                [{
                    "event": "life_terminal",
                    "timestamp": now,
                    "reason": reason,
                    "life_value": life_value,
                }],
                timing_state,
                life_value,
                touch_state,
            )

        def record_startup_timeout_frame(
            image: np.ndarray,
            now: float,
            *,
            life_status: str,
        ) -> None:
            """Persist one diagnostic frame before normal trace capture starts."""
            if self.debug_recorder is None:
                return
            trace_state = getattr(self.touch, "trace_state", None)
            touch_state = trace_state() if trace_state is not None else {
                "active_contacts": sorted(
                    getattr(self.touch, "active_contacts", ())
                ),
            }
            self.debug_recorder.record(
                image,
                now,
                [],
                [],
                life_status,
                [{
                    "event": "playfield_start_timeout",
                    "timestamp": now,
                    "timeout_seconds": startup_timeout_seconds,
                    "reason": (
                        f"{startup_marker} was never confirmed after live start"
                    ),
                }],
                {},
                None,
                touch_state,
            )

        def snapshot_stats(terminal_reason: str) -> EngineStats:
            measured_seconds = (
                last_processed_at - first_processed_at
                if (
                    first_processed_at is not None
                    and last_processed_at is not None
                    and frame_interval_sample_count > 0
                )
                else 0.0
            )
            stage_timings_ms = {
                stage: {
                    "p50": (
                        float(np.percentile(samples, 50)) if samples else 0.0
                    ),
                    "p95": (
                        float(np.percentile(samples, 95)) if samples else 0.0
                    ),
                    "max": stage_max_ms[stage],
                    "sample_count": stage_sample_counts[stage],
                    "retained_samples": len(samples),
                    "percentile_scope": (
                        "full_run"
                        if stage_sample_counts[stage] == len(samples)
                        else "recent_window"
                    ),
                }
                for stage, samples in stage_samples_ms.items()
                if samples
            }
            return EngineStats(
                processed_frames=frames,
                dispatched_actions=actions_count,
                stopped=was_stopped,
                aborted_for_life=aborted_for_life,
                completed=completed,
                life_depleted=life_depleted,
                life_failed=life_failed,
                timing_feedback_fast=(
                    self.timing_controller.fast_samples
                    if self.timing_controller is not None else 0
                ),
                timing_feedback_slow=(
                    self.timing_controller.slow_samples
                    if self.timing_controller is not None else 0
                ),
                initial_timing_offset_ms=initial_timing_offset_ms,
                final_timing_offset_ms=(
                    initial_timing_offset_ms
                    if native_exclusive
                    else self.timing_controller.current_offset_ms
                    if self.timing_controller is not None
                    else int(getattr(self.planner, "timing_offset_ms", 0))
                ),
                timing_feedback_valid=(
                    self.timing_controller.valid_samples
                    if self.timing_controller is not None else 0
                ),
                timing_feedback_ignored=(
                    self.timing_controller.ignored_samples
                    if self.timing_controller is not None else 0
                ),
                timing_feedback_ignored_reasons=(
                    self.timing_controller.ignored_reasons
                    if self.timing_controller is not None else {}
                ),
                timing_feedback_sightings=int(
                    getattr(self.timing_feedback_detector, "sightings", 0)
                ),
                timing_feedback_reports=int(
                    getattr(self.timing_feedback_detector, "reports", 0)
                ),
                filtered_adjacent_artifacts=int(
                    getattr(self.planner, "filtered_adjacent_artifacts", 0)
                ),
                rejected_hold_candidates=int(
                    getattr(self.planner, "rejected_hold_candidates", 0)
                ),
                terminal_reason=terminal_reason,
                action_counts=action_counts,
                frame_interval_p50_ms=(
                    float(np.percentile(frame_interval_samples_ms, 50))
                    if frame_interval_samples_ms else 0.0
                ),
                frame_interval_p95_ms=(
                    float(np.percentile(frame_interval_samples_ms, 95))
                    if frame_interval_samples_ms else 0.0
                ),
                frame_interval_max_ms=frame_interval_max_ms,
                effective_fps=(
                    frame_interval_sample_count / measured_seconds
                    if measured_seconds > 0 else 0.0
                ),
                recovered_contacts=int(
                    getattr(self.touch, "recovered_contacts", 0)
                ),
                down_recoveries=int(
                    getattr(self.touch, "down_recoveries", 0)
                ),
                stale_move_recoveries=int(
                    getattr(self.touch, "stale_move_recoveries", 0)
                ),
                touch_resets=touch_resets,
                input_wait_count=int(
                    getattr(self.touch, "wait_count", 0)
                ),
                input_wait_total_ms=float(
                    getattr(self.touch, "wait_seconds_total", 0.0)
                ) * 1000.0,
                input_wait_max_ms=float(
                    getattr(self.touch, "wait_max_seconds", 0.0)
                ) * 1000.0,
                stage_timings_ms=stage_timings_ms,
                frame_interval_outliers=tuple(frame_interval_outliers),
                startup_timed_out=startup_timed_out,
                engine_mode="native" if native_exclusive else "legacy",
            )

        synchronize_touch = getattr(self.touch, "synchronize", None)
        try:
            if native_exclusive:
                arm_native = getattr(self.native_backend, "arm", None)
                if arm_native is None:
                    raise RuntimeError("Native 后端缺少 arm() 会话接口")
                arm_native()
            elif synchronize_touch is not None:
                synchronize_touch()
            while self.clock() < deadline:
                if stopping():
                    was_stopped = True
                    break
                now = self.clock()
                # 无论本轮是等待还是捕获，都先派发已到期的谱面按压，避免把
                # 按压重新量化到截图帧上。
                due_now = [
                    action for action in scheduled_actions
                    if action.timestamp <= now
                ]
                if due_now:
                    scheduled_actions[:] = [
                        action for action in scheduled_actions
                        if action.timestamp > now
                    ]
                    if not native_exclusive:
                        self.touch.dispatch(due_now)
                        actions_count += len(due_now)
                        for action in due_now:
                            kind = action.kind.value
                            action_counts[kind] = (
                                action_counts.get(kind, 0) + 1
                            )
                        if any(
                            action.kind.value in {"tap", "flick"}
                            for action in due_now
                        ):
                            last_transient_action_at = now
                if now < next_frame:
                    # Pace BEFORE capturing. Capturing first and then
                    # discarding the frame whenever the loop is early wastes
                    # a full screenshot; once per-frame work crosses the
                    # 60 Hz budget that waste halves the effective rate.
                    # 等待目标取下一帧与最早到期按压的较小者，确保到期
                    # 按压在计时器粒度内派发。
                    wait_target = next_frame
                    if scheduled_actions:
                        wait_target = min(
                            wait_target,
                            scheduled_actions[0].timestamp,
                        )
                    time.sleep(min(0.002, wait_target - now))
                    continue
                capture_interval = (
                    0.2 if native_exclusive and native_started else interval
                )
                next_frame += capture_interval
                if now - next_frame > capture_interval:
                    next_frame = now + capture_interval
                stage_started = self.clock()
                image = capture()
                now = self.clock()
                record_stage_sample("capture", (now - stage_started) * 1000)
                if stopping():
                    was_stopped = True
                    break
                if native_exclusive and not native_started:
                    observe_start = getattr(
                        self.native_backend, "observe_start_frame", None
                    )
                    if observe_start is None:
                        raise RuntimeError(
                            "Native 后端缺少 observe_start_frame() photogate 接口"
                        )
                    first_action_anchor = observe_start(image, now)
                    if first_action_anchor is None:
                        frames += 1
                        if now - started_at >= startup_timeout_seconds:
                            startup_timed_out = True
                            record_startup_timeout_frame(
                                image,
                                now,
                                life_status=LifeStatus.UNKNOWN.value,
                            )
                            break
                        # 触发前保持目标高帧率，只做截图和 photogate；不得
                        # 启动 Legacy detector/planner/touch 或生命监控。
                        continue
                    start_native = getattr(self.native_backend, "start", None)
                    if start_native is None:
                        raise RuntimeError("Native 后端缺少 start() 会话接口")
                    start_native(float(first_action_anchor))
                    native_started = True
                    if self.playfield_monitor is not None:
                        self.playfield_monitor.mark_active(now)
                    # 首拍之后截图只服务生命和终态识别，固定降到约 5Hz。
                    next_frame = now + 0.2
                # 死亡弹窗监控不依赖数值生命条；演奏场成立后以约 5Hz 检查
                # “演出失败”弹窗，必须先于演奏场消失判定，否则弹窗遮挡会
                # 被误判成“进入结算”。
                playfield_active = (
                    self.playfield_monitor is not None
                    and bool(getattr(self.playfield_monitor, "active", False))
                )
                if (
                    self.live_failed_detector is not None
                    and playfield_active
                    and self.live_failed_detector.observe(image, now)
                ):
                    life_failed = True
                    life_depleted = True
                    record_terminal_life_frame(
                        image,
                        now,
                        life_status=LifeStatus.DEAD.value,
                        life_value=0,
                        reason="life-failed-popup",
                    )
                    break
                if (
                    self.playfield_monitor is not None
                    and (not native_exclusive or native_started)
                ):
                    stage_started = self.clock()
                    playfield_state = self.playfield_monitor.observe(image, now)
                    record_stage_sample(
                        "playfield_monitor",
                        (self.clock() - stage_started) * 1000,
                    )
                    if playfield_state == "waiting":
                        frames += 1
                        if now - started_at >= startup_timeout_seconds:
                            startup_timed_out = True
                            record_startup_timeout_frame(
                                image,
                                now,
                                life_status=LifeStatus.UNKNOWN.value,
                            )
                            break
                        continue
                    if playfield_state == "completed":
                        completed = True
                        break
                    if playfield_state == "missing" and not native_exclusive:
                        # 结算转场不得再送入 Legacy 音符检测，避免白色动画
                        # 被误判为最后一批音符；Native 仍需继续轮询设备回执。
                        frames += 1
                        continue
                # Flick gestures span several game frames. Progress them here
                # so input never sleeps inside dispatch and blocks capture.
                advance_touch = getattr(self.touch, "advance", None)
                stage_started = self.clock()
                if advance_touch is not None and not native_exclusive:
                    advance_touch(now)
                record_stage_sample(
                    "touch_advance", (self.clock() - stage_started) * 1000
                )
                life_status = None
                stage_started = self.clock()
                try:
                    if self.life_detector is not None and self.life_guard is not None:
                        reading = self.life_detector.detect(image)
                        status = self.life_guard.update(reading)
                        life_status = status.value
                        if (
                            self.completion_guard is not None
                            and self.completion_guard.update(
                                reading, alive_confirmed=self.life_guard.alive_confirmed
                            )
                        ):
                            completed = True
                            break
                        if (
                            life_exit_threshold is not None
                            and self.life_guard.alive_confirmed
                            and reading.visible
                        ):
                            below_threshold_streak = (
                                below_threshold_streak + 1
                                if reading.value < life_exit_threshold else 0
                            )
                            if below_threshold_streak >= 3:
                                life_depleted = (
                                    life_depleted or status is LifeStatus.DEAD
                                )
                                aborted_for_life = True
                                safety_reading = reading
                                record_terminal_life_frame(
                                    image,
                                    now,
                                    life_status=status.value,
                                    life_value=reading.value,
                                    reason="life-exit-threshold",
                                )
                                break
                        elif not reading.visible:
                            # Invisible readings default to zero. Song-end fades
                            # must contribute to completion, not low-life debounce.
                            below_threshold_streak = 0
                        if status is LifeStatus.DEAD:
                            life_depleted = True
                            if not continue_after_life_depleted:
                                aborted_for_life = True
                                record_terminal_life_frame(
                                    image,
                                    now,
                                    life_status=status.value,
                                    life_value=reading.value,
                                    reason="life-dead",
                                )
                                break
                        if reading.visible and self.life_guard.alive_confirmed:
                            touch_reset_life_samples.append((now, reading.value))
                            while (
                                touch_reset_life_samples
                                and now - touch_reset_life_samples[0][0]
                                > touch_reset_drop_window_seconds
                            ):
                                touch_reset_life_samples.popleft()
                        # Never interpret transition pixels as notes until a
                        # non-zero life bar has been confirmed.
                        if not self.life_guard.alive_confirmed:
                            frames += 1
                            if now - started_at >= startup_timeout_seconds:
                                startup_timed_out = True
                                record_startup_timeout_frame(
                                    image,
                                    now,
                                    life_status=(
                                        life_status or LifeStatus.UNKNOWN.value
                                    ),
                                )
                                break
                            continue
                finally:
                    record_stage_sample(
                        "life", (self.clock() - stage_started) * 1000
                    )
                if native_exclusive and native_started:
                    poll_native = getattr(self.native_backend, "poll", None)
                    if poll_native is None:
                        raise RuntimeError("Native 后端缺少 poll() 会话接口")
                    stage_started = self.clock()
                    poll_native(now)
                    record_stage_sample(
                        "native_backend",
                        (self.clock() - stage_started) * 1000,
                    )

                diagnostics: list[dict[str, object]] = []
                reset_touch = getattr(
                    self.touch, "emergency_release_all", None
                )
                if reset_touch is not None and not native_exclusive:
                    has_live_touches = getattr(
                        self.touch,
                        "has_active_or_pending_contacts",
                        True,
                    )
                    if callable(has_live_touches):
                        has_live_touches = has_live_touches()
                    recent_peak_life = max(
                        (
                            value
                            for _sample_at, value in touch_reset_life_samples
                        ),
                        default=(reading.value if reading is not None else 0),
                    )
                    rapid_life_drop = (
                        reading is not None
                        and reading.visible
                        and recent_peak_life - reading.value
                        >= touch_reset_drop_threshold
                    )
                    life_drop_reset = (
                        self.life_guard is not None
                        and self.life_guard.alive_confirmed
                        and reading is not None
                        and rapid_life_drop
                        and bool(has_live_touches)
                        and now - last_transient_action_at
                        <= touch_reset_recent_action_seconds
                        and now - last_touch_reset_at
                        >= touch_reset_cooldown_seconds
                    )
                    if life_drop_reset:
                        # Device and planner touch state form one boundary.  A
                        # device-only release leaves the next planned MOVE
                        # referring to a contact that no longer exists; the
                        # controller then has to guess a DOWN and the gesture
                        # can remain broken for the rest of the hold.
                        reset_touch()
                        recover_planner = getattr(
                            self.planner, "recover_touch_state", None
                        )
                        if recover_planner is not None:
                            recover_planner(now)
                        touch_resets += 1
                        last_touch_reset_at = now
                        diagnostics.append({
                            "event": "touch_reset",
                            "timestamp": now,
                            "reason": "rapid-life-drop",
                            "life_value": reading.value,
                            "recent_peak_life": recent_peak_life,
                            "life_drop": recent_peak_life - reading.value,
                        })
                        touch_reset_life_samples.clear()
                        touch_reset_life_samples.append((now, reading.value))
                        print(
                            "RealtimeTouchReset reason=rapid-life-drop"
                            + f" life_value={reading.value}"
                            + f" recent_peak={recent_peak_life}",
                            flush=True,
                        )
                if native_exclusive:
                    # Native 从第 0 帧独占输入；Python 只保留生命与终态监控。
                    notes = []
                    actions = []
                    scheduled_actions.clear()
                else:
                    stage_started = self.clock()
                    notes = self.detector.detect(image, now)
                    record_stage_sample(
                        "detector", (self.clock() - stage_started) * 1000
                    )
                    stage_started = self.clock()
                    actions = self.planner.update(notes, now)
                    diagnostics.extend(self.planner.drain_diagnostics())
                    record_stage_sample(
                        "planner", (self.clock() - stage_started) * 1000
                    )
                # 谱面动作拆成“立即”与“到期派发”。DOWN/MOVE 必须立即派发
                # 以维持 hold 触点生命周期与视觉跟随 MOVE 的因果顺序；
                # TAP/FLICK/UP 按到期时刻毫秒级发送，消除帧对齐量化。
                recorded_actions = actions
                scheduled_now = [
                    action for action in actions
                    if (
                        action.reason in {"chart-predicted", "chart-tail"}
                        and action.kind not in {
                            ActionKind.DOWN,
                            ActionKind.MOVE,
                        }
                        and action.timestamp > now + 0.002
                    )
                ]
                if scheduled_now:
                    scheduled_ids = {id(action) for action in scheduled_now}
                    actions = [
                        action for action in actions
                        if id(action) not in scheduled_ids
                    ]
                    scheduled_actions.extend(scheduled_now)
                    scheduled_actions.sort(
                        key=lambda action: action.timestamp
                    )
                current_processed_at = now
                frame_interval_ms = None
                if first_processed_at is None:
                    first_processed_at = current_processed_at
                if previous_processed_at is not None:
                    frame_interval_ms = (
                        current_processed_at - previous_processed_at
                    ) * 1000
                    frame_interval_samples_ms.append(frame_interval_ms)
                    frame_interval_sample_count += 1
                    frame_interval_max_ms = max(
                        frame_interval_max_ms, frame_interval_ms
                    )
                previous_processed_at = current_processed_at
                last_processed_at = current_processed_at
                if any(
                    action.kind.value in {"tap", "flick"}
                    for action in recorded_actions
                ):
                    last_transient_action_at = now
                if any(
                    action.kind.value == "up"
                    for action in recorded_actions
                ):
                    hold_feedback_block_until = max(
                        hold_feedback_block_until, now + .15
                    )
                stage_started = self.clock()
                if (
                    not native_exclusive
                    and
                    self.timing_feedback_detector is not None
                    and self.timing_controller is not None
                ):
                    feedback = self.timing_feedback_detector.detect(image)
                    # hold 常驻期间普通 TAP 的 FAST/SLOW 判定条仍是有效信号；
                    # 整段丢弃会让 drift 完全失去局内修正（实机曾出现 10 个
                    # FAST 全程 valid=0）。hold 尾判定假信号由下方
                    # recent_hold_release 窗口单独屏蔽。
                    if now < hold_feedback_block_until:
                        eligible = False
                        ignored_reason = "recent_hold_release"
                    elif now - last_transient_action_at > .6:
                        eligible = False
                        ignored_reason = "no_recent_transient_input"
                    else:
                        eligible = True
                        ignored_reason = ""
                    adjusted = self.timing_controller.update(
                        feedback,
                        now,
                        eligible=eligible,
                        ignored_reason=ignored_reason,
                    )
                    if adjusted is not None:
                        self.planner.set_timing_offset_ms(adjusted)
                record_stage_sample(
                    "timing_feedback", (self.clock() - stage_started) * 1000
                )
                stage_started = self.clock()
                if actions and not native_exclusive:
                    self.touch.dispatch(actions)
                    actions_count += len(actions)
                    for action in actions:
                        kind = action.kind.value
                        action_counts[kind] = action_counts.get(kind, 0) + 1
                record_stage_sample(
                    "dispatch", (self.clock() - stage_started) * 1000
                )
                stage_started = self.clock()
                if self.debug_recorder is not None:
                    timing_state = (
                        {
                            "initial_offset_ms": initial_timing_offset_ms,
                            "current_offset_ms": self.timing_controller.current_offset_ms,
                            "valid_samples": self.timing_controller.valid_samples,
                            "ignored_samples": self.timing_controller.ignored_samples,
                            "ignored_reasons": self.timing_controller.ignored_reasons,
                        }
                        if self.timing_controller is not None else {}
                    )
                    trace_state = getattr(self.touch, "trace_state", None)
                    touch_state = trace_state() if trace_state is not None else {
                        "active_contacts": sorted(
                            getattr(self.touch, "active_contacts", ())
                        ),
                    }
                    life_value = (
                        reading.value
                        if reading is not None and reading.visible
                        else None
                    )
                    self.debug_recorder.record(
                        image, now, notes, recorded_actions, life_status,
                        diagnostics, timing_state, life_value, touch_state,
                    )
                record_stage_sample(
                    "recorder_enqueue", (self.clock() - stage_started) * 1000
                )
                active_contacts = len(
                    getattr(self.touch, "active_contacts", ())
                )
                current_frame_context = {
                    "frame": frames,
                    "notes": len(notes),
                    "actions": len(recorded_actions),
                    "active_contacts": active_contacts,
                }
                if frame_interval_ms is not None:
                    if frame_interval_ms > 100:
                        interval_stage_ms = {
                            "capture": stage_samples_ms["capture"][-1],
                            **previous_tail_stage_ms,
                        }
                        unattributed_ms = max(
                            0.0,
                            frame_interval_ms - sum(interval_stage_ms.values()),
                        )
                        interval_stage_ms["unattributed"] = unattributed_ms
                        dominant_stage = max(
                            interval_stage_ms,
                            key=interval_stage_ms.__getitem__,
                        )
                        dominant_context = current_frame_context
                        if (
                            dominant_stage in previous_tail_stage_ms
                            and previous_frame_context is not None
                        ):
                            dominant_context = previous_frame_context
                        frame_interval_outliers.append({
                            "frame": frames,
                            "dominant_stage_frame": dominant_context["frame"],
                            "elapsed_ms": (current_processed_at - started_at) * 1000,
                            "interval_ms": frame_interval_ms,
                            "dominant_stage": dominant_stage,
                            "dominant_stage_ms": interval_stage_ms[dominant_stage],
                            "unattributed_ms": unattributed_ms,
                            "notes": dominant_context["notes"],
                            "actions": dominant_context["actions"],
                            "active_contacts": dominant_context["active_contacts"],
                        })
                        frame_interval_outliers.sort(
                            key=lambda event: float(event["interval_ms"]),
                            reverse=True,
                        )
                        del frame_interval_outliers[8:]
                previous_tail_stage_ms = {
                    stage: stage_samples_ms[stage][-1]
                    for stage in _HOT_PATH_STAGES
                    if stage != "capture" and stage_samples_ms[stage]
                }
                previous_frame_context = current_frame_context
                frames += 1
            # 演奏已进入终态：丢弃尚未派发的谱面按压，绝不在结算/停止后补发。
            scheduled_actions.clear()
            if was_stopped:
                terminal_reason = "用户已停止任务"
            elif aborted_for_life:
                terminal_reason = "生命值触发安全停止"
            elif life_failed:
                terminal_reason = "演出失败：生命值归零"
            elif completed:
                terminal_reason = "已识别演奏结束并进入结算"
            elif startup_timed_out:
                terminal_reason = (
                    f"开演后 {startup_timeout_seconds:g} 秒仍未识别到"
                    f"{startup_marker}，"
                    "可能停留在加载页、网络弹窗或非演奏画面"
                )
            elif duration_seconds is not None:
                terminal_reason = (
                    f"演奏超过安全时限 {duration_seconds:g} 秒，"
                    "仍未识别到结算画面"
                )
            else:
                terminal_reason = "持续监听演奏已结束"
            base_stats = snapshot_stats(terminal_reason)
        except Exception as exc:
            run_error = exc
            base_stats = snapshot_stats(
                "实时演奏引擎异常: "
                f"{type(exc).__name__}: {exc}"
            )
        finally:
            if not native_exclusive:
                try:
                    cleanup = self.planner.reset(self.clock())
                except Exception as exc:
                    cleanup = []
                    cleanup_errors.append(
                        f"planner_reset={type(exc).__name__}: {exc}"
                    )
                try:
                    if cleanup:
                        self.touch.dispatch(cleanup)
                except Exception as exc:
                    cleanup_errors.append(
                        f"cleanup_dispatch={type(exc).__name__}: {exc}"
                    )
            if self.native_backend is not None:
                try:
                    set_terminal_reason = getattr(
                        self.native_backend, "set_terminal_reason", None
                    )
                    if set_terminal_reason is not None and base_stats is not None:
                        set_terminal_reason(base_stats.terminal_reason)
                    self.native_backend.stop()
                except Exception as exc:
                    cleanup_errors.append(
                        f"native_backend_stop={type(exc).__name__}: {exc}"
                    )
            try:
                self.touch.close()
            except Exception as exc:
                cleanup_errors.append(
                    f"touch_close={type(exc).__name__}: {exc}"
                )
            if on_life_safety is not None and safety_reading is not None:
                try:
                    on_life_safety(safety_reading)
                except Exception as exc:
                    cleanup_errors.append(
                        f"life_safety={type(exc).__name__}: {exc}"
                    )
            if self.debug_recorder is not None:
                try:
                    self.debug_recorder.close()
                except Exception as exc:
                    recorder_error = f"{type(exc).__name__}: {exc}"
                    print(
                        "RealtimeEngine recorder_error=" + recorder_error,
                        flush=True,
                    )
        if base_stats is None:
            raise RuntimeError("realtime engine finished without statistics")
        terminal_reason = base_stats.terminal_reason
        if cleanup_errors:
            detail = "; ".join(cleanup_errors)
            terminal_reason = (
                f"{terminal_reason}; 实时触控收尾失败: {detail}"
                if terminal_reason
                else f"实时触控收尾失败: {detail}"
            )
        native_report: dict[str, object] = {}
        if native_exclusive and self.native_backend is not None:
            try:
                native_report = dict(self.native_backend.report())
            except Exception as exc:
                cleanup_errors.append(
                    f"native_backend_report={type(exc).__name__}: {exc}"
                )
        final_stats = replace(
            base_stats,
            terminal_reason=terminal_reason,
            cleanup_failed=bool(cleanup_errors),
            cleanup_errors=tuple(cleanup_errors),
            recorder_error=recorder_error,
            dispatched_actions=(
                int(native_report.get("sent", 0))
                if native_exclusive else base_stats.dispatched_actions
            ),
            action_counts=(
                dict(native_report.get("action_counts", {}))
                if native_exclusive else base_stats.action_counts
            ),
            native_report=native_report,
        )
        if run_error is not None:
            run_error.realtime_stats = final_stats
            raise run_error
        if self.timing_feedback_detector is not None and not native_exclusive:
            detector = self.timing_feedback_detector
            print(
                "RealtimeTimingFeedback "
                f"sightings={getattr(detector, 'sightings', -1)} "
                f"reports={getattr(detector, 'reports', -1)}",
                flush=True,
            )
        return final_stats
