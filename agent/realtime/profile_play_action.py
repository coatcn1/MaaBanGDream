from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path

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
from .engine import EngineStats, RealtimeEngine
from .game_effect_settings_action import verified_game_visual_settings
from .life_monitor import LifeDetector, LifeGuard, PlayfieldCompletionGuard
from .live_session import (
    LiveRunContext,
    current_live_run,
    reset_live_run,
    update_live_run,
)
from .note_detector import NoteDetector
from .profile_action import PROJECT_ROOT
from .profile_store import EnvironmentSignature, RealtimeProfileStore
from .rehearsal_action import frame_resolution
from .result_parser import LiveResult, ResultParser, adjusted_timing_offset
from .run_reporting import (
    PreflightPerformanceSnapshot,
    result_report_payload as _result_report_payload,
    write_json_atomic as _write_json_atomic,
    write_preflight_terminal_result,
)
from .timing_feedback import AdaptiveTimingController, TimingFeedbackDetector
from .touch_planner import RealtimePlanner, sliding_holds_enabled
from .runtime_options import debug_enabled
from .performance_settings_action import verified_settings
from .chart_timeline import ChartTimeline


REWARD_CONFIRM_TEMPLATE = PROJECT_ROOT / "resource" / "image" / "result_reward_confirm.png"
REWARD_OK_TEMPLATE = PROJECT_ROOT / "resource" / "image" / "result_reward_ok.png"
REWARD_TEMPLATE_THRESHOLD = 0.85
REWARD_DISMISS_LIMIT = 3
REWARD_CLICK_DELAY_SECONDS = 1.0


_LAST_LIFE_SAFETY_ABORT = False


class StallSafeCapture:
    """Screencap wrapper that never blocks the engine for a full stall.

    LDPlayer's EmulatorExtras screencap can freeze for 200-400 ms under
    load.  A blocking capture stalls the whole engine loop, so every note
    due during that window goes unhit and the song fails.  This wrapper
    double-buffers: it returns the latest completed frame immediately and
    posts the next capture right away so the screencap overlaps the engine's
    detection/planning work.  When the backend is stuck, the wrapper reuses
    the last completed frame instead of blocking, so the engine clock and
    the chart-timeline after-due rescues keep advancing.
    """

    def __init__(self, controller, *, timeout_seconds: float = 0.05):
        self._controller = controller
        self._timeout_seconds = float(timeout_seconds)
        self._last_image = None
        self._pending = None
        self.stall_count = 0

    @staticmethod
    def _job_done(job) -> bool:
        try:
            return bool(job.done)
        except Exception:
            return True

    def __call__(self):
        if self._pending is not None and self._job_done(self._pending):
            try:
                image = self._pending.get()
                if image is not None:
                    self._last_image = image
            except Exception:
                pass
            self._pending = None
        if self._pending is None:
            # Start the next capture immediately so it overlaps the engine's
            # detection/planning work (true double buffering).
            self._pending = self._controller.post_screencap()
        if self._last_image is None and not self._job_done(self._pending):
            # The very first frame must exist before the detector can run;
            # blocking once here is unavoidable and only happens at startup.
            self._pending.wait()
            self._last_image = self._pending.get()
            self._pending = self._controller.post_screencap()
            return self._last_image
        if self._job_done(self._pending):
            try:
                image = self._pending.get()
            except Exception:
                image = None
            if image is None:
                if self._last_image is None:
                    # First frame must exist before the detector can run.
                    image = self._controller.post_screencap().wait().get()
                else:
                    image = self._last_image
            self._last_image = image
            # Pre-post the next capture for the following frame.
            self._pending = self._controller.post_screencap()
            return image
        # The in-flight capture has not finished: reuse the last completed
        # frame so the engine clock and chart rescues keep advancing.
        self.stall_count += 1
        return self._last_image


def _run_mode(params: dict, *, is_rehearsal: bool) -> str:
    explicit = params.get("run_mode")
    if explicit:
        return str(explicit)
    if params.get("visual_evaluation"):
        return "visual-evaluation"
    if params.get("calibration_report"):
        return "calibration"
    if params.get("ignore_note_speed"):
        return "continuous"
    return "rehearsal" if is_rehearsal else "formal"


