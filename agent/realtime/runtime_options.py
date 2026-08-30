from __future__ import annotations

import json

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction


_DEBUG_ENABLED = False
_DIAGNOSTIC_TRACE_ENABLED = True
_CALIBRATION_DIFFICULTY = "Easy"
_CALIBRATION_SONG_MODE = "current"
_CALIBRATION_RESUME_MODE = "auto"


def debug_enabled() -> bool:
    return _DEBUG_ENABLED


def diagnostic_trace_enabled() -> bool:
    return _DIAGNOSTIC_TRACE_ENABLED


def calibration_difficulty() -> str:
    return _CALIBRATION_DIFFICULTY


def calibration_song_mode() -> str:
    return _CALIBRATION_SONG_MODE


def calibration_resume_mode() -> str:
    return _CALIBRATION_RESUME_MODE


@AgentServer.custom_action("RealtimeRuntimeOptions")
class RealtimeRuntimeOptions(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        global _DEBUG_ENABLED, _DIAGNOSTIC_TRACE_ENABLED
        global _CALIBRATION_DIFFICULTY
        global _CALIBRATION_SONG_MODE, _CALIBRATION_RESUME_MODE
        if context.tasker.stopping:
            return True
        params = json.loads(argv.custom_action_param or "{}")
        if "debug_recording" in params:
            _DEBUG_ENABLED = bool(params["debug_recording"])
        if "diagnostic_trace" in params:
            _DIAGNOSTIC_TRACE_ENABLED = bool(params["diagnostic_trace"])
        if "difficulty" in params:
            _CALIBRATION_DIFFICULTY = str(params["difficulty"])
        if "calibration_song_mode" in params:
            value = str(params["calibration_song_mode"])
            if value not in {"current", "random"}:
                raise ValueError(f"invalid calibration_song_mode: {value}")
            _CALIBRATION_SONG_MODE = value
        if "calibration_resume_mode" in params:
            value = str(params["calibration_resume_mode"])
            if value not in {"auto", "restart"}:
                raise ValueError(f"invalid calibration_resume_mode: {value}")
            _CALIBRATION_RESUME_MODE = value
        return True
