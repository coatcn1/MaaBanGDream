from __future__ import annotations

import json
import time
import traceback
from datetime import datetime

import cv2

try:
    from ..foreground_guard import require_game_foreground
except ImportError:  # AgentServer imports realtime as a top-level package.
    from foreground_guard import require_game_foreground

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
from .touch_planner import RealtimePlanner
from .runtime_options import debug_enabled


_LAST_LIFE_SAFETY_ABORT = False


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
        "song_id": str(song_id),
        "survived": not stats.life_depleted,
        "completed": bool(stats.completed),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def collect_result(
    controller,
    stopping,
    *,
    attempts: int = 30,
    interval_seconds: float = 1.5,
    parser: ResultParser | None = None,
    sleeper=time.sleep,
    before_input=lambda: None,
) -> tuple[LiveResult, object]:
    """Press BACK one step at a time until a valid result panel is visible."""
    parser = parser or ResultParser()
    for attempt in range(attempts + 1):
        if stopping():
            raise InterruptedError("任务停止，取消结算读取")
        image = controller.post_screencap().wait().get()
        try:
            return parser.parse(image), image
        except ValueError:
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


def resolve_profile(context: Context, params: dict, *, controller=None):
    controller = controller or context.tasker.controller
    image = controller.post_screencap().wait().get()
    signature = EnvironmentSignature(
        frame_resolution(image),
        int(params.get("dpi", 240)),
        int(params.get("game_fps", 60)),
        str(params.get("render_quality", "standard")),
        float(params.get("note_speed", 2.0)),
    )
    return RealtimeProfileStore(PROJECT_ROOT / "profiles").resolve_latest(
        difficulty=str(params.get("difficulty", "Easy")),
        current_signature=signature,
    )


@AgentServer.custom_action("RealtimeProfileCheck")
class RealtimeProfileCheck(CustomAction):
    """Refuse to start a live before its accepted Profile is available."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            if context.tasker.stopping:
                return False
            params = json.loads(argv.custom_action_param or "{}")
            settings = resolve_profile(context, params)
            print(f"RealtimeProfileCheck profile={settings.profile_path.name}", flush=True)
            return not context.tasker.stopping
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
            traceback.print_exc()
            print(f"RealtimeProfilePlay failed={type(exc).__name__}: {exc}", flush=True)
            return False

    def _run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        global _LAST_LIFE_SAFETY_ABORT
        _LAST_LIFE_SAFETY_ABORT = False
        params = json.loads(argv.custom_action_param or "{}")
        if context.tasker.stopping:
            return False
        controller = context.tasker.controller
        require_profile = bool(params.get("require_profile", True))
        settings = (
            resolve_profile(context, params, controller=controller)
            if require_profile else None
        )
        if context.tasker.stopping:
            return False
        target_fps = settings.target_fps if settings else int(params.get("target_fps", 60))
        timing_offset_ms = (
            settings.timing_offset_ms if settings else int(params.get("timing_offset_ms", 0))
        )
        mode = f"profile={settings.profile_path.name}" if settings else "mode=rehearsal-defaults"
        print(f"RealtimeProfilePlay {mode}", flush=True)
        recorder = (
            RealtimeDebugRecorder(PROJECT_ROOT / "debug" / "recordings")
            if (params.get("debug_recording") or debug_enabled()) else None
        )
        if recorder is not None:
            print(f"RealtimeProfilePlay debug={recorder.output_dir}", flush=True)
        touch = ControllerTouchDispatcher(
            controller,
            lambda: context.tasker.stopping,
            before_input=lambda: require_game_foreground(controller),
        )
        engine = RealtimeEngine(
            NoteDetector(),
            RealtimePlanner(
                judgement_y=565,
                timing_offset_ms=timing_offset_ms,
                rescue_first_visible=True,
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
        )
        continue_after_depleted = bool(params.get("continue_after_life_depleted", False))
        # Required-profile play is formal play (including challenge mode).
        # Calibration and rehearsal use no required profile and ignore this option.
        use_life_safety = bool(params.get("use_life_safety", require_profile))
        runtime_options = RealtimeProfileStore(PROJECT_ROOT / "profiles").runtime_options()
        life_threshold = (
            int(runtime_options["life_exit_threshold"])
            if use_life_safety and runtime_options["life_safety_enabled"] else None
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
            f"life_depleted={stats.life_depleted} completed={stats.completed}",
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
            suggestion = adjusted_timing_offset(timing_offset_ms, result_data)
            report = output / f"realtime-result-{stamp}.json"
            report.write_text(json.dumps({
                **result_data.to_dict(),
                "current_timing_offset_ms": timing_offset_ms,
                "suggested_timing_offset_ms": suggestion,
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(
                f"RealtimeProfilePlay result_frame={path.name} "
                f"perfect={result_data.perfect} great={result_data.great} "
                f"good={result_data.good} bad={result_data.bad} miss={result_data.miss} "
                f"fast={result_data.fast} slow={result_data.slow} "
                f"timing_offset={timing_offset_ms}->{suggestion}",
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
                    timing_offset_ms=timing_offset_ms,
                    song_id=current_song_id(),
                )
        success = not stats.stopped and not stats.aborted_for_life
        if params.get("require_completion"):
            success = success and stats.completed
        return success


@AgentServer.custom_action("RealtimeLifeSafetyAbortCheck")
class RealtimeLifeSafetyAbortCheck(CustomAction):
    """Route a protected abort to StopTask while ordinary failures may recover."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        return _LAST_LIFE_SAFETY_ABORT
