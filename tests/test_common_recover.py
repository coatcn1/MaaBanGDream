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
    def __init__(self):
        self.image = object()
        self.captures = 0
        self.clicks = []
        self.keys = []
        self.stops = []
        self.starts = []

    def post_screencap(self):
        self.captures += 1
        return Job(self.image)

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
    def __init__(self, stopping=False):
        self.controller = Controller()
        self.stopping = stopping


class Context:
    def __init__(self, recognitions=None, *, stopping=False):
        self.recognitions = {
            name: iter(results) for name, results in (recognitions or {}).items()
        }
        self.tasker = Tasker(stopping)

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
    assert context.tasker.controller.captures == 1
    assert context.tasker.controller.keys == []
    assert context.tasker.controller.starts == []


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

    assert not common_recover.CommonRecover().run(context, argv())
    assert context.tasker.controller.captures == 0
    assert context.tasker.controller.clicks == []
    assert context.tasker.controller.keys == []
    assert context.tasker.controller.stops == []
    assert context.tasker.controller.starts == []


def test_failure_path_restarts_only_up_to_limit(monkeypatch):
    context = Context()
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
