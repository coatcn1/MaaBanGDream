from __future__ import annotations

import json
import time
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction


def _params(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        return json.loads(raw)
    return {}


def _wait_unless_stopping(context: Context, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if context.tasker.stopping:
            return False
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    return not context.tasker.stopping


@AgentServer.custom_action("CommonRecover")
class CommonRecover(CustomAction):
    """Recover an unknown page with BACK, then bounded app restarts."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        params = _params(argv.custom_action_param)
        home_node = str(params.get("home_node", "HomeMarker"))
        interval = int(params.get("escape_interval_ms", 1500)) / 1000
        timeout = int(params.get("escape_timeout_ms", 30000)) / 1000
        package = str(params.get("package", "com.bilibili.star.bili"))
        restart_limit = int(params.get("restart_limit", 2))
        restart_wait = int(params.get("restart_wait_ms", 5000)) / 1000
        click_nodes = [str(node) for node in params.get("click_nodes", [])]
        controller = context.tasker.controller

        for restart in range(restart_limit + 1):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if context.tasker.stopping:
                    return False
                image = controller.post_screencap().wait().get()
                if context.tasker.stopping:
                    return False
                result = context.run_recognition(home_node, image)
                if result and result.hit:
                    return True
                clicked = False
                for node in click_nodes:
                    result = context.run_recognition(node, image)
                    if not result or not result.hit or not result.box:
                        continue
                    if context.tasker.stopping:
                        return False
                    box = result.box
                    controller.post_click(
                        box.x + box.w // 2,
                        box.y + box.h // 2,
                    ).wait()
                    clicked = True
                    break
                if not clicked:
                    if context.tasker.stopping:
                        return False
                    controller.post_click_key(4).wait()
                if not _wait_unless_stopping(context, interval):
                    return False
            if restart < restart_limit:
                if context.tasker.stopping:
                    return False
                controller.post_stop_app(package).wait()
                if context.tasker.stopping:
                    return False
                controller.post_start_app(package).wait()
                if not _wait_unless_stopping(context, restart_wait):
                    return False
        return False
