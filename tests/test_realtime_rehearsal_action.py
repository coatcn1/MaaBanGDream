from __future__ import annotations

import pytest
import numpy as np

from agent.realtime.rehearsal_action import (
    RealtimeEasyRehearsal,
    frame_resolution,
    validate_rehearsal_environment,
)


PARAMS = {"game_fps": 60, "render_quality": "standard", "note_speed": 2.0}


def test_frame_resolution_uses_captured_image_shape():
    assert frame_resolution(np.zeros((720, 1280, 3), dtype=np.uint8)) == (1280, 720)


def test_frame_resolution_rejects_invalid_frame():
    with pytest.raises(ValueError, match="截图数据无效"):
        frame_resolution(None)


def test_easy_rehearsal_accepts_only_locked_environment():
    signature = validate_rehearsal_environment(
        (1280, 720), "Override density: 240", PARAMS
    )
    assert signature.resolution == (1280, 720)


@pytest.mark.parametrize(
    ("resolution", "density", "params"),
    [
        ((1920, 1080), "Override density: 240", PARAMS),
        ((1280, 720), "Override density: 320", PARAMS),
        ((1280, 720), "Override density: 240", {**PARAMS, "game_fps": 90}),
        ((1280, 720), "Override density: 240", {**PARAMS, "note_speed": 2.5}),
    ],
)
def test_easy_rehearsal_rejects_environment_drift(resolution, density, params):
    with pytest.raises(ValueError, match="环境不匹配"):
        validate_rehearsal_environment(resolution, density, params)


def test_rehearsal_callback_reports_failure_instead_of_leaking_exception(monkeypatch):
    action = RealtimeEasyRehearsal()
    monkeypatch.setattr(action, "_run", lambda *_: (_ for _ in ()).throw(RuntimeError("boom")))

    assert action.run(object(), object()) is False


def test_rehearsal_enables_near_line_rescue_for_short_lived_skill_notes(monkeypatch):
    captured = {}
    foreground_checks = []
    dispatcher_options = []

    class FakeScreenshot:
        def wait(self):
            return self

        def get(self):
            return np.zeros((720, 1280, 3), dtype=np.uint8)

    class FakeController:
        def post_screencap(self):
            return FakeScreenshot()

    class FakeEngine:
        def __init__(self, detector, planner, touch, **kwargs):
            captured["planner"] = planner

        def run(self, *args, **kwargs):
            return type("Stats", (), {
                "processed_frames": 0,
                "dispatched_actions": 0,
                "stopped": False,
                "aborted_for_life": False,
            })()

    class FakeDispatcher:
        def __init__(self, controller, stopping, **kwargs):
            dispatcher_options.append(kwargs)

    monkeypatch.setattr("agent.realtime.rehearsal_action.RealtimeEngine", FakeEngine)
    monkeypatch.setattr(
        "agent.realtime.rehearsal_action.ControllerTouchDispatcher",
        FakeDispatcher,
    )
    monkeypatch.setattr(
        "agent.realtime.rehearsal_action.require_game_foreground",
        lambda controller: foreground_checks.append(controller),
    )
    context = type("Context", (), {
        "tasker": type("Tasker", (), {
            "stopping": False,
            "controller": FakeController(),
        })(),
    })()
    argv = type("Arg", (), {"custom_action_param": "{}"})()

    assert RealtimeEasyRehearsal().run(context, argv) is True
    assert captured["planner"].rescue_first_visible is True
    assert len(foreground_checks) == 1
    assert dispatcher_options == [{}]
