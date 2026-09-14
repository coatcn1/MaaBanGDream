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

try:
    from .live_visual_gate import MODE_TOGGLE_POINT, live_performance_mode_is_off
except ImportError:  # AgentServer 以顶层 realtime 包加载本模块时同样成立。
    from live_visual_gate import MODE_TOGGLE_POINT, live_performance_mode_is_off


def formal_live_mode_is_off(image) -> bool:
    return live_performance_mode_is_off(image)


def cut_in_is_checked(image) -> bool:
    hsv = cv2.cvtColor(image[630:670, 480:520], cv2.COLOR_BGR2HSV)
    pink = (hsv[..., 1] >= 80) & (hsv[..., 2] >= 130)
    return float(np.count_nonzero(pink) / pink.size) > .12


def _wait(context: Context, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if context.tasker.stopping:
            return True
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
                    return True
                image = controller.post_screencap().wait().get()
                auto = context.run_recognition("AutoLiveEnabled", image)
                if auto and auto.hit and auto.box:
                    if context.tasker.stopping:
                        return True
                    box = auto.box
                    require_game_foreground(controller)
                    print(
                        "RealtimeFormalPreflight action=disable_auto_live "
                        f"target=({box.x + box.w // 2},{box.y + box.h // 2})",
                        flush=True,
                    )
                    controller.post_click(box.x + box.w // 2, box.y + box.h // 2).wait()
                    if not _wait(context, 1):
                        return False
                    continue
                # 3D 演出时 Cut-in 复选框尚未出现，同一区域显示的是成员头像；
                # 必须先把演出模式切到 OFF，再读取随后出现的 Cut-in 复选框。
                if not formal_live_mode_is_off(image):
                    require_game_foreground(controller)
                    print(
                        "RealtimeFormalPreflight action=cycle_performance_mode "
                        f"target={MODE_TOGGLE_POINT}",
                        flush=True,
                    )
                    controller.post_click(*MODE_TOGGLE_POINT).wait()
                    if not _wait(context, .5):
                        return False
                    continue
                if cut_in_is_checked(image):
                    require_game_foreground(controller)
                    print(
                        "RealtimeFormalPreflight action=disable_3d_cut_in "
                        "target=(500,650)",
                        flush=True,
                    )
                    controller.post_click(500, 650).wait()
                    if not _wait(context, 1):
                        return False
                    continue
                print("RealtimeFormalPreflight completed=true", flush=True)
                return True
            raise RuntimeError("无法在正式演奏前关闭自动演出和演出显示效果")
        except Exception as exc:
            traceback.print_exc()
            print(f"RealtimeFormalPreflight failed={type(exc).__name__}: {exc}", flush=True)
            return False
