from __future__ import annotations

import json

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction


_DEBUG_ENABLED = False
_CALIBRATION_DIFFICULTY = "Easy"


def debug_enabled() -> bool:
    return _DEBUG_ENABLED


def calibration_difficulty() -> str:
    return _CALIBRATION_DIFFICULTY


@AgentServer.custom_action("RealtimeRuntimeOptions")
class RealtimeRuntimeOptions(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        global _DEBUG_ENABLED, _CALIBRATION_DIFFICULTY
        if context.tasker.stopping:
            return False
        params = json.loads(argv.custom_action_param or "{}")
        if "debug_recording" in params:
            _DEBUG_ENABLED = bool(params["debug_recording"])
        if "difficulty" in params:
            _CALIBRATION_DIFFICULTY = str(params["difficulty"])
        return True
