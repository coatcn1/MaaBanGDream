from __future__ import annotations

import json
import traceback

import numpy as np

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from .controller_touch import ControllerTouchDispatcher
try:
    from ..foreground_guard import require_game_foreground
except ImportError:  # AgentServer imports realtime as a top-level package.
    from foreground_guard import require_game_foreground
from .engine import RealtimeEngine
from .life_monitor import LifeDetector, LifeGuard
from .note_detector import NoteDetector
from .profile_action import parse_density
from .profile_store import EnvironmentSignature
from .touch_planner import RealtimePlanner


SUPPORTED_SIGNATURE = EnvironmentSignature((1280, 720), 240, 60, "standard", 2.0)


def frame_resolution(image: np.ndarray) -> tuple[int, int]:
    if image is None or image.ndim < 2:
        raise ValueError("截图数据无效，无法确认排练分辨率")
    return int(image.shape[1]), int(image.shape[0])


def validate_rehearsal_environment(
    resolution: tuple[int, int], density_output: str, params: dict
) -> EnvironmentSignature:
    current = EnvironmentSignature(
        resolution,
        parse_density(density_output),
        int(params.get("game_fps", 60)),
        str(params.get("render_quality", "standard")),
        float(params.get("note_speed", 2.0)),
    )
    current.validate()
    if current != SUPPORTED_SIGNATURE:
        raise ValueError(
            f"机器人排练环境不匹配: expected={SUPPORTED_SIGNATURE.to_mapping()} "
            f"current={current.to_mapping()}"
        )
    return current


@AgentServer.custom_action("RealtimeEasyRehearsal")
class RealtimeEasyRehearsal(CustomAction):
    """Run the first bounded robot rehearsal after a strict environment gate."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            return self._run(context, argv)
        except Exception as exc:
            traceback.print_exc()
            print(
                f"RealtimeEasyRehearsal failed={type(exc).__name__}: {exc}",
                flush=True,
            )
            return False

    def _run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        params = json.loads(argv.custom_action_param or "{}")
        if context.tasker.stopping:
            return False
        controller = context.tasker.controller
        image = controller.post_screencap().wait().get()
        density = f"Override density: {int(params.get('dpi', 240))}"
        validate_rehearsal_environment(frame_resolution(image), density, params)
        print("RealtimeEasyRehearsal stage=environment_ok", flush=True)
        if context.tasker.stopping:
            return False
        touch = ControllerTouchDispatcher(
            controller,
            lambda: context.tasker.stopping,
            before_input=lambda: require_game_foreground(controller),
        )
        engine = RealtimeEngine(
            NoteDetector(),
            RealtimePlanner(
                judgement_y=565,
                timing_offset_ms=int(params.get("timing_offset_ms", 0)),
                # Bright skill notes can enter the colour range for only one
                # fresh frame near the judgement line. The detector has
                # already validated their colour, geometry, and lane, so a
                # first-visible rescue is safe and prevents silent misses.
                rescue_first_visible=True,
            ),
            touch,
            life_detector=LifeDetector(),
            life_guard=LifeGuard(),
        )
        print("RealtimeEasyRehearsal stage=engine_start", flush=True)
        stats = engine.run(
            lambda: controller.post_screencap().wait().get(),
            lambda: context.tasker.stopping,
            duration_seconds=float(params.get("duration_seconds", 30)),
            target_fps=60,
        )
        print(
            "RealtimeEasyRehearsal "
            f"frames={stats.processed_frames} actions={stats.dispatched_actions} "
            f"stopped={stats.stopped} life_abort={stats.aborted_for_life}",
            flush=True,
        )
        return not stats.stopped and not stats.aborted_for_life
