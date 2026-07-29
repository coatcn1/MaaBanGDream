from __future__ import annotations

import json
import time
import traceback
from datetime import datetime

import cv2

try:
    from ..foreground_guard import require_game_foreground
    from ..task_reporting import record_failure_reason
except ImportError:  # AgentServer imports realtime as a top-level package.
    from foreground_guard import require_game_foreground
    from task_reporting import record_failure_reason

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from .controller_touch import ControllerTouchDispatcher
from .debug_recorder import RealtimeDebugRecorder
from .engine import RealtimeEngine
from .life_monitor import LifeDetector, LifeGuard, PlayfieldCompletionGuard
from .note_detector import NoteDetector
from .profile_action import PROJECT_ROOT
from .profile_store import EnvironmentSignature, RealtimeProfileStore
from .rehearsal_action import frame_resolution
from .result_parser import LiveResult, ResultParser, adjusted_timing_offset
from .timing_feedback import AdaptiveTimingController, TimingFeedbackDetector
from .touch_planner import RealtimePlanner, sliding_holds_enabled
from .runtime_options import debug_enabled
from .performance_settings_action import verified_settings


_LAST_LIFE_SAFETY_ABORT = False


def resolve_life_policy(
    params: dict,
    runtime_options: dict,
) -> tuple[bool, bool, int | None]:
    """Return rehearsal mode, continue-after-depletion and safety threshold."""
    require_profile = bool(params.get("require_profile", True))
    is_rehearsal = bool(params.get("rehearsal_mode", not require_profile))
    ignore_rehearsal_life = bool(
        runtime_options.get("rehearsal_ignore_life_safety", True)
    )
    continue_after_depleted = bool(params.get(
        "continue_after_life_depleted",
        is_rehearsal and ignore_rehearsal_life,
    ))
    default_use_safety = not (is_rehearsal and ignore_rehearsal_life)
    use_life_safety = bool(params.get("use_life_safety", default_use_safety))
    life_threshold = (
        int(runtime_options["life_exit_threshold"])
        if use_life_safety and runtime_options["life_safety_enabled"] else None
    )
    return is_rehearsal, continue_after_depleted, life_threshold


