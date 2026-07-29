from __future__ import annotations

import json
from types import SimpleNamespace

from agent import common_recover


class Job:
    def __init__(self, result=None):
        self.result = result

    def wait(self):
        return self

    def get(self):
        return self.result


class Controller:
    def __init__(self, foreground="com.bilibili.star.bili"):
        self.image = object()
        self.foreground = foreground
        self.captures = 0
        self.clicks = []
        self.keys = []
        self.stops = []
        self.starts = []
        self.cached_image = self.image

    def post_shell(self, _command, _timeout=20000):
        foreground = (
            next(self.foreground)
            if hasattr(self.foreground, "__next__")
            else self.foreground
        )
        return Job(f"mCurrentFocus=Window{{123 u0 {foreground}/.MainActivity}}")

    def post_screencap(self):
        self.captures += 1
        image = next(self.image) if hasattr(self.image, "__next__") else self.image
        self.cached_image = image
        return Job(image)

    def post_click(self, x, y):
        self.clicks.append((x, y))
        return Job()

    def post_click_key(self, key):
        self.keys.append(key)
        return Job()

    def post_stop_app(self, package):
        self.stops.append(package)
        return Job()

    def post_start_app(self, package):
        self.starts.append(package)
        return Job()


class Tasker:
    def __init__(self, stopping=False, foreground="com.bilibili.star.bili"):
        self.controller = Controller(foreground)
        self.stopping = stopping


class Context:
    def __init__(self, recognitions=None, *, stopping=False, foreground="com.bilibili.star.bili"):
        self.recognitions = {
            name: iter(results) for name, results in (recognitions or {}).items()
        }
        self.tasker = Tasker(stopping, foreground)
        self.refreshes = 0

    def run_task(self, node):
        assert node == "CommonRefreshScreen"
        self.refreshes += 1
        controller = self.tasker.controller
        controller.cached_image = (
            next(controller.image)
            if hasattr(controller.image, "__next__")
            else controller.image
        )
        return SimpleNamespace(status=SimpleNamespace(succeeded=True))

    def run_recognition(self, node, _image):
        hit = next(self.recognitions.get(node, iter(())), False)
        box = SimpleNamespace(x=10, y=20, w=30, h=40) if hit else None
        return SimpleNamespace(hit=hit, box=box)


def argv(**params):
    return SimpleNamespace(custom_action_param=json.dumps(params))


def test_returns_immediately_when_home_is_visible(monkeypatch):
    context = Context({"HomeMarker": [True]})
    monkeypatch.setattr(common_recover.time, "monotonic", lambda: 0)

    assert common_recover.CommonRecover().run(context, argv(escape_timeout_ms=1))
    assert context.refreshes == 1
    assert context.tasker.controller.captures == 0
    assert context.tasker.controller.keys == []
    assert context.tasker.controller.starts == []


def test_callback_exception_is_converted_to_failure(monkeypatch):
    context = Context()
    monkeypatch.setattr(
        context,
        "run_task",
        lambda _node: (_ for _ in ()).throw(RuntimeError("refresh failed")),
    )

    assert not common_recover.CommonRecover().run(
        context,
        argv(escape_timeout_ms=1),
    )


def test_reacquires_controller_after_nested_refresh(monkeypatch):
    context = Context({"HomeMarker": [True]})
    original_run_task = context.run_task
    first_controller = context.tasker.controller

    def invalidate_and_refresh(node):
        detail = original_run_task(node)
        first_controller.post_shell = lambda *_args: (_ for _ in ()).throw(
            RuntimeError("stale controller")
        )
        context.tasker.controller = Controller()
        return detail

    monkeypatch.setattr(context, "run_task", invalidate_and_refresh)
    monkeypatch.setattr(common_recover.time, "monotonic", lambda: 0)

    assert common_recover.CommonRecover().run(
        context,
        argv(escape_timeout_ms=1),
    )


