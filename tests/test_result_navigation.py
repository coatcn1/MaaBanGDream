from __future__ import annotations

import numpy as np

from agent.realtime.result_navigation import (
    RESULT_ANIMATION_SKIP_POINT,
    ResultNavigationStatus,
    _wait_unless_stopping,
    advance_result_cadence,
    accelerated_back,
    navigate_result_pages,
    handle_story_page,
)


def test_animation_skip_uses_the_actual_bottom_right_pixel():
    assert RESULT_ANIMATION_SKIP_POINT == (1279, 719)


def test_story_pages_are_handled_without_back_cancelling_skip_confirmation():
    clock = Clock()
    frames = iter(["menu", "skip", "confirm", "pggbm"])
    handled = []
    class Controller:
        def post_screencap(self):
            return Job(next(frames))
        def post_click(self, *_point):
            return Job()
        def post_click_key(self, _key):
            raise AssertionError("剧情跳过期间不能用 BACK 取消弹窗")
    outcome = navigate_result_pages(
        Controller(), lambda: False,
        lambda frame: "pggbm" if frame == "pggbm" else None,
        handle_intermediate=lambda frame: handled.append(frame) or True,
        clock=clock.monotonic, sleeper=clock.sleep,
    )
    assert outcome.status is ResultNavigationStatus.IDENTIFIED
    assert handled == ["menu", "skip", "confirm"]


def test_story_stop_during_recognition_prevents_click():
    from types import SimpleNamespace
    stopped = [False]
    def recognise(_image, _node):
        stopped[0] = True
        return SimpleNamespace(x=0, y=0, w=2, h=2)
    assert not handle_story_page(
        None, recognise=recognise,
        click=lambda _point: (_ for _ in ()).throw(AssertionError("停止后仍输入")),
        stopping=lambda: stopped[0],
    )


def test_wait_never_sleeps_negative_when_stopping_blocks_past_deadline():
    t = [100.0]

    def clock() -> float:
        return t[0]

    def stopping() -> bool:
        # 模拟 stopping 的原生调用阻塞，把时钟推进到 deadline 之后。
        t[0] += 1.0
        return False

    slept: list[float] = []

    def sleeper(seconds: float) -> None:
        slept.append(seconds)
        t[0] += seconds

    result = _wait_unless_stopping(
        0.05,
        stopping,
        clock=clock,
        sleeper=sleeper,
    )
    assert result is True
    assert all(seconds >= 0 for seconds in slept)


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


def test_single_step_cadence_preserves_click_back_click_order():
    actions = []

    class Controller:
        def post_click(self, x, y):
            actions.append(("click", (x, y)))
            return Job()

        def post_click_key(self, key):
            actions.append(("key", key))
            return Job()

    back_next = False
    for index in range(3):
        back_next = advance_result_cadence(
            Controller(),
            back_next=back_next,
            phase=f"step-{index + 1}",
        )

    assert actions == [
        ("click", RESULT_ANIMATION_SKIP_POINT),
        ("key", 4),
        ("click", RESULT_ANIMATION_SKIP_POINT),
    ]
    assert back_next is True


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


def test_accelerated_back_refreshes_reverse_controller_after_each_guard():
    actions = []
    generation = [0]

    class Controller:
        def __init__(self, created_generation):
            self.created_generation = created_generation

        def _require_current(self):
            if self.created_generation != generation[0]:
                raise OSError(
                    "exception: access violation reading 0xFFFFFFFFFFFFFFFF"
                )

        def post_click(self, x, y):
            self._require_current()
            actions.append(("click", (x, y), self.created_generation))
            return Job()

        def post_click_key(self, key):
            self._require_current()
            actions.append(("key", key, self.created_generation))
            return Job()

    def before_input():
        # 真实 Agent 的前台保护会调用一次反向控制器；之后旧包装对象
        # 可能指向已释放的原生代理，输入前必须重新获取。
        generation[0] += 1

    def current_controller():
        return Controller(generation[0])

    accelerated_back(
        current_controller,
        before_input=before_input,
        phase="regression",
    )

    assert actions == [
        ("click", RESULT_ANIMATION_SKIP_POINT, 1),
        ("key", 4, 2),
        ("click", RESULT_ANIMATION_SKIP_POINT, 3),
    ]
