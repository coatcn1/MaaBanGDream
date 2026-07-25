from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    from .foreground_guard import require_game_foreground
except ImportError:  # AgentServer loads modules from the agent directory.
    from foreground_guard import require_game_foreground


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def _click_targets() -> dict[str, list[int]]:
    targets: dict[str, list[int]] = {}
    for path in (PROJECT_ROOT / "resource" / "pipeline").glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for name, node in payload.items():
            target = node.get("target")
            if (
                node.get("action") == "Custom"
                and node.get("custom_action") == "ForegroundClick"
                and isinstance(target, list)
            ):
                targets[name] = [int(value) for value in target]
    return targets


def _click_point(node_name: str, box) -> tuple[int, int]:
    target = _click_targets().get(node_name)
    if target and len(target) == 2:
        return target[0], target[1]
    if target and len(target) == 4:
        return target[0] + target[2] // 2, target[1] + target[3] // 2
    return box.x + box.w // 2, box.y + box.h // 2


@AgentServer.custom_action("ForegroundClick")
class ForegroundClick(CustomAction):
    """Preserve Pipeline click targeting while blocking input to other apps."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        if context.tasker.stopping:
            return False
        controller = context.tasker.controller
        try:
            require_game_foreground(controller)
        except Exception as exc:
            print(f"ForegroundClick blocked node={argv.node_name}: {exc}", flush=True)
            return False
        if context.tasker.stopping:
            return False
        x, y = _click_point(argv.node_name, argv.box)
        controller.post_click(x, y).wait()
        return not context.tasker.stopping