def test_clicks_safe_node_center_instead_of_back(monkeypatch):
    context = Context(
        {
            "HomeMarker": [False, True],
            "ResultConfirm": [True],
        }
    )
    ticks = iter(range(100))
    monkeypatch.setattr(common_recover.time, "monotonic", lambda: next(ticks) / 1000)
    monkeypatch.setattr(common_recover.time, "sleep", lambda _seconds: None)

    assert common_recover.CommonRecover().run(
        context,
        argv(
            escape_interval_ms=0,
            escape_timeout_ms=20,
            click_nodes=["ResultConfirm"],
        ),
    )
    assert context.tasker.controller.clicks == [(25, 40)]
    assert context.tasker.controller.keys == []


def test_stopping_exits_before_any_controller_operation():
    context = Context(stopping=True)

    assert common_recover.CommonRecover().run(context, argv())
    assert context.tasker.controller.captures == 0
    assert context.tasker.controller.clicks == []
    assert context.tasker.controller.keys == []
    assert context.tasker.controller.stops == []
    assert context.tasker.controller.starts == []


def test_foreign_foreground_is_focused_without_sending_input(monkeypatch):
    context = Context(foreground="com.bilibili.azurlane")
    ticks = iter(range(100))
    monkeypatch.setattr(common_recover.time, "monotonic", lambda: next(ticks) / 1000)
    monkeypatch.setattr(common_recover.time, "sleep", lambda _seconds: None)

    assert not common_recover.CommonRecover().run(context, argv(escape_timeout_ms=20))
    assert context.refreshes == 1
    assert context.tasker.controller.captures == 0
    assert context.tasker.controller.clicks == []
    assert context.tasker.controller.keys == []
    assert context.tasker.controller.stops == []
    assert context.tasker.controller.starts == ["com.bilibili.star.bili"]


def test_failure_path_restarts_only_up_to_limit(monkeypatch):
    context = Context(foreground="test.package")
    ticks = iter(range(1000))
    monkeypatch.setattr(common_recover.time, "monotonic", lambda: next(ticks) / 1000)
    monkeypatch.setattr(common_recover.time, "sleep", lambda _seconds: None)

    result = common_recover.CommonRecover().run(
        context,
        argv(
            escape_interval_ms=0,
            escape_timeout_ms=2,
            restart_wait_ms=0,
            restart_limit=2,
            package="test.package",
        ),
    )

    assert not result
    assert context.tasker.controller.keys
    assert context.tasker.controller.stops == ["test.package", "test.package"]
    assert context.tasker.controller.starts == ["test.package", "test.package"]


