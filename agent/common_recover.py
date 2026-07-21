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

        for restart in range(restart_limit + 1):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                image = context.tasker.controller.cached_image
                result = context.run_recognition(home_node, image)
                if result and result.hit:
                    return True
                context.tasker.controller.post_click_key(4).wait()
                time.sleep(interval)
            if restart < restart_limit:
                context.tasker.controller.post_stop_app(package).wait()
                context.tasker.controller.post_start_app(package).wait()
                time.sleep(5)
        return False
