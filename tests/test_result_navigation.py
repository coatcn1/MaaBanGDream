from __future__ import annotations

import numpy as np

from agent.realtime.result_navigation import (
    RESULT_ANIMATION_SKIP_POINT,
    ResultNavigationStatus,
    navigate_result_pages,
)


def test_animation_skip_uses_the_actual_bottom_right_pixel():
    assert RESULT_ANIMATION_SKIP_POINT == (1279, 719)


class Job:
    def __init__(self, value=None):
        self.value = value

    def wait(self):
        return self

    def get(self):
        return self.value


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_unknown_result_pages_repeat_click_recognise_back_click_until_terminal():
    unknown = np.zeros((2, 2, 3), dtype=np.uint8)
    pggbm = np.ones((2, 2, 3), dtype=np.uint8)
    frames = [unknown, unknown, pggbm]
    actions = []

    class Controller:
        def post_screencap(self):
            actions.append(("capture", None))
            return Job(frames.pop(0))

        def post_click(self, x, y):
            actions.append(("click", (x, y)))
            return Job()

        def post_click_key(self, key):
            actions.append(("key", key))
            return Job()

    clock = Clock()
    outcome = navigate_result_pages(
        Controller(),
        lambda: False,
        lambda image: "pggbm" if image.any() else None,
        clock=clock.monotonic,
        sleeper=clock.sleep,
        timeout_seconds=10,
        settle_seconds=0,
        retry_interval_seconds=1,
    )

    assert outcome.status is ResultNavigationStatus.IDENTIFIED
    assert outcome.page_state == "pggbm"
    assert outcome.back_attempts == 2
    assert actions == [
        ("click", RESULT_ANIMATION_SKIP_POINT),
        ("capture", None),
        ("key", 4),
        ("click", RESULT_ANIMATION_SKIP_POINT),
        ("capture", None),
        ("key", 4),
        ("click", RESULT_ANIMATION_SKIP_POINT),
        ("capture", None),
    ]


def test_result_navigation_stop_before_first_click_is_input_neutral():
    class ControllerMustNotRun:
        def post_screencap(self):
            raise AssertionError("stopped navigation must not capture")

        def post_click(self, *_point):
            raise AssertionError("stopped navigation must not click")

        def post_click_key(self, _key):
            raise AssertionError("stopped navigation must not press Back")

    outcome = navigate_result_pages(
        ControllerMustNotRun(),
        lambda: True,
        lambda _image: None,
    )

    assert outcome.status is ResultNavigationStatus.STOPPED
    assert outcome.back_attempts == 0


def test_result_navigation_is_bounded_by_time_not_a_small_back_retry_cap():
    frame = np.zeros((2, 2, 3), dtype=np.uint8)
    backs = []

    class Controller:
        def post_screencap(self):
            return Job(frame.copy())

        def post_click(self, _x, _y):
            return Job()

        def post_click_key(self, key):
            backs.append(key)
            return Job()

    clock = Clock()
    outcome = navigate_result_pages(
        Controller(),
        lambda: False,
        lambda _image: None,
        clock=clock.monotonic,
        sleeper=clock.sleep,
        timeout_seconds=15,
        settle_seconds=0,
        retry_interval_seconds=1,
    )

    assert outcome.status is ResultNavigationStatus.TIMED_OUT
    assert len(backs) == 15
    assert outcome.back_attempts == 15
