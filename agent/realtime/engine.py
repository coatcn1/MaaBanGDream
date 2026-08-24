from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace

import numpy as np

from .controller_touch import ControllerTouchDispatcher
from .note_detector import NoteDetector
from .life_monitor import LifeDetector, LifeGuard, LifeStatus, PlayfieldCompletionGuard
from .timing_feedback import AdaptiveTimingController, TimingFeedbackDetector
from .touch_planner import RealtimePlanner


_HOT_PATH_STAGES = (
    "capture",
    "touch_advance",
    "life",
    "detector",
    "planner",
    "timing_feedback",
    "recorder_enqueue",
    "dispatch",
)
_STAGE_SAMPLE_CAPACITY = 120 * 600
_FRAME_INTERVAL_SAMPLE_CAPACITY = 120 * 600


class DebugRecorder:
    def record(
        self, image, timestamp, notes, actions, life_status,
        diagnostics=None, timing_state=None,
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
    timing_feedback_fast: int = 0
    timing_feedback_slow: int = 0
    initial_timing_offset_ms: int = 0
    final_timing_offset_ms: int = 0
    timing_feedback_valid: int = 0
    timing_feedback_ignored: int = 0
    timing_feedback_ignored_reasons: dict[str, int] = field(default_factory=dict)
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


class RealtimeEngine:
    """Own the realtime lifecycle and guarantee touch cleanup on every exit."""

    def __init__(
        self,
        detector: NoteDetector,
        planner: RealtimePlanner,
        touch: ControllerTouchDispatcher,
        clock: Callable[[], float] = time.monotonic,
        life_detector: LifeDetector | None = None,
        life_guard: LifeGuard | None = None,
        completion_guard: PlayfieldCompletionGuard | None = None,
        debug_recorder: DebugRecorder | None = None,
        timing_feedback_detector: TimingFeedbackDetector | None = None,
        timing_controller: AdaptiveTimingController | None = None,
    ) -> None:
        self.detector = detector
        self.planner = planner
        self.touch = touch
        self.clock = clock
        self.life_detector = life_detector
        self.life_guard = life_guard
        self.completion_guard = completion_guard
        self.debug_recorder = debug_recorder
        self.timing_feedback_detector = timing_feedback_detector
        self.timing_controller = timing_controller

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
    ) -> EngineStats:
        if duration_seconds is not None and not 1 <= duration_seconds <= 600:
            raise ValueError("duration_seconds 必须在 1..600 之间")
        if not 15 <= target_fps <= 120:
            raise ValueError("target_fps 必须在 15..120 之间")
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
        below_threshold_streak = 0
        touch_resets = 0
        last_touch_reset_at = float("-inf")
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

        def record_stage_sample(stage: str, elapsed_ms: float) -> None:
            stage_samples_ms[stage].append(elapsed_ms)
            stage_sample_counts[stage] += 1
            stage_max_ms[stage] = max(stage_max_ms[stage], elapsed_ms)

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
            }
            return EngineStats(
                processed_frames=frames,
                dispatched_actions=actions_count,
                stopped=was_stopped,
                aborted_for_life=aborted_for_life,
                completed=completed,
                life_depleted=life_depleted,
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
                    self.timing_controller.current_offset_ms
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
            )

        synchronize_touch = getattr(self.touch, "synchronize", None)
        if synchronize_touch is not None:
            synchronize_touch()
        try:
            while self.clock() < deadline:
                if stopping():
                    was_stopped = True
                    break
                now = self.clock()
                if now < next_frame:
                    # Pace BEFORE capturing. Capturing first and then
                    # discarding the frame whenever the loop is early wastes
                    # a full screenshot; once per-frame work crosses the
                    # 60 Hz budget that waste halves the effective rate.
                    time.sleep(min(0.002, next_frame - now))
                    continue
                next_frame += interval
                if now - next_frame > interval:
                    next_frame = now + interval
                stage_started = self.clock()
                image = capture()
                now = self.clock()
                record_stage_sample("capture", (now - stage_started) * 1000)
                if stopping():
                    was_stopped = True
                    break
                # Flick gestures span several game frames. Progress them here
                # so input never sleeps inside dispatch and blocks capture.
                advance_touch = getattr(self.touch, "advance", None)
                stage_started = self.clock()
                if advance_touch is not None:
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
                                break
                        elif not reading.visible:
                            # Invisible readings default to zero. Song-end fades
                            # must contribute to completion, not low-life debounce.
                            below_threshold_streak = 0
                        if status is LifeStatus.DEAD:
                            life_depleted = True
                            if not continue_after_life_depleted:
                                aborted_for_life = True
                                break
                        # Never interpret transition pixels as notes until a
                        # non-zero life bar has been confirmed.
                        if not self.life_guard.alive_confirmed:
                            frames += 1
                            continue
                finally:
                    record_stage_sample(
                        "life", (self.clock() - stage_started) * 1000
                    )
                stage_started = self.clock()
                notes = self.detector.detect(image, now)
                record_stage_sample(
                    "detector", (self.clock() - stage_started) * 1000
                )
                stage_started = self.clock()
                actions = self.planner.update(notes, now)
                diagnostics = self.planner.drain_diagnostics()
                record_stage_sample(
                    "planner", (self.clock() - stage_started) * 1000
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
                    action.kind.value in {"tap", "flick"} for action in actions
                ):
                    last_transient_action_at = now
                if any(action.kind.value == "up" for action in actions):
                    hold_feedback_block_until = max(
                        hold_feedback_block_until, now + .4
                    )
                stage_started = self.clock()
                if (
                    self.timing_feedback_detector is not None
                    and self.timing_controller is not None
                ):
                    feedback = self.timing_feedback_detector.detect(image)
                    if self.planner.has_active_holds:
                        eligible = False
                        ignored_reason = "active_hold"
                    elif now < hold_feedback_block_until:
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
                    self.debug_recorder.record(
                        image, now, notes, actions, life_status,
                        diagnostics, timing_state,
                    )
                record_stage_sample(
                    "recorder_enqueue", (self.clock() - stage_started) * 1000
                )
                stage_started = self.clock()
                if actions:
                    self.touch.dispatch(actions)
                    actions_count += len(actions)
                    for action in actions:
                        kind = action.kind.value
                        action_counts[kind] = action_counts.get(kind, 0) + 1
                record_stage_sample(
                    "dispatch", (self.clock() - stage_started) * 1000
                )
                active_contacts = len(
                    getattr(self.touch, "active_contacts", ())
                )
                reset_touch = getattr(
                    self.touch, "emergency_release_all", None
                )
                if reset_touch is not None:
                    life_drop_reset = (
                        self.life_guard is not None
                        and self.life_guard.alive_confirmed
                        and reading is not None
                        and reading.value <= touch_reset_life_threshold
                        and now - last_transient_action_at
                        <= touch_reset_recent_action_seconds
                        and now - last_touch_reset_at
                        >= touch_reset_cooldown_seconds
                    )
                    if life_drop_reset:
                        reset_touch()
                        touch_resets += 1
                        last_touch_reset_at = now
                        print(
                            "RealtimeTouchReset reason=life-drop"
                            + f" life_value={reading.value if reading is not None else -1}",
                            flush=True,
                        )
                current_frame_context = {
                    "frame": frames,
                    "notes": len(notes),
                    "actions": len(actions),
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
                    if stage != "capture"
                }
                previous_frame_context = current_frame_context
                frames += 1
            if was_stopped:
                terminal_reason = "用户已停止任务"
            elif aborted_for_life:
                terminal_reason = "生命值触发安全停止"
            elif completed:
                terminal_reason = "已识别演奏结束并进入结算"
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
        final_stats = replace(
            base_stats,
            terminal_reason=terminal_reason,
            cleanup_failed=bool(cleanup_errors),
            cleanup_errors=tuple(cleanup_errors),
            recorder_error=recorder_error,
        )
        if run_error is not None:
            run_error.realtime_stats = final_stats
            raise run_error
        return final_stats
