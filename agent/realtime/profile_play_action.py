from __future__ import annotations

import json
import traceback

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from .controller_touch import ControllerTouchDispatcher
from .engine import RealtimeEngine
from .life_monitor import LifeDetector, LifeGuard
from .note_detector import NoteDetector
from .profile_action import PROJECT_ROOT
from .profile_store import EnvironmentSignature, RealtimeProfileStore
from .rehearsal_action import frame_resolution
from .touch_planner import RealtimePlanner


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
            f"stopped={stats.stopped} life_abort={stats.aborted_for_life}",
            flush=True,
        )
        return not stats.stopped and not stats.aborted_for_life
