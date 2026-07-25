from __future__ import annotations

import time
import traceback

import cv2
import numpy as np

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    from ..foreground_guard import require_game_foreground
except ImportError:  # AgentServer imports realtime as a top-level package.
    from foreground_guard import require_game_foreground


def formal_live_mode_is_off(image) -> bool:
    hsv = cv2.cvtColor(image[615:700, 165:235], cv2.COLOR_BGR2HSV)
    saturated = np.count_nonzero((hsv[..., 1] >= 90) & (hsv[..., 2] >= 130))
    return saturated < 45


def cut_in_is_checked(image) -> bool:
    hsv = cv2.cvtColor(image[630:670, 480:520], cv2.COLOR_BGR2HSV)
    pink = (hsv[..., 1] >= 80) & (hsv[..., 2] >= 130)
    return float(np.count_nonzero(pink) / pink.size) > .12


def _wait(context: Context, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if context.tasker.stopping:
            return False
        time.sleep(min(.1, deadline - time.monotonic()))
    return True


@AgentServer.custom_action("RealtimeFormalPreflight")
class RealtimeFormalPreflight(CustomAction):
    """Idempotently disable Auto Live, 3D Cut-in, and 3D/MV visuals."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            controller = context.tasker.controller
            for _ in range(8):
                if context.tasker.stopping:
                    return False
                image = controller.post_screencap().wait().get()
                auto = context.run_recognition("AutoLiveEnabled", image)
                if auto and auto.hit and auto.box:
                    if context.tasker.stopping:
                        return False
                    box = auto.box
                    require_game_foreground(controller)
                    controller.post_click(box.x + box.w // 2, box.y + box.h // 2).wait()
                    if not _wait(context, 1):
                        return False
                    continue
                if cut_in_is_checked(image):
                    require_game_foreground(controller)
                    controller.post_click(500, 650).wait()
                    if not _wait(context, 1):
                        return False
                    continue
                if not formal_live_mode_is_off(image):
                    require_game_foreground(controller)
                    controller.post_click(200, 655).wait()
                    if not _wait(context, .5):
                        return False
                    continue
                return not context.tasker.stopping
            raise RuntimeError("无法在正式演奏前关闭自动演出和演出显示效果")
        except Exception as exc:
            traceback.print_exc()
            print(f"RealtimeFormalPreflight failed={type(exc).__name__}: {exc}", flush=True)
            return False
