from __future__ import annotations

import json
from types import SimpleNamespace

from agent import common_recover


class Job:
    def __init__(self, value=None):
        self.value = value

    def wait(self):
        return self

    def get(self):
        return self.value


class Controller:
    def __init__(self, *, running: bool, foregrounds: list[str]):
        self.running = running
        self.foregrounds = iter(foregrounds)
        self.starts = []
        self.keys = []
        self.captures = 0
        self.cached_image = object()

    def post_shell(self, command, _timeout=20000):
        if command.startswith("pidof "):
            return Job("4321" if self.running else "")
        foreground = next(self.foregrounds)
        return Job(
            f"mCurrentFocus=Window{{123 u0 {foreground}/.MainActivity}}"
        )

    def post_start_app(self, package):
        self.starts.append(package)
        return Job()

    def post_screencap(self):
        self.captures += 1
        return Job(object())

    def post_click_key(self, key):
        self.keys.append(key)
        return Job()


class Context:
    def __init__(self, controller):
        self.tasker = SimpleNamespace(
            stopping=False,
            controller=controller,
        )
        self.refreshes = 0

    def run_task(self, node):
        assert node == "CommonRefreshScreen"
        self.refreshes += 1
        return SimpleNamespace(status=SimpleNamespace(succeeded=True))

    def run_recognition(self, node, _image):
        return SimpleNamespace(
            hit=node == "HomeMarker",
            box=None,
        )


def argv(**params):
    return SimpleNamespace(custom_action_param=json.dumps(params))


def test_absent_process_is_started_before_home_detection(monkeypatch):
    controller = Controller(
        running=False,
        foregrounds=["com.android.launcher3", "com.bilibili.star.bili"],
    )
    context = Context(controller)
    monkeypatch.setattr(common_recover.time, "monotonic", lambda: 0)

    assert common_recover.CommonRecover().run(
        context,
        argv(escape_timeout_ms=1, startup_grace_ms=0),
    )
    assert controller.starts == ["com.bilibili.star.bili"]
    assert controller.keys == []
    assert context.refreshes == 1


def test_running_game_on_launcher_is_brought_to_foreground(monkeypatch):
    controller = Controller(
        running=True,
        foregrounds=["com.android.launcher3", "com.bilibili.star.bili"],
    )
    context = Context(controller)
    monkeypatch.setattr(common_recover.time, "monotonic", lambda: 0)

    assert common_recover.CommonRecover().run(
        context,
        argv(escape_timeout_ms=1, startup_grace_ms=0),
    )
    assert controller.starts == ["com.bilibili.star.bili"]
    assert controller.keys == []
