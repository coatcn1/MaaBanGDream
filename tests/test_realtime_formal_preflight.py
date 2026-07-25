from types import SimpleNamespace

import numpy as np

from agent.realtime import formal_preflight


class Job:
    def __init__(self, value=None):
        self.value = value

    def wait(self):
        return self

    def get(self):
        return self.value


class Controller:
    def __init__(self, foreground):
        self.foreground = foreground
        self.clicks = []

    def post_screencap(self):
        return Job(np.zeros((720, 1280, 3), dtype=np.uint8))

    def post_shell(self, _command, _timeout=20000):
        return Job(f"mCurrentFocus=Window{{123 u0 {self.foreground}/.MainActivity}}")

    def post_click(self, x, y):
        self.clicks.append((x, y))
        return Job()


class Context:
    def __init__(self, foreground):
        self.tasker = SimpleNamespace(
            stopping=False,
            controller=Controller(foreground),
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
