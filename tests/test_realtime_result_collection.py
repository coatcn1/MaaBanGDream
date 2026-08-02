from __future__ import annotations

import numpy as np
from agent.realtime.profile_play_action import (
    ResultCollectionStatus,
    collect_result,
)
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


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_result_collection_checks_each_frame_and_presses_one_back_at_a_time():
    controller = Controller()
    clock = Clock()

    outcome = collect_result(
        controller,
        lambda: False,
        stability_interval_seconds=1,
        parser=Parser(),
        clock=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert outcome.status is ResultCollectionStatus.STABLE
    assert outcome.result.fast == 33
    assert outcome.result.slow == 9
    assert outcome.image.any()
    assert controller.backs == 1


def test_result_collection_stops_without_back_input():
    controller = Controller()

    outcome = collect_result(controller, lambda: True, parser=Parser())

    assert outcome.status is ResultCollectionStatus.STOPPED
    assert controller.backs == 0


def test_result_collection_checks_foreground_before_back_input():
    controller = Controller()

    def reject_foreign_app():
        raise RuntimeError("foreign foreground")

    try:
        collect_result(
            controller, lambda: False, parser=Parser(),
            before_input=reject_foreign_app,
        )
    except RuntimeError as exc:
        assert "foreign foreground" in str(exc)
    else:
        raise AssertionError("foreground rejection must propagate")

    assert controller.backs == 0


class AnimatingController:
    """Result panel whose numbers count up across screenshots."""

    def __init__(self):
        self.frames = [
            np.full((720, 1280, 3), 1, dtype=np.uint8),
            np.full((720, 1280, 3), 2, dtype=np.uint8),
            np.full((720, 1280, 3), 3, dtype=np.uint8),
            np.full((720, 1280, 3), 3, dtype=np.uint8),
        ]
        self.backs = 0

    def post_screencap(self):
        return Job(self.frames.pop(0))

    def post_click_key(self, key):
        assert key == 4
        self.backs += 1
        return Job()


class AnimatingParser:
    COUNTS = {
        1: LiveResult(1, 0, 0, 0, 0, 1, 0, .9),
        2: LiveResult(120, 9, 0, 0, 1, 10, 2, .9),
        3: LiveResult(374, 24, 0, 0, 6, 23, 1, .9),
    }

    def parse(self, image):
        return self.COUNTS[int(image.flat[0])]


def test_result_collection_waits_for_count_up_animation_to_settle():
    controller = AnimatingController()
    clock = Clock()

    outcome = collect_result(
        controller,
        lambda: False,
        stability_interval_seconds=1,
        parser=AnimatingParser(),
        clock=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert outcome.status is ResultCollectionStatus.STABLE
    assert outcome.result.perfect == 374
    assert outcome.result.miss == 6
    assert int(outcome.image.flat[0]) == 3
    # The panel is already visible, so settling must never press BACK.
    assert controller.backs == 0


def test_result_timeout_uses_slow_then_medium_esc_and_never_clicks():
    clock = Clock()

    class NoResultController:
        def __init__(self):
            self.esc_times = []

        def post_screencap(self):
            return Job(np.zeros((720, 1280, 3), dtype=np.uint8))

        def post_click_key(self, key):
            assert key == 4
            self.esc_times.append(clock.now)
            return Job()

    class NoResultParser:
        def parse(self, _image):
            raise ValueError("not result")

    controller = NoResultController()
    outcome = collect_result(
        controller,
        lambda: False,
        parser=NoResultParser(),
        clock=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert outcome.status is ResultCollectionStatus.TIMED_OUT
    assert outcome.elapsed_seconds == 60
    early = [time for time in controller.esc_times if time < 30]
    late = [time for time in controller.esc_times if time >= 30]
    assert early[:3] == [0.0, 1.5, 3.0]
    assert late[:3] == [30.0, 31.0, 32.0]
