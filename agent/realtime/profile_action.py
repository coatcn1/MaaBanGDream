from __future__ import annotations

import json
import re
import ctypes
from pathlib import Path

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from maa.define import MaaControllerHandle, MaaCtrlId
from maa.library import Library

from .profile_store import EnvironmentSignature, RealtimeProfileStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def ensure_post_shell_binding() -> None:
    """Work around MaaFw 5.10.2 omitting MaaControllerPostShell argtypes."""
    function = Library.framework().MaaControllerPostShell
    function.restype = MaaCtrlId
    function.argtypes = [MaaControllerHandle, ctypes.c_char_p, ctypes.c_int64]


def parse_density(output: str) -> int:
    overrides = re.findall(r"Override density:\s*(\d+)", output)
    if overrides:
        return int(overrides[-1])
    physical = re.findall(r"Physical density:\s*(\d+)", output)
    if physical:
        return int(physical[-1])
    plain = re.search(r"\b(\d{2,4})\b", output)
    if plain:
        return int(plain.group(1))
    raise ValueError(f"无法解析设备 DPI: {output!r}")


def build_draft_payload(
    params: dict,
    resolution: tuple[int, int],
    density_output: str,
) -> dict:
    signature = EnvironmentSignature(
        resolution=resolution,
        dpi=parse_density(density_output),
        game_fps=int(params.get("game_fps", 60)),
        render_quality=str(params.get("render_quality", "standard")),
        note_speed=float(params.get("note_speed", 2.0)),
    )
    signature.validate()
    return {
        "difficulty": str(params.get("difficulty", "Easy")),
        "accepted": False,
        "environment": signature.to_mapping(),
        "settings": {
            "target_fps": int(params.get("target_fps", 60)),
            "timing_offset_ms": int(params.get("timing_offset_ms", 0)),
            "frame_timeout_ms": 150,
            "playfield_timeout_ms": 1500,
        },
    }


@AgentServer.custom_action("RealtimeProfileDraft")
class RealtimeProfileDraft(CustomAction):
    """Create an unaccepted local profile from controller facts and user settings."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            return self._run(context, argv)
        except Exception as exc:
            print(
                f"RealtimeProfileDraft failed={type(exc).__name__}: {exc}",
                flush=True,
            )
            return False

    def _run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        if context.tasker.stopping:
            return False
        params = json.loads(argv.custom_action_param or "{}")
        controller = context.tasker.controller
        controller.post_screencap().wait()
        if context.tasker.stopping:
            return False
        ensure_post_shell_binding()
        density = controller.post_shell("wm density").wait().get()
        if context.tasker.stopping:
            return False
        payload = build_draft_payload(params, controller.resolution, density)
        path = RealtimeProfileStore(PROJECT_ROOT / "profiles").write(payload)
        print(f"RealtimeProfileDraft path={path.name} accepted=false", flush=True)
        return True
