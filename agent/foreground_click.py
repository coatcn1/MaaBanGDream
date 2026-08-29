from __future__ import annotations

import json
import time

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


def _wait_unless_stopping(context: Context, seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        if context.tasker.stopping:
            return False
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    return not context.tasker.stopping


@AgentServer.custom_action("ForegroundClick")
class ForegroundClick(CustomAction):
    """Preserve Pipeline click targeting while blocking input to other apps."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            return self._run(context, argv)
        except Exception as exc:
            print(
                f"ForegroundClick failed node={argv.node_name}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            return False

    def _run(self, context: Context, argv: CustomAction.RunArg) -> bool:
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
        params = json.loads(
            getattr(argv, "custom_action_param", None) or "{}"
        ) or {}
        confirm_node = params.get("confirm_absent_node")
        max_attempts = max(1, int(params.get("confirm_attempts", 1)))
        interval = max(0, int(params.get("confirm_interval_ms", 750))) / 1000
        box = argv.box
        for attempt in range(1, max_attempts + 1):
            x, y = _click_point(box)
            controller.post_click(x, y).wait()
            if not confirm_node:
                return True
            if not _wait_unless_stopping(context, interval):
                return True
            controller = context.tasker.controller
            image = controller.post_screencap().wait().get()
            result = context.run_recognition(str(confirm_node), image)
            if not result or not result.hit:
                return True
            if attempt == max_attempts:
                print(
                    "ForegroundClick confirmation_failed "
                    f"node={argv.node_name} marker={confirm_node} "
                    f"attempts={max_attempts}",
                    flush=True,
                )
                return False
            if context.tasker.stopping:
                return True
            try:
                require_game_foreground(controller)
            except Exception as exc:
                print(f"ForegroundClick retry_blocked node={argv.node_name}: {exc}", flush=True)
                return False
            if result.box:
                box = result.box
            print(
                f"ForegroundClick retry node={argv.node_name} "
                f"attempt={attempt + 1}/{max_attempts}",
                flush=True,
            )
        return False