def _relative_artifact_path(path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


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


def _write_calibration_report(
    path,
    *,
    result,
    stats,
    timing_offset_ms,
    song_id="unknown",
    run_context: LiveRunContext | None = None,
):
    payload = _result_report_payload(
        result,
        stats,
        timing_offset_ms=stats.initial_timing_offset_ms,
        suggested_timing_offset_ms=int(timing_offset_ms),
        run_context=run_context,
        result_status="stable",
    )
    payload.update({
        "timing_offset_ms": int(timing_offset_ms),
        "initial_timing_offset_ms": stats.initial_timing_offset_ms,
        "survived": not stats.life_depleted,
        "completed": bool(stats.completed),
    })
    if run_context is None:
        payload["song_id"] = str(song_id)
    _write_json_atomic(path, payload)


def _result_counts(result: LiveResult) -> tuple[int, ...]:
    return (
        result.perfect, result.great, result.good, result.bad,
        result.miss, result.fast, result.slow,
    )


class ResultCollectionStatus(str, Enum):
    STABLE = "stable"
    TIMED_OUT = "timed_out"
    STOPPED = "stopped"


@dataclass(frozen=True)
class ResultCollectionOutcome:
    status: ResultCollectionStatus
    result: LiveResult | None = None
    image: object | None = None
    elapsed_seconds: float = 0.0


def _wait_until(deadline, stopping, *, clock, sleeper) -> bool:
    while clock() < deadline:
        if stopping():
            return False
        sleeper(min(.1, max(0.0, deadline - clock())))
    return not stopping()


def _dismiss_reward_popup(
    controller,
    image,
    *,
    before_input=lambda: None,
    templates=(REWARD_CONFIRM_TEMPLATE, REWARD_OK_TEMPLATE),
    threshold: float = REWARD_TEMPLATE_THRESHOLD,
) -> bool:
    """Click a visible achievement-reward OK button, if any."""
    best_score = threshold
    best_point = None
    for template_path in templates:
        template = cv2.imread(str(template_path))
        if template is None:
            continue
        matched = cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(matched)
        if score > best_score:
            best_score = score
            height, width = template.shape[:2]
            best_point = (location[0] + width // 2, location[1] + height // 2)
    if best_point is None:
        return False
    before_input()
    controller.post_click(*best_point).wait()
    return True


def collect_result(
    controller,
    stopping,
    *,
    parser: ResultParser | None = None,
    sleeper=time.sleep,
    clock=time.monotonic,
    before_input=lambda: None,
    timeout_seconds: float = 60.0,
    slow_phase_seconds: float = 30.0,
    slow_interval_seconds: float = 1.5,
    medium_interval_seconds: float = 1.0,
    stability_interval_seconds: float = 1.0,
    reward_templates=(REWARD_CONFIRM_TEMPLATE, REWARD_OK_TEMPLATE),
    reward_threshold: float = REWARD_TEMPLATE_THRESHOLD,
    reward_dismiss_limit: int = REWARD_DISMISS_LIMIT,
    reward_click_delay_seconds: float = REWARD_CLICK_DELAY_SECONDS,
) -> ResultCollectionOutcome:
    """Use only ESC while seeking a stable result, bounded by 60 seconds."""
    parser = parser or ResultParser()
    started_at = clock()
    deadline = started_at + timeout_seconds
    candidate: LiveResult | None = None
    candidate_at = 0.0
    last_image = None
    none_streak = 0
    dismissals = 0
    while clock() < deadline:
        if stopping():
            return ResultCollectionOutcome(
                ResultCollectionStatus.STOPPED,
                elapsed_seconds=clock() - started_at,
            )
        image = controller.post_screencap().wait().get()
        last_image = image
        try:
            result = parser.parse(image)
        except ValueError:
            result = None

        now = clock()
        if result is not None:
            none_streak = 0
            if (
                candidate is not None
                and now - candidate_at >= stability_interval_seconds
                and _result_counts(result) == _result_counts(candidate)
            ):
                return ResultCollectionOutcome(
                    ResultCollectionStatus.STABLE,
                    result=result,
                    image=image,
                    elapsed_seconds=now - started_at,
                )
            if candidate is None or _result_counts(result) != _result_counts(candidate):
                candidate = result
                candidate_at = now
            if not _wait_until(
                min(deadline, now + stability_interval_seconds),
                stopping,
                clock=clock,
                sleeper=sleeper,
            ):
                return ResultCollectionOutcome(
                    ResultCollectionStatus.STOPPED,
                    elapsed_seconds=clock() - started_at,
                )
            continue

        if candidate is not None:
            none_streak = 0
            if not _wait_until(
                min(deadline, now + stability_interval_seconds),
                stopping,
                clock=clock,
                sleeper=sleeper,
            ):
                return ResultCollectionOutcome(
                    ResultCollectionStatus.STOPPED,
                    elapsed_seconds=clock() - started_at,
                )
            continue

        none_streak += 1
        if (
            none_streak >= 2
            and dismissals < reward_dismiss_limit
            and _dismiss_reward_popup(
                controller,
                image,
                before_input=before_input,
                templates=reward_templates,
                threshold=reward_threshold,
            )
        ):
            dismissals += 1
            none_streak = 0
            if not _wait_until(
                min(deadline, now + reward_click_delay_seconds),
                stopping,
                clock=clock,
                sleeper=sleeper,
            ):
                return ResultCollectionOutcome(
                    ResultCollectionStatus.STOPPED,
                    elapsed_seconds=clock() - started_at,
                )
            continue

        before_input()
        controller.post_click_key(4).wait()
        elapsed = now - started_at
        interval = (
            slow_interval_seconds
            if elapsed < slow_phase_seconds
            else medium_interval_seconds
        )
        if not _wait_until(
            min(deadline, now + interval),
            stopping,
            clock=clock,
            sleeper=sleeper,
        ):
            return ResultCollectionOutcome(
                ResultCollectionStatus.STOPPED,
                elapsed_seconds=clock() - started_at,
            )

    return ResultCollectionOutcome(
        ResultCollectionStatus.TIMED_OUT,
        image=last_image,
        elapsed_seconds=clock() - started_at,
    )


def _visual_signature_values(
    store: RealtimeProfileStore,
    *,
    require_verified: bool,
) -> tuple[int, int, bool]:
    verified = verified_game_visual_settings()
    if verified is not None:
        return (
            verified.note_skin_type,
            verified.tap_effect,
            verified.judgement_assist_effect,
        )
    if require_verified:
        raise RuntimeError("本次开演前尚未实际验证游戏视觉设置")
    options = store.runtime_options()
    return (
        int(options.get("note_skin_type", 1)),
        int(options.get("tap_effect", 1)),
        bool(options.get("judgement_assist_effect", True)),
    )


def resolve_profile_for_settings_gate(
    context: Context,
    params: dict,
    *,
    controller=None,
    require_verified_visual: bool = False,
):
    controller = controller or context.tasker.controller
    store = RealtimeProfileStore(PROJECT_ROOT / "profiles")
    note_skin_type, tap_effect, judgement_assist_effect = (
        _visual_signature_values(store, require_verified=require_verified_visual)
    )
    image = controller.post_screencap().wait().get()
    signature = EnvironmentSignature(
        frame_resolution(image),
        int(params.get("dpi", 240)),
        int(params.get("game_fps", 60)),
        str(params.get("render_quality", "standard")),
        1.0,
        note_skin_type,
        tap_effect,
        judgement_assist_effect,
    )
    resolver = (
        store.resolve_latest_for_visual_evaluation_environment
        if params.get("visual_evaluation")
        else store.resolve_latest_for_environment
    )
    return resolver(
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
    store = RealtimeProfileStore(PROJECT_ROOT / "profiles")
    note_skin_type, tap_effect, judgement_assist_effect = (
        _visual_signature_values(
            store,
            require_verified=bool(params.get("settings_gate_required", False)),
        )
    )
    image = controller.post_screencap().wait().get()
    signature = EnvironmentSignature(
        frame_resolution(image),
        int(params.get("dpi", 240)),
        int(params.get("game_fps", 60)),
        str(params.get("render_quality", "standard")),
        note_speed,
        note_skin_type,
        tap_effect,
        judgement_assist_effect,
    )
    visual_evaluation = bool(params.get("visual_evaluation", False))
    if verified is not None and verified.profile:
        resolver = (
            store.resolve_for_visual_evaluation
            if visual_evaluation else store.resolve
        )
        return resolver(
            verified.profile,
            difficulty=difficulty,
            current_signature=signature,
        )
    latest_resolver = (
        store.resolve_latest_for_visual_evaluation
        if visual_evaluation else store.resolve_latest
    )
    return latest_resolver(
        difficulty=difficulty,
        current_signature=signature,
    )


@AgentServer.custom_action("RealtimeProfileCheck")
class RealtimeProfileCheck(CustomAction):
    """Refuse to start a live before its accepted Profile is available."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        params: dict = {}
        try:
            if context.tasker.stopping:
                return True
            params = json.loads(argv.custom_action_param or "{}")
            settings = resolve_profile_for_settings_gate(
                context, params, require_verified_visual=True,
            )
            print(
                "RealtimeProfileCheck "
                f"profile={settings.profile_path.name} "
                f"expected_speed={settings.note_speed:.2f}",
                flush=True,
            )
            return True
        except Exception as exc:
            if context.tasker.stopping:
                print("RealtimeProfileCheck stopped=true", flush=True)
                return True
            reason = f"{type(exc).__name__}: {exc}"
            record_failure_reason(reason)
            try:
                write_preflight_terminal_result(
                    output_dir=PROJECT_ROOT / "screencap",
                    params=params,
                    terminal_stage="profile_check",
                    reason=reason,
                    visual_settings=verified_game_visual_settings(),
                )
            except Exception as artifact_error:
                print(
                    "RealtimeProfileCheck artifact_failed="
                    f"{type(artifact_error).__name__}: {artifact_error}",
                    flush=True,
                )
                traceback.print_exc()
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
        verified = None
        settings = None
        visual = None
        try:
            controller = context.tasker.controller
            require_profile = bool(params.get("require_profile", True))
            difficulty = str(params.get("difficulty", "Easy"))
            ignore_note_speed = bool(params.get("ignore_note_speed", False))
            verified = (
                None if ignore_note_speed else verified_settings(difficulty)
            )
            if (
                bool(params.get("settings_gate_required", False))
                and verified is None
            ):
                raise RuntimeError("本次开演前尚未实际验证游戏流速")
            visual = verified_game_visual_settings()
            if (
                bool(params.get("settings_gate_required", False))
                and visual is None
            ):
                raise RuntimeError("本次开演前尚未实际验证游戏视觉设置")
            settings = (
                (
                    resolve_profile_for_settings_gate(
                        context, params, controller=controller,
                    )
                    if ignore_note_speed
                    else resolve_profile(context, params, controller=controller)
                )
                if require_profile else None
            )
            if context.tasker.stopping:
                return True
            target_fps = (
                settings.target_fps
                if settings else int(params.get("target_fps", 60))
            )
            timing_offset_ms = (
                settings.timing_offset_ms
                if settings else int(params.get("timing_offset_ms", 0))
            )
            runtime_options = RealtimeProfileStore(
                PROJECT_ROOT / "profiles"
            ).runtime_options()
            chart_prediction_enabled = (
                bool(runtime_options.get("chart_prediction_enabled", False))
                and sliding_holds_enabled(
                    str(params.get("difficulty", "Easy"))
                )
            )
            chart_predict_presses = bool(
                runtime_options.get("chart_predict_presses", False)
            )
            chart_timeline = None
            if chart_prediction_enabled:
                chart_path = (
                    PROJECT_ROOT / "resource" / "charts" / "song-306-hard.json"
                )
                if chart_path.is_file():
                    chart_timeline = ChartTimeline.from_json(chart_path)
                    print(
                        "RealtimeProfilePlay chart_prediction=on",
                        flush=True,
                    )
                else:
                    chart_prediction_enabled = False
                    print(
                        "RealtimeProfilePlay chart_prediction=off "
                        "chart file missing",
                        flush=True,
                    )
            (
                is_rehearsal,
                continue_after_depleted,
                life_threshold,
            ) = resolve_life_policy(params, runtime_options)
            debug_recording = bool(
                params.get("debug_recording") or debug_enabled()
            )
            run_mode = _run_mode(params, is_rehearsal=is_rehearsal)
            expected_note_speed = (
                verified.expected_note_speed
                if verified is not None
                else float(
                    getattr(
                        settings,
                        "note_speed",
                        params.get("note_speed", 2.0),
                    )
                )
            )
            actual_note_speed = (
                verified.actual_note_speed if verified is not None else None
            )
            live_run = current_live_run()
            if (
                live_run is None
                or run_mode == "continuous"
                or not live_run.prepared_for_play
            ):
                live_run = reset_live_run(
                    mode=run_mode,
                    difficulty=difficulty,
                )
            else:
                live_run = update_live_run(prepared_for_play=False)
            live_run = update_live_run(
                mode=run_mode,
                difficulty=difficulty,
                profile_name=(settings.profile_path.name if settings else None),
                expected_note_speed=expected_note_speed,
                actual_note_speed=actual_note_speed,
                note_skin_type=(
                    visual.note_skin_type if visual is not None else None
                ),
                tap_effect=(visual.tap_effect if visual is not None else None),
                judgement_assist=(
                    visual.judgement_assist_effect
                    if visual is not None else None
                ),
                debug_recording=debug_recording,
                recording_path=None,
            )
        except Exception as exc:
            if context.tasker.stopping:
                return True
            reason = f"{type(exc).__name__}: {exc}"
            performance_snapshot = None
            if verified is not None:
                performance_snapshot = PreflightPerformanceSnapshot(
                    expected_note_speed=float(verified.expected_note_speed),
                    actual_note_speed=float(verified.actual_note_speed),
                    profile=(
                        verified.profile
                        or (
                            settings.profile_path.name
                            if settings is not None else None
                        )
                    ),
                )
            elif settings is not None:
                performance_snapshot = PreflightPerformanceSnapshot(
                    expected_note_speed=float(
                        getattr(
                            settings,
                            "note_speed",
                            params.get("note_speed", 2.0),
                        )
                    ),
                    profile=settings.profile_path.name,
                )
            try:
                write_preflight_terminal_result(
                    output_dir=PROJECT_ROOT / "screencap",
                    params=params,
                    terminal_stage="profile_play_preflight",
                    reason=reason,
                    visual_settings=visual,
                    performance_snapshot=performance_snapshot,
                )
            except Exception as artifact_error:
                print(
                    "RealtimeProfilePlay preflight_artifact_failed="
                    f"{type(artifact_error).__name__}: {artifact_error}",
                    flush=True,
                )
                traceback.print_exc()
            raise

        def write_failure_artifacts(
            stats: EngineStats,
            *,
            result_status: str,
            reason: str,
        ) -> None:
            calibration_report = params.get("calibration_report")
            if not params.get("save_result_frame") and not calibration_report:
                return
            payload = _result_report_payload(
                None,
                stats,
                timing_offset_ms=timing_offset_ms,
                suggested_timing_offset_ms=None,
                run_context=live_run,
                result_status=result_status,
                reason=reason,
            )
            if params.get("save_result_frame"):
                output = PROJECT_ROOT / "screencap"
                output.mkdir(parents=True, exist_ok=True)
                stamp = (
                    datetime.now().strftime("%Y%m%d-%H%M%S")
                    + f"-{live_run.run_id[:8]}"
                )
                _write_json_atomic(
                    output / f"realtime-result-{stamp}.json", payload,
                )
            if calibration_report:
                report_path = PROJECT_ROOT / str(calibration_report)
                report_path.parent.mkdir(parents=True, exist_ok=True)
                _write_json_atomic(report_path, {
                    **payload,
                    "timing_offset_ms": stats.final_timing_offset_ms,
                    "survived": not stats.life_depleted,
                    "completed": bool(stats.completed),
                })

        mode = f"profile={settings.profile_path.name}" if settings else "mode=rehearsal-defaults"
        speed_message = (
            f"actual_speed={verified.actual_note_speed:.2f} "
            f"expected_speed={verified.expected_note_speed:.2f}"
            if verified is not None
            else f"declared_speed={float(params.get('note_speed', 2.0)):.2f}"
        )
        print(f"RealtimeProfilePlay {mode} {speed_message}", flush=True)
        if ignore_note_speed and settings is not None:
            print(
                "RealtimeProfilePlay listener_mode=true "
                f"profile_speed={settings.note_speed:.2f} "
                "actual game note speed must match the accepted Profile",
                flush=True,
            )
        touch = None
        recorder = None
        try:
            require_game_foreground(controller)
            # Foreground verification is intentionally outside the realtime
            # touch hot path. A dumpsys query before every down/move/up blocks
            # capture for 100-450 ms and causes otherwise correct SLOW notes.
            touch = ControllerTouchDispatcher(
                controller,
                lambda: context.tasker.stopping,
            )
            recorder = (
                RealtimeDebugRecorder(PROJECT_ROOT / "debug" / "recordings")
                if debug_recording else None
            )
            if recorder is not None:
                live_run = update_live_run(
                    recording_path=_relative_artifact_path(recorder.output_dir),
                )
                recorder.set_session_metadata(live_run.to_mapping())
                print(
                    f"RealtimeProfilePlay debug={recorder.output_dir}",
                    flush=True,
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
                    chart_timeline=chart_timeline,
                    chart_prediction=chart_prediction_enabled,
                    chart_predict_presses=(
                        chart_predict_presses
                        and chart_prediction_enabled
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
                timing_controller=AdaptiveTimingController(
                    timing_offset_ms,
                    # Hard+ sessions drift their game-side input latency by
                    # 10-20 ms run to run; adapt faster and wider so the
                    # finale does not play at the wrong end of the window.
                    # Normal keeps the gentler defaults.
                    **(
                        {
                            "step_ms": 2,
                            "minimum_samples": 8,
                            "imbalance": 6,
                            "window_size": 12,
                            "adjustment_cooldown_seconds": 1.0,
                            "maximum_live_adjustment_ms": 20,
                        }
                        if sliding_holds_enabled(
                            str(params.get("difficulty", "Easy"))
                        )
                        else {}
                    ),
                ),
            )
        except Exception as setup_error:
            cleanup_errors = []
            recorder_error = None
            if recorder is not None:
                try:
                    recorder.close()
                except Exception as cleanup_error:
                    recorder_error = (
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            if touch is not None:
                try:
                    touch.close()
                except Exception as cleanup_error:
                    cleanup_errors.append(
                        f"touch_close={type(cleanup_error).__name__}: {cleanup_error}"
                    )
            reason = f"preflight error: {type(setup_error).__name__}: {setup_error}"
            preflight_stats = EngineStats(
                0,
                0,
                False,
                initial_timing_offset_ms=timing_offset_ms,
                final_timing_offset_ms=timing_offset_ms,
                terminal_reason=reason,
                cleanup_failed=bool(cleanup_errors),
                cleanup_errors=tuple(cleanup_errors),
                recorder_error=recorder_error,
            )
            try:
                write_failure_artifacts(
                    preflight_stats,
                    result_status="preflight_error",
                    reason=reason,
                )
            except Exception as artifact_error:
                setup_error.add_note(
                    "preflight artifact write failed: "
                    f"{type(artifact_error).__name__}: {artifact_error}"
                )
            raise
        save_screenshot = recorder is not None
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
            confirmed = False
            for attempt in range(2):
                try:
                    require_game_foreground(controller)
                    before = controller.post_screencap().wait().get()
                    controller.post_click(1237, 58).wait()
                    time.sleep(.4)
                    after = controller.post_screencap().wait().get()
                    confirmed = pause_overlay_changed(before, after)
                except Exception:
                    confirmed = False
                if confirmed:
                    break
            print(
                f"RealtimeProfilePlay life_safety value={reading.value} "
                f"threshold={life_threshold} pause_confirmed={confirmed}",
                flush=True,
            )
            if not confirmed:
                print(
                    "RealtimeProfilePlay life_safety warning: "
                    "pause overlay was not confirmed; touches are already "
                    "released, continuing as a life-safety abort",
                    flush=True,
                )

        duration_value = params.get("duration_seconds", 30)
        duration_seconds = (
            None if duration_value is None else float(duration_value)
        )
        try:
            stall_safe_capture = StallSafeCapture(controller)
            stats = engine.run(
                stall_safe_capture,
                lambda: context.tasker.stopping,
                duration_seconds=duration_seconds,
                target_fps=target_fps,
                continue_after_life_depleted=continue_after_depleted,
                life_exit_threshold=life_threshold,
                on_life_safety=(
                    pause_for_life if life_threshold is not None else None
                ),
            )
        except Exception as exc:
            error_stats = getattr(exc, "realtime_stats", None)
            if error_stats is not None and context.tasker.stopping:
                stopped_reason = "用户已停止任务"
                stopped_stats = replace(
                    error_stats,
                    stopped=True,
                    terminal_reason=stopped_reason,
                )
                write_failure_artifacts(
                    stopped_stats,
                    result_status="stopped",
                    reason=stopped_reason,
                )
                return True
            if error_stats is None:
                cleanup_errors = []
                recorder_error = None
                if recorder is not None:
                    try:
                        recorder.close()
                    except Exception as cleanup_error:
                        recorder_error = (
                            f"{type(cleanup_error).__name__}: {cleanup_error}"
                        )
                if touch is not None:
                    try:
                        touch.close()
                    except Exception as cleanup_error:
                        cleanup_errors.append(
                            "touch_close="
                            f"{type(cleanup_error).__name__}: {cleanup_error}"
                        )
                reason = f"preflight error: {type(exc).__name__}: {exc}"
                error_stats = EngineStats(
                    0,
                    0,
                    False,
                    initial_timing_offset_ms=timing_offset_ms,
                    final_timing_offset_ms=timing_offset_ms,
                    terminal_reason=reason,
                    cleanup_failed=bool(cleanup_errors),
                    cleanup_errors=tuple(cleanup_errors),
                    recorder_error=recorder_error,
                )
                status = "preflight_error"
            else:
                reason = (
                    error_stats.terminal_reason
                    or f"{type(exc).__name__}: {exc}"
                )
                status = "engine_error"
            write_failure_artifacts(
                error_stats,
                result_status=status,
                reason=reason,
            )
            raise
        capture_metrics = stats.stage_timings_ms.get("capture", {})
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
            f"capture_ms_p95={capture_metrics.get('p95', 0.0):.2f} "
            f"capture_ms_max={capture_metrics.get('max', 0.0):.2f} "
            f"frame_outliers={len(stats.frame_interval_outliers)} "
            f"actual_speed={live_run.actual_note_speed} "
            f"expected_speed={live_run.expected_note_speed} "
            f"note_skin_type={live_run.note_skin_type} "
            f"tap_effect={live_run.tap_effect} "
            f"judgement_assist={live_run.judgement_assist} "
            f"touch_recoveries={stats.recovered_contacts} "
            f"down_recoveries={stats.down_recoveries} "
            f"stale_move_recoveries={stats.stale_move_recoveries} "
            f"input_wait_count={stats.input_wait_count} "
            f"input_wait_total_ms={stats.input_wait_total_ms:.1f} "
            f"input_wait_max_ms={stats.input_wait_max_ms:.1f} "
            f"reason={stats.terminal_reason}",
            flush=True,
        )
        result_output = PROJECT_ROOT / "screencap"
        result_stamp = (
            datetime.now().strftime("%Y%m%d-%H%M%S")
            + f"-{live_run.run_id[:8]}"
        )
        save_result = bool(params.get("save_result_frame"))
        result_report_path = result_output / f"realtime-result-{result_stamp}.json"

        def write_calibration_payload(payload: dict) -> None:
            calibration_report = params.get("calibration_report")
            if not calibration_report:
                return
            report_path = PROJECT_ROOT / str(calibration_report)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(report_path, {
                **payload,
                "timing_offset_ms": stats.final_timing_offset_ms,
                "survived": not stats.life_depleted,
                "completed": bool(stats.completed),
            })

        if save_result and stats.stopped:
            result_output.mkdir(parents=True, exist_ok=True)
            stopped_payload = _result_report_payload(
                None,
                stats,
                timing_offset_ms=timing_offset_ms,
                suggested_timing_offset_ms=None,
                run_context=live_run,
                result_status="stopped",
                reason=stats.terminal_reason or "用户已停止任务",
            )
            _write_json_atomic(result_report_path, stopped_payload)
            write_calibration_payload(stopped_payload)

        if save_result and (
            not stats.completed or stats.cleanup_failed
        ) and not stats.stopped:
            result_output.mkdir(parents=True, exist_ok=True)
            status = (
                "life_safety_abort" if stats.aborted_for_life
                else "cleanup_failed" if stats.cleanup_failed
                else "engine_incomplete"
            )
            failed_payload = _result_report_payload(
                None,
                stats,
                timing_offset_ms=timing_offset_ms,
                suggested_timing_offset_ms=None,
                run_context=live_run,
                result_status=status,
                reason=stats.terminal_reason or "实时演奏引擎未完成",
            )
            _write_json_atomic(result_report_path, failed_payload)
            write_calibration_payload(failed_payload)

        if stats.completed and not stats.cleanup_failed and save_result:
            result_output.mkdir(parents=True, exist_ok=True)
            try:
                outcome = collect_result(
                    controller,
                    lambda: context.tasker.stopping,
                    before_input=lambda: require_game_foreground(controller),
                    timeout_seconds=60.0,
                )
            except Exception as exc:
                reason = (
                    "结算读取异常: "
                    f"{type(exc).__name__}: {exc}"
                )
                collection_error_payload = _result_report_payload(
                    None,
                    stats,
                    timing_offset_ms=timing_offset_ms,
                    suggested_timing_offset_ms=None,
                    run_context=live_run,
                    result_status="result_collection_error",
                    reason=reason,
                )
                _write_json_atomic(
                    result_report_path, collection_error_payload,
                )
                write_calibration_payload(collection_error_payload)
                raise
            if outcome.status is ResultCollectionStatus.STOPPED:
                stopped_payload = _result_report_payload(
                    None,
                    stats,
                    timing_offset_ms=timing_offset_ms,
                    suggested_timing_offset_ms=None,
                    run_context=live_run,
                    result_status="stopped",
                    reason="用户在结算读取期间停止任务",
                )
                _write_json_atomic(result_report_path, stopped_payload)
                write_calibration_payload(stopped_payload)
                print("RealtimeProfilePlay result collection stopped by user", flush=True)
                return True
            if outcome.status is ResultCollectionStatus.TIMED_OUT:
                diagnostic = result_output / (
                    f"realtime-result-timeout-{result_stamp}.png"
                )
                if save_screenshot and outcome.image is not None:
                    cv2.imwrite(str(diagnostic), outcome.image)
                reason = "结算数字在 60 秒内未稳定，已跳过本次读取并继续"
                timeout_payload = _result_report_payload(
                    None,
                    stats,
                    timing_offset_ms=timing_offset_ms,
                    suggested_timing_offset_ms=None,
                    run_context=live_run,
                    result_status="timed_out",
                    reason=reason,
                )
                _write_json_atomic(result_report_path, timeout_payload)
                write_calibration_payload(timeout_payload)
                print(
                    "RealtimeProfilePlay result_timeout=true "
                    f"diagnostic={diagnostic.name if save_screenshot else 'none'} "
                    f"reason={reason}",
                    flush=True,
                )
                return True
            result_data = outcome.result
            result = outcome.image
            if result_data is None or result is None:
                reason = "结算读取返回 stable，但判定数据或画面不完整"
                incomplete_payload = _result_report_payload(
                    None,
                    stats,
                    timing_offset_ms=timing_offset_ms,
                    suggested_timing_offset_ms=None,
                    run_context=live_run,
                    result_status="result_collection_error",
                    reason=reason,
                )
                _write_json_atomic(result_report_path, incomplete_payload)
                write_calibration_payload(incomplete_payload)
                raise RuntimeError(reason)
            screenshot_path = result_output / f"realtime-result-{result_stamp}.png"
            screenshot_error = None
            if save_screenshot:
                try:
                    if not cv2.imwrite(str(screenshot_path), result):
                        screenshot_error = (
                            f"无法保存结算截图: {screenshot_path}"
                        )
                except Exception as exc:
                    screenshot_error = (
                        "保存结算截图异常: "
                        f"{type(exc).__name__}: {exc}"
                    )
            effective_timing_offset_ms = stats.final_timing_offset_ms
            suggestion = adjusted_timing_offset(
                effective_timing_offset_ms, result_data,
            )
            stable_payload = _result_report_payload(
                result_data,
                stats,
                timing_offset_ms=timing_offset_ms,
                suggested_timing_offset_ms=suggestion,
                run_context=live_run,
                result_status=(
                    "experimental"
                    if run_mode == "visual-evaluation" else "stable"
                ),
            )
            if screenshot_error is not None:
                stable_payload["result_screenshot_error"] = screenshot_error
            _write_json_atomic(result_report_path, stable_payload)
            if screenshot_error is not None:
                print(
                    "RealtimeProfilePlay screenshot_error="
                    + screenshot_error,
                    flush=True,
                )
            print(
                "RealtimeProfilePlay "
                "result_frame="
                f"{screenshot_path.name if save_screenshot and screenshot_error is None else 'none'} "
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
                    run_context=live_run,
                )
        if stats.stopped:
            print("[任务][实时演奏][结束][INFO] 用户已停止任务", flush=True)
            return True
        success = not stats.aborted_for_life and not stats.cleanup_failed
        if params.get("require_completion"):
            success = success and stats.completed
        if not success:
            if (
                run_mode in {"calibration-rehearsal", "calibration-formal"}
                and not stats.cleanup_failed
            ):
                print(
                    "RealtimeProfilePlay calibration_round_retry=true "
                    f"reason={stats.terminal_reason or '实时演奏引擎未完成'}",
                    flush=True,
                )
                return True
            reason = stats.terminal_reason or "实时演奏引擎未完成"
            record_failure_reason(reason)
            print(f"[任务][实时演奏][演奏][ERROR] {reason}", flush=True)
        return success


@AgentServer.custom_action("RealtimeLifeSafetyAbortCheck")
class RealtimeLifeSafetyAbortCheck(CustomAction):
    """Route a protected abort to StopTask while ordinary failures may recover."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        return _LAST_LIFE_SAFETY_ABORT
