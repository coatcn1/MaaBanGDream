from __future__ import annotations

import json
import time
import traceback
from datetime import datetime

import cv2

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from .controller_touch import ControllerTouchDispatcher
from .engine import RealtimeEngine
from .life_monitor import LifeDetector, LifeGuard, PlayfieldCompletionGuard
from .note_detector import NoteDetector
from .profile_action import PROJECT_ROOT
from .profile_store import EnvironmentSignature, RealtimeProfileStore
from .rehearsal_action import frame_resolution
from .result_parser import LiveResult, ResultParser, adjusted_timing_offset
from .touch_planner import RealtimePlanner


def collect_result(
    controller,
    stopping,
    *,
    attempts: int = 12,
    interval_seconds: float = 1.0,
    parser: ResultParser | None = None,
    sleeper=time.sleep,
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
        controller.post_click_key(4).wait()
        deadline = time.monotonic() + interval_seconds
        while time.monotonic() < deadline:
            if stopping():
                raise InterruptedError("任务停止，取消结算读取")
            sleeper(min(.1, max(0.0, deadline - time.monotonic())))
    raise ValueError("连续按 BACK 后仍未识别到有效结算画面")


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
        params = json.loads(argv.custom_action_param or "{}")
        if context.tasker.stopping:
            return False
        controller = context.tasker.controller
        image = controller.post_screencap().wait().get()
        signature = EnvironmentSignature(
            frame_resolution(image),
            int(params.get("dpi", 240)),
            int(params.get("game_fps", 60)),
            str(params.get("render_quality", "standard")),
            float(params.get("note_speed", 2.0)),
        )
        settings = RealtimeProfileStore(PROJECT_ROOT / "profiles").resolve_latest(
            difficulty=str(params.get("difficulty", "Easy")),
            current_signature=signature,
        )
        if context.tasker.stopping:
            return False
        print(f"RealtimeProfilePlay profile={settings.profile_path.name}", flush=True)
        touch = ControllerTouchDispatcher(controller, lambda: context.tasker.stopping)
        engine = RealtimeEngine(
            NoteDetector(),
            RealtimePlanner(
                judgement_y=565,
                timing_offset_ms=settings.timing_offset_ms,
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
        )
        stats = engine.run(
            lambda: controller.post_screencap().wait().get(),
            lambda: context.tasker.stopping,
            duration_seconds=float(params.get("duration_seconds", 30)),
            target_fps=settings.target_fps,
        )
        print(
            "RealtimeProfilePlay "
            f"frames={stats.processed_frames} actions={stats.dispatched_actions} "
            f"stopped={stats.stopped} life_abort={stats.aborted_for_life} "
            f"completed={stats.completed}",
            flush=True,
        )
        if stats.completed and params.get("save_result_frame"):
            result_data, result = collect_result(
                controller,
                lambda: context.tasker.stopping,
                attempts=int(params.get("result_back_attempts", 12)),
                interval_seconds=float(params.get("result_back_interval_seconds", 1.0)),
            )
            output = PROJECT_ROOT / "screencap"
            output.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = output / f"realtime-result-{stamp}.png"
            if not cv2.imwrite(str(path), result):
                raise OSError(f"无法保存结算截图: {path}")
            suggestion = adjusted_timing_offset(
                settings.timing_offset_ms, result_data
            )
            report = output / f"realtime-result-{stamp}.json"
            report.write_text(json.dumps({
                **result_data.to_dict(),
                "current_timing_offset_ms": settings.timing_offset_ms,
                "suggested_timing_offset_ms": suggestion,
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(
                f"RealtimeProfilePlay result_frame={path.name} "
                f"perfect={result_data.perfect} great={result_data.great} "
                f"good={result_data.good} bad={result_data.bad} miss={result_data.miss} "
                f"fast={result_data.fast} slow={result_data.slow} "
                f"timing_offset={settings.timing_offset_ms}->{suggestion}",
                flush=True,
            )
        success = not stats.stopped and not stats.aborted_for_life
        if params.get("require_completion"):
            success = success and stats.completed
        return success
