from __future__ import annotations

import json
from types import SimpleNamespace

from agent import common_recover


class Job:
    def wait(self):
        return self


class Controller:
    cached_image = object()

    def __init__(self):
        self.keys = []
        self.stops = []
        self.starts = []

    def post_click_key(self, key):
        self.keys.append(key)
        return Job()

    def post_stop_app(self, package):
        self.stops.append(package)
        return Job()

    def post_start_app(self, package):
        self.starts.append(package)
        return Job()


class Context:
    def __init__(self, hits):
        self.hits = iter(hits)
        self.tasker = SimpleNamespace(controller=Controller())

    def run_recognition(self, _node, _image):
        return SimpleNamespace(hit=next(self.hits, False))


def argv(**params):
    return SimpleNamespace(custom_action_param=json.dumps(params))


def test_returns_immediately_when_home_is_visible(monkeypatch):
    context = Context([True])
    monkeypatch.setattr(common_recover.time, "monotonic", lambda: 0)

    assert common_recover.CommonRecover().run(context, argv(escape_timeout_ms=1))
    assert context.tasker.controller.keys == []
    assert context.tasker.controller.starts == []


def test_failure_path_restarts_only_up_to_limit(monkeypatch):
    context = Context([])
    ticks = iter(range(100))
    monkeypatch.setattr(common_recover.time, "monotonic", lambda: next(ticks) / 1000)
    monkeypatch.setattr(common_recover.time, "sleep", lambda _seconds: None)

    result = common_recover.CommonRecover().run(
        context,
        argv(
            escape_interval_ms=0,
            escape_timeout_ms=2,
            restart_limit=2,
            package="test.package",
        ),
    )

    assert not result
    assert context.tasker.controller.keys
    assert context.tasker.controller.stops == ["test.package", "test.package"]
    assert context.tasker.controller.starts == ["test.package", "test.package"]
