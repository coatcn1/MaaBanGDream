from types import SimpleNamespace

import numpy as np

from agent.realtime import formal_preflight, live_visual_gate


class Job:
    def __init__(self, value=None):
        self.value = value

    def wait(self):
        return self

    def get(self):
        return self.value


class Controller:
    def __init__(self, foreground, frames=None):
        self.foreground = foreground
        self.clicks = []
        self.frames = list(frames or [np.zeros((720, 1280, 3), dtype=np.uint8)])

    def post_screencap(self):
        frame = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        return Job(frame)

    def post_shell(self, _command, _timeout=20000):
        return Job(f"mCurrentFocus=Window{{123 u0 {self.foreground}/.MainActivity}}")

    def post_click(self, x, y):
        self.clicks.append((x, y))
        return Job()


class Context:
    def __init__(self, foreground, frames=None):
        self.tasker = SimpleNamespace(
            stopping=False,
            controller=Controller(foreground, frames),
        )

    def run_recognition(self, _node, _image):
        return SimpleNamespace(hit=False, box=None)


def test_foreign_foreground_blocks_formal_preflight_click(monkeypatch):
    context = Context("com.bilibili.azurlane")
    monkeypatch.setattr(formal_preflight, "cut_in_is_checked", lambda _image: True)

    assert not formal_preflight.RealtimeFormalPreflight().run(
        context,
        SimpleNamespace(custom_action_param="{}"),
    )
    assert context.tasker.controller.clicks == []


def test_3d_mode_is_disabled_before_cut_in_checkbox(monkeypatch):
    mode_3d = np.zeros((720, 1280, 3), dtype=np.uint8)
    x0, x1, y0, y1 = live_visual_gate.MODE_TAG_REGION
    mode_3d[y0:y1, x0:x1] = (0, 0, 255)
    # 3D 演出状态下复选框位置显示粉色成员头像，不能把头像当作 Cut-in。
    mode_3d[630:670, 480:520] = (180, 80, 255)
    mode_mv = np.zeros((720, 1280, 3), dtype=np.uint8)
    mode_mv[y0:y1, x0:x1] = (180, 80, 255)
    mode_off_checked = np.zeros((720, 1280, 3), dtype=np.uint8)
    mode_off_checked[630:670, 480:520] = (180, 80, 255)
    mode_off_unchecked = np.zeros((720, 1280, 3), dtype=np.uint8)

    context = Context(
        "com.bilibili.star.bili",
        frames=[mode_3d, mode_mv, mode_off_checked, mode_off_unchecked],
    )
    monkeypatch.setattr(formal_preflight, "_wait", lambda *_args: True)

    assert formal_preflight.RealtimeFormalPreflight().run(
        context,
        SimpleNamespace(custom_action_param="{}"),
    )
    assert context.tasker.controller.clicks == [
        live_visual_gate.MODE_TOGGLE_POINT,
        live_visual_gate.MODE_TOGGLE_POINT,
        (500, 650),
    ]