def test_startup_grace_waits_without_sending_back(monkeypatch):
    context = Context({"HomeMarker": [False, True]})
    ticks = iter([0, 0, .001, .002, .003, .004, .005])
    monkeypatch.setattr(common_recover.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(common_recover.time, "sleep", lambda _seconds: None)

    assert common_recover.CommonRecover().run(
        context,
        argv(escape_interval_ms=0, escape_timeout_ms=10000, startup_grace_ms=5000),
    )
    assert context.tasker.controller.keys == []


def test_startup_grace_allows_launcher_until_game_reaches_foreground(monkeypatch):
    context = Context({"HomeMarker": [False, True]})
    context.tasker.controller.foreground = iter([
        "com.android.launcher3", "com.android.launcher3",
        "com.bilibili.star.bili", "com.bilibili.star.bili"
    ])
    ticks = iter(value / 1000 for value in range(1000))
    monkeypatch.setattr(common_recover.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(common_recover.time, "sleep", lambda _seconds: None)

    assert common_recover.CommonRecover().run(
        context,
        argv(escape_interval_ms=0, escape_timeout_ms=10000, startup_grace_ms=5000),
    )
    assert context.tasker.controller.keys == []


def test_login_mode_never_sends_back_before_start_is_detected(monkeypatch):
    context = Context({
        "HomeMarker": [False, False, True],
        "LoginScreenMarker": [False, False],
    })
    ticks = iter(value / 1000 for value in range(1000))
    monkeypatch.setattr(common_recover.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(common_recover.time, "sleep", lambda _seconds: None)

    assert common_recover.CommonRecover().run(
        context,
        argv(
            escape_interval_ms=0,
            escape_timeout_ms=100,
            login_start_node="LoginScreenMarker",
            login_start_target=[640, 635],
            escape_after_login_start=True,
        ),
    )
    assert context.tasker.controller.clicks == []
    assert context.tasker.controller.keys == []


def test_cold_start_extends_grace_for_slow_title_screen(monkeypatch):
    context = Context({
        "HomeMarker": [False, False, False, False, True],
        "LoginScreenMarker": [False, False, False, False],
    })
    ticks = iter(range(1000))
    monkeypatch.setattr(common_recover.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(common_recover.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(common_recover, "_prepare_game", lambda *_args: (True, True))

    assert common_recover.CommonRecover().run(
        context,
        argv(
            escape_interval_ms=0,
            escape_timeout_ms=60000,
            startup_grace_ms=12000,
            login_start_node="LoginScreenMarker",
            login_start_target=[640, 635],
            escape_after_login_start=True,
        ),
    )
    assert context.tasker.controller.keys == []


def test_login_mode_clicks_start_before_using_back(monkeypatch):
    context = Context({
        "HomeMarker": [False, False, True],
        "LoginScreenMarker": [True, False],
    })
    ticks = iter(value / 1000 for value in range(1000))
    monkeypatch.setattr(common_recover.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(common_recover.time, "sleep", lambda _seconds: None)

    assert common_recover.CommonRecover().run(
        context,
        argv(
            escape_interval_ms=0,
            escape_timeout_ms=100,
            login_start_node="LoginScreenMarker",
            login_start_target=[640, 635],
            escape_after_login_start=True,
        ),
    )
    assert context.tasker.controller.clicks == [(640, 635)]
    assert context.tasker.controller.keys == [4]


def test_login_menu_marker_gets_multiple_attempts_before_tap_to_start(
    monkeypatch,
):
    context = Context({
        "HomeMarker": [False, False, False, True],
        "LoginScreenMarker": [False, False, True],
        "LoginTap": [True, True],
    })
    ticks = iter(value / 1000 for value in range(1000))
    monkeypatch.setattr(common_recover.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(common_recover.time, "sleep", lambda _seconds: None)

    assert common_recover.CommonRecover().run(
        context,
        argv(
            escape_interval_ms=0,
            escape_timeout_ms=100,
            click_nodes=["LoginTap"],
            login_start_node="LoginScreenMarker",
            login_start_target=[640, 635],
            login_marker_priority_attempts=3,
            escape_after_login_start=True,
        ),
    )
    assert context.tasker.controller.clicks == [(640, 635)]
    assert context.tasker.controller.keys == []


def test_login_mode_uses_safe_tap_anywhere_fallback_once(monkeypatch):
    context = Context({
        "HomeMarker": [False, False, True],
        "LoginScreenMarker": [True],
    })
    ticks = iter(value / 1000 for value in range(1000))
    monkeypatch.setattr(common_recover.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(common_recover.time, "sleep", lambda _seconds: None)

    assert common_recover.CommonRecover().run(
        context,
        argv(
            escape_interval_ms=0,
            escape_timeout_ms=100,
            login_start_node="LoginScreenMarker",
            login_start_target=[640, 635],
            login_tap_target=[640, 360],
            escape_after_login_start=True,
        ),
    )
    assert context.tasker.controller.clicks == [(640, 635), (640, 360)]
    assert context.tasker.controller.keys == []


def test_login_start_marker_false_positive_is_clicked_only_once(monkeypatch):
    context = Context({
        "HomeMarker": [False, False, True],
        # The bottom-right menu-shaped marker also occurs on ordinary game
        # pages. It must not suppress BACK forever when it is a false positive.
        "LoginScreenMarker": [True, True],
    })
    ticks = iter(value / 1000 for value in range(1000))
    monkeypatch.setattr(common_recover.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(common_recover.time, "sleep", lambda _seconds: None)

    assert common_recover.CommonRecover().run(
        context,
        argv(
            escape_interval_ms=0,
            escape_timeout_ms=100,
            login_start_node="LoginScreenMarker",
            login_start_target=[640, 635],
            escape_after_login_start=True,
        ),
    )
    assert context.tasker.controller.clicks == [(640, 635)]
    assert context.tasker.controller.keys == [4]