def pause_overlay_changed(before, after) -> bool:
    if before.shape != after.shape or before.size == 0:
        return False
    height, width = before.shape[:2]
    roi = (slice(height // 8, height * 7 // 8), slice(width // 8, width * 7 // 8))
    difference = cv2.absdiff(before[roi], after[roi])
    return float(difference.mean()) >= 8.0


def _write_calibration_report(path, *, result, stats, timing_offset_ms, song_id):
    payload = {
        **result.to_dict(),
        "timing_offset_ms": int(timing_offset_ms),
        "initial_timing_offset_ms": stats.initial_timing_offset_ms,
        "song_id": str(song_id),
        "survived": not stats.life_depleted,
        "completed": bool(stats.completed),
        "realtime_feedback_fast": stats.timing_feedback_fast,
        "realtime_feedback_slow": stats.timing_feedback_slow,
        "realtime_feedback_valid": stats.timing_feedback_valid,
        "realtime_feedback_ignored": stats.timing_feedback_ignored,
        "realtime_feedback_ignored_reasons": stats.timing_feedback_ignored_reasons,
        "filtered_adjacent_artifacts": stats.filtered_adjacent_artifacts,
        "rejected_hold_candidates": stats.rejected_hold_candidates,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _result_report_payload(
    result: LiveResult,
    stats,
    *,
    timing_offset_ms: int,
    suggested_timing_offset_ms: int,
) -> dict:
    return {
        **result.to_dict(),
        "initial_timing_offset_ms": timing_offset_ms,
        "current_timing_offset_ms": stats.final_timing_offset_ms,
        "suggested_timing_offset_ms": suggested_timing_offset_ms,
        "realtime_feedback_fast": stats.timing_feedback_fast,
        "realtime_feedback_slow": stats.timing_feedback_slow,
        "realtime_feedback_valid": stats.timing_feedback_valid,
        "realtime_feedback_ignored": stats.timing_feedback_ignored,
        "realtime_feedback_ignored_reasons": stats.timing_feedback_ignored_reasons,
        "filtered_adjacent_artifacts": stats.filtered_adjacent_artifacts,
        "rejected_hold_candidates": stats.rejected_hold_candidates,
        "processed_frames": stats.processed_frames,
        "dispatched_actions": stats.dispatched_actions,
        "action_counts": stats.action_counts,
        "frame_interval_p50_ms": stats.frame_interval_p50_ms,
        "frame_interval_p95_ms": stats.frame_interval_p95_ms,
        "frame_interval_max_ms": stats.frame_interval_max_ms,
        "effective_fps": stats.effective_fps,
        "terminal_reason": stats.terminal_reason,
    }


def _result_counts(result: LiveResult) -> tuple[int, ...]:
    return (
        result.perfect, result.great, result.good, result.bad,
        result.miss, result.fast, result.slow,
    )


def _settle_result(
    controller,
    stopping,
    *,
    parser,
    candidate: tuple[LiveResult, object],
    sleeper,
    attempts: int,
    interval_seconds: float,
) -> tuple[LiveResult, object]:
    """Wait for the result panel's count-up animation to finish.

    The panel counts every judgement number up from zero over a couple of
    seconds. A frame caught mid-animation parses cleanly (the glyphs are
    crisp) but reports tiny values, so a single successful parse is not
    trustworthy: only a reading that survives a re-check unchanged counts.
    No input is sent while settling — the panel is already visible.
    """
    result, _ = candidate
    for _ in range(attempts):
        if stopping():
            raise InterruptedError("任务停止，取消结算读取")
        deadline = time.monotonic() + interval_seconds
        while time.monotonic() < deadline:
            if stopping():
                raise InterruptedError("任务停止，取消结算读取")
            sleeper(min(.1, max(0.0, deadline - time.monotonic())))
        latest_image = controller.post_screencap().wait().get()
        try:
            latest = parser.parse(latest_image)
        except ValueError:
            continue
        if _result_counts(latest) == _result_counts(result):
            return latest, latest_image
        result = latest
    raise ValueError("结算数字长时间未稳定，放弃本次读取")


def collect_result(
    controller,
    stopping,
    *,
    attempts: int = 30,
    interval_seconds: float = 1.5,
    parser: ResultParser | None = None,
    sleeper=time.sleep,
    before_input=lambda: None,
    stability_attempts: int = 10,
    stability_interval_seconds: float = 1.0,
) -> tuple[LiveResult, object]:
    """Press BACK one step at a time until a valid result panel is visible."""
    parser = parser or ResultParser()
    for attempt in range(attempts + 1):
        if stopping():
            raise InterruptedError("任务停止，取消结算读取")
        image = controller.post_screencap().wait().get()
        try:
            result = parser.parse(image)
        except ValueError:
            pass
        else:
            return _settle_result(
                controller,
                stopping,
                parser=parser,
                candidate=(result, image),
                sleeper=sleeper,
                attempts=stability_attempts,
                interval_seconds=stability_interval_seconds,
            )
        if attempt >= attempts:
            break
        if stopping():
            raise InterruptedError("任务停止，取消结算读取")
        before_input()
        controller.post_click_key(4).wait()
        deadline = time.monotonic() + interval_seconds
        while time.monotonic() < deadline:
            if stopping():
                raise InterruptedError("任务停止，取消结算读取")
            sleeper(min(.1, max(0.0, deadline - time.monotonic())))
    raise ValueError("连续按 BACK 后仍未识别到有效结算画面")


def resolve_profile_for_settings_gate(context: Context, params: dict, *, controller=None):
    controller = controller or context.tasker.controller
    image = controller.post_screencap().wait().get()
    signature = EnvironmentSignature(
        frame_resolution(image),
        int(params.get("dpi", 240)),
        int(params.get("game_fps", 60)),
        str(params.get("render_quality", "standard")),
        1.0,
    )
    return RealtimeProfileStore(
        PROJECT_ROOT / "profiles"
    ).resolve_latest_for_environment(
        difficulty=str(params.get("difficulty", "Easy")),
        current_signature=signature,
    )


def resolve_profile(context: Context, params: dict, *, controller=None):
    controller = controller or context.tasker.controller
    difficulty = str(params.get("difficulty", "Easy"))
    verified = verified_settings(difficulty)
    if bool(params.get("settings_gate_required", False)) and verified is None:
        raise RuntimeError("本次开演前尚未实际验证游戏流速")
    note_speed = (
        verified.actual_note_speed
        if verified is not None
        else float(params.get("note_speed", 2.0))
    )
    image = controller.post_screencap().wait().get()
    signature = EnvironmentSignature(
        frame_resolution(image),
        int(params.get("dpi", 240)),
        int(params.get("game_fps", 60)),
        str(params.get("render_quality", "standard")),
        note_speed,
    )
    store = RealtimeProfileStore(PROJECT_ROOT / "profiles")
    if verified is not None and verified.profile:
        return store.resolve(
            verified.profile,
            difficulty=difficulty,
            current_signature=signature,
        )
    return store.resolve_latest(
        difficulty=difficulty,
        current_signature=signature,
    )


@AgentServer.custom_action("RealtimeProfileCheck")
class RealtimeProfileCheck(CustomAction):
    """Refuse to start a live before its accepted Profile is available."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            if context.tasker.stopping:
                return True
            params = json.loads(argv.custom_action_param or "{}")
            settings = resolve_profile_for_settings_gate(context, params)
            print(
                "RealtimeProfileCheck "
                f"profile={settings.profile_path.name} "
                f"expected_speed={settings.note_speed:.2f}",
                flush=True,
            )
            return True
        except Exception as exc:
            traceback.print_exc()
            print(f"RealtimeProfileCheck failed={type(exc).__name__}: {exc}", flush=True)
            return False


@AgentServer.custom_action("RealtimeProfilePlay")
class RealtimeProfilePlay(CustomAction):
    """Run a bounded rehearsal using only a matching accepted local profile."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            return self._run(context, argv)
        except Exception as exc:
            record_failure_reason(f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
            print(f"RealtimeProfilePlay failed={type(exc).__name__}: {exc}", flush=True)
            return False

    def _run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        global _LAST_LIFE_SAFETY_ABORT
        _LAST_LIFE_SAFETY_ABORT = False
        params = json.loads(argv.custom_action_param or "{}")
        if context.tasker.stopping:
            return True
        controller = context.tasker.controller
        require_profile = bool(params.get("require_profile", True))
        difficulty = str(params.get("difficulty", "Easy"))
        verified = verified_settings(difficulty)
        if bool(params.get("settings_gate_required", False)) and verified is None:
            raise RuntimeError("本次开演前尚未实际验证游戏流速")
        settings = (
            resolve_profile(context, params, controller=controller)
            if require_profile else None
        )
        if context.tasker.stopping:
            return True
        target_fps = settings.target_fps if settings else int(params.get("target_fps", 60))
        timing_offset_ms = (
            settings.timing_offset_ms if settings else int(params.get("timing_offset_ms", 0))
        )
        mode = f"profile={settings.profile_path.name}" if settings else "mode=rehearsal-defaults"
        speed_message = (
            f"actual_speed={verified.actual_note_speed:.2f} "
            f"expected_speed={verified.expected_note_speed:.2f}"
            if verified is not None
            else f"declared_speed={float(params.get('note_speed', 2.0)):.2f}"
        )
        print(f"RealtimeProfilePlay {mode} {speed_message}", flush=True)
        recorder = (
            RealtimeDebugRecorder(PROJECT_ROOT / "debug" / "recordings")
            if (params.get("debug_recording") or debug_enabled()) else None
        )
        if recorder is not None:
            print(f"RealtimeProfilePlay debug={recorder.output_dir}", flush=True)
        # Foreground verification is intentionally outside the realtime touch
        # hot path. A dumpsys query before every down/move/up blocks capture for
        # 100-450 ms and turns otherwise correct notes into SLOW judgements.
        require_game_foreground(controller)
        touch = ControllerTouchDispatcher(
            controller,
            lambda: context.tasker.stopping,
        )
        engine = RealtimeEngine(
            NoteDetector(),
            RealtimePlanner(
                judgement_y=565,
                timing_offset_ms=timing_offset_ms,
                rescue_first_visible=True,
                enable_slide=sliding_holds_enabled(
                    str(params.get("difficulty", "Easy"))
                ),
            ),
            touch,
            life_detector=LifeDetector(),
            life_guard=LifeGuard(),
            completion_guard=(
                PlayfieldCompletionGuard(
                    int(params.get("completion_missing_frames", 120))
                )
                if params.get("wait_for_completion")
                else None
            ),
            debug_recorder=recorder,
            timing_feedback_detector=TimingFeedbackDetector(),
            timing_controller=AdaptiveTimingController(timing_offset_ms),
        )
        runtime_options = RealtimeProfileStore(PROJECT_ROOT / "profiles").runtime_options()
        is_rehearsal, continue_after_depleted, life_threshold = resolve_life_policy(
            params, runtime_options,
        )
        print(
            "RealtimeProfilePlay life_policy "
            f"rehearsal={is_rehearsal} "
            f"continue_after_depleted={continue_after_depleted} "
            f"threshold={life_threshold}",
            flush=True,
        )

        def pause_for_life(reading) -> None:
            global _LAST_LIFE_SAFETY_ABORT
            _LAST_LIFE_SAFETY_ABORT = True
            require_game_foreground(controller)
            before = controller.post_screencap().wait().get()
            controller.post_click(1237, 58).wait()
            time.sleep(.4)
            after = controller.post_screencap().wait().get()
            confirmed = pause_overlay_changed(before, after)
            print(
                f"RealtimeProfilePlay life_safety value={reading.value} "
                f"threshold={life_threshold} pause_confirmed={confirmed}",
                flush=True,
            )
            if not confirmed:
                raise RuntimeError("life safety triggered but pause overlay was not confirmed")

        stats = engine.run(
            lambda: controller.post_screencap().wait().get(),
            lambda: context.tasker.stopping,
            duration_seconds=float(params.get("duration_seconds", 30)),
            target_fps=target_fps,
            continue_after_life_depleted=continue_after_depleted,
            life_exit_threshold=life_threshold,
            on_life_safety=pause_for_life if life_threshold is not None else None,
        )
        print(
            "RealtimeProfilePlay "
            f"frames={stats.processed_frames} actions={stats.dispatched_actions} "
            f"stopped={stats.stopped} life_abort={stats.aborted_for_life} "
            f"life_depleted={stats.life_depleted} completed={stats.completed} "
            f"feedback_fast={stats.timing_feedback_fast} "
            f"feedback_slow={stats.timing_feedback_slow} "
            f"feedback_valid={stats.timing_feedback_valid} "
            f"feedback_ignored={stats.timing_feedback_ignored} "
            f"filtered_adjacent={stats.filtered_adjacent_artifacts} "
            f"rejected_holds={stats.rejected_hold_candidates} "
            f"timing_offset={stats.initial_timing_offset_ms}"
            f"->{stats.final_timing_offset_ms} "
            f"tap={stats.action_counts.get('tap', 0)} "
            f"flick={stats.action_counts.get('flick', 0)} "
            f"hold={stats.action_counts.get('down', 0)} "
            f"frame_ms_p50={stats.frame_interval_p50_ms:.2f} "
            f"frame_ms_p95={stats.frame_interval_p95_ms:.2f} "
            f"frame_ms_max={stats.frame_interval_max_ms:.2f} "
            f"effective_fps={stats.effective_fps:.2f} "
            f"reason={stats.terminal_reason}",
            flush=True,
        )
        if stats.completed and params.get("save_result_frame"):
            result_data, result = collect_result(
                controller,
                lambda: context.tasker.stopping,
                attempts=int(params.get("result_back_attempts", 30)),
                interval_seconds=float(params.get("result_back_interval_seconds", 1.5)),
                before_input=lambda: require_game_foreground(controller),
            )
            output = PROJECT_ROOT / "screencap"
            output.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = output / f"realtime-result-{stamp}.png"
            if not cv2.imwrite(str(path), result):
                raise OSError(f"无法保存结算截图: {path}")
            effective_timing_offset_ms = stats.final_timing_offset_ms
            suggestion = adjusted_timing_offset(
                effective_timing_offset_ms, result_data,
            )
            report = output / f"realtime-result-{stamp}.json"
            report.write_text(json.dumps(_result_report_payload(
                result_data,
                stats,
                timing_offset_ms=timing_offset_ms,
                suggested_timing_offset_ms=suggestion,
            ), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(
                f"RealtimeProfilePlay result_frame={path.name} "
                f"perfect={result_data.perfect} great={result_data.great} "
                f"good={result_data.good} bad={result_data.bad} miss={result_data.miss} "
                f"fast={result_data.fast} slow={result_data.slow} "
                f"timing_offset={timing_offset_ms}"
                f"->{effective_timing_offset_ms}->{suggestion}",
                flush=True,
            )
            calibration_report = params.get("calibration_report")
            if calibration_report:
                from .calibration_action import current_song_id

                report_path = PROJECT_ROOT / str(calibration_report)
                report_path.parent.mkdir(parents=True, exist_ok=True)
                _write_calibration_report(
                    report_path,
                    result=result_data,
                    stats=stats,
                    timing_offset_ms=effective_timing_offset_ms,
                    song_id=current_song_id(),
                )
        if stats.stopped:
            print("[任务][实时演奏][结束][INFO] 用户已停止任务", flush=True)
            return True
        success = not stats.aborted_for_life
        if params.get("require_completion"):
            success = success and stats.completed
        if not success:
            reason = stats.terminal_reason or "实时演奏引擎未完成"
            record_failure_reason(reason)
            print(f"[任务][实时演奏][演奏][ERROR] {reason}", flush=True)
        return success


@AgentServer.custom_action("RealtimeLifeSafetyAbortCheck")
class RealtimeLifeSafetyAbortCheck(CustomAction):
    """Route a protected abort to StopTask while ordinary failures may recover."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        return _LAST_LIFE_SAFETY_ABORT
