from __future__ import annotations

import numpy as np
import pytest

from agent.realtime.profile_play_action import collect_result
from agent.realtime.result_parser import LiveResult


class Job:
    def __init__(self, value=None):
        self.value = value

    def wait(self):
        return self

    def get(self):
        return self.value


class Controller:
    def __init__(self):
        self.frames = [
            np.zeros((720, 1280, 3), dtype=np.uint8),
            np.ones((720, 1280, 3), dtype=np.uint8),
        ]
        self.backs = 0

    def post_screencap(self):
        return Job(self.frames.pop(0))

    def post_click_key(self, key):
        assert key == 4
        self.backs += 1
        return Job()


class Parser:
    def parse(self, image):
        if not image.any():
            raise ValueError("not result")
        return LiveResult(170, 42, 0, 0, 3, 33, 9, .8)


def test_result_collection_checks_each_frame_and_presses_one_back_at_a_time():
    controller = Controller()

    result, image = collect_result(
        controller,
        lambda: False,
        attempts=3,
        interval_seconds=0,
        parser=Parser(),
    )

    assert result.fast == 33
    assert result.slow == 9
    assert image.any()
    assert controller.backs == 1


def test_result_collection_stops_without_back_input():
    controller = Controller()

    with pytest.raises(InterruptedError, match="任务停止"):
        collect_result(controller, lambda: True, parser=Parser())

    assert controller.backs == 0


def test_result_collection_checks_foreground_before_back_input():
    controller = Controller()

    def reject_foreign_app():
        raise RuntimeError("foreign foreground")

    with pytest.raises(RuntimeError, match="foreign foreground"):
        collect_result(
            controller,
            lambda: False,
            parser=Parser(),
            before_input=reject_foreign_app,
        )

    assert controller.backs == 0
