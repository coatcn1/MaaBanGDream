from __future__ import annotations

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    from .foreground_guard import require_game_foreground
except ImportError:  # AgentServer loads modules from the agent directory.
    from foreground_guard import require_game_foreground


def _click_point(box) -> tuple[int, int]:
    # MaaFramework has already applied target and pipeline_override when it
    # constructs argv.box. Reading the source JSON here would discard the
    # user's resolved override (for example Expert -> Easy).
    return box.x + box.w // 2, box.y + box.h // 2


@AgentServer.custom_action("ForegroundClick")
class ForegroundClick(CustomAction):
    """Preserve Pipeline click targeting while blocking input to other apps."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        if context.tasker.stopping:
            return True
        controller = context.tasker.controller
        try:
            require_game_foreground(controller)
        except Exception as exc:
            print(f"ForegroundClick blocked node={argv.node_name}: {exc}", flush=True)
            return False
        if context.tasker.stopping:
            return True
        x, y = _click_point(argv.box)
        controller.post_click(x, y).wait()
        return True
