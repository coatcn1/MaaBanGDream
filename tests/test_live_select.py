from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from agent import live_select


class Job:
    def __init__(self, result=None, *, succeeded=True):
        self.result = result
        self.succeeded = succeeded

    def wait(self):
        return self

    def get(self):
        return self.result


class Controller:
    def __init__(self):
        self.clicks = []
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.cached_image = self.image

    def post_screencap(self):
        return Job(self.image)

    def post_shell(self, _command, _timeout=20000):
        return Job(
            "mCurrentFocus=Window{123 u0 "
            "com.bilibili.star.bili/.MainActivity}"
        )

    def post_click(self, x, y):
        self.clicks.append((x, y))
        return Job()


class Context:
    def __init__(self, *, template_hit=False, ocr_hits=None):
        self.tasker = SimpleNamespace(
            stopping=False,
            controller=Controller(),
        )
        self.template_hit = template_hit
        self.ocr_hits = ocr_hits or {}
        self.ocr_requests = []

    def run_task(self, node):
        assert node == "CommonRefreshScreen"
        return SimpleNamespace(status=SimpleNamespace(succeeded=True))

    def run_recognition(self, _node, _image):
        box = SimpleNamespace(x=100, y=200, w=80, h=40)
        return SimpleNamespace(hit=self.template_hit, box=box)

    def run_recognition_direct(self, reco_type, reco_param, _image):
        self.ocr_requests.append((reco_type, reco_param))
        expected = reco_param.expected[0]
        hit = self.ocr_hits.get(expected, False)
        box = SimpleNamespace(x=300, y=400, w=120, h=60) if hit else None
        return SimpleNamespace(hit=hit, box=box)


def test_reacquires_controller_after_nested_refresh():
    context = Context(template_hit=True)
    original_run_task = context.run_task
    first_controller = context.tasker.controller

    def invalidate_and_refresh(node):
        detail = original_run_task(node)
        first_controller.post_shell = lambda *_args: (_ for _ in ()).throw(
            RuntimeError("stale controller")
        )
        context.tasker.controller = Controller()
        return detail

    context.run_task = invalidate_and_refresh

    assert live_select.LiveSelectFind().run(
        context,
        argv(
            expected="自由演出",
            template_node="AutoLiveFreeLiveTemplate",
            click=True,
        ),
    )
    assert context.tasker.controller.clicks == [(140, 220)]


def argv(**params):
    return SimpleNamespace(custom_action_param=json.dumps(params, ensure_ascii=False))


def test_template_match_clicks_the_recognized_box():
    context = Context(template_hit=True)

    assert live_select.LiveSelectFind().run(
        context,
        argv(
            expected="自由演出",
            template_node="AutoLiveFreeLiveTemplate",
            click=True,
        ),
    )

    assert context.tasker.controller.clicks == [(140, 220)]
    assert context.ocr_requests == []


def test_ocr_fallback_clicks_moved_entry_box():
    context = Context(ocr_hits={"挑战演出": True})

    assert live_select.LiveSelectFind().run(
        context,
        argv(
            expected="挑战演出",
            template_node="ChallengeEntryTemplate",
            roi=[0, 120, 1280, 600],
            click=True,
        ),
    )

    assert context.tasker.controller.clicks == [(360, 430)]
    assert context.ocr_requests[0][1].expected == ["挑战演出"]


def test_color_fallback_finds_current_free_live_card():
    context = Context()
    context.tasker.controller.image[202:524, 701:914] = (246, 186, 74)

    assert live_select.LiveSelectFind().run(
        context,
        argv(
            expected="自由演出",
            template_node="AutoLiveFreeLiveTemplate",
            click=True,
        ),
    )

    assert context.tasker.controller.clicks == [(807, 363)]


def test_color_fallback_finds_default_cooperative_live_card():
    context = Context()
    context.tasker.controller.image[202:524, 927:1140] = (70, 70, 235)

    assert live_select.LiveSelectFind().run(
        context,
        argv(
            expected="协力演出",
            template_node="CooperativeLiveEntryTemplate",
            click=True,
        ),
    )

    assert context.tasker.controller.clicks == [(1033, 363)]


def test_color_fallback_finds_tour_live_card():
    context = Context()
    context.tasker.controller.image[202:345, 701:914] = (183, 93, 213)

    assert live_select.LiveSelectFind().run(
        context,
        argv(expected="巡回演出", click=True),
    )

    assert context.tasker.controller.clicks == [(807, 273)]


def test_missing_challenge_is_failure_and_never_clicks(capsys):
    context = Context()

    assert not live_select.LiveSelectFind().run(
        context,
        argv(
            expected="挑战演出",
            template_node="ChallengeEntryTemplate",
            click=True,
            missing_reason="当前没有可用的挑战演出活动",
        ),
    )

    assert context.tasker.controller.clicks == []
    assert "当前没有可用的挑战演出活动" in capsys.readouterr().out


def test_page_confirmation_can_recognize_without_clicking():
    context = Context(ocr_hits={"自由演出": True})

    assert live_select.LiveSelectFind().run(
        context,
        argv(expected="自由演出", click=False),
    )

    assert context.tasker.controller.clicks == []


@pytest.fixture
def delivery_clock(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(live_select.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(live_select.time, "sleep", lambda seconds: setattr(clock, "now", clock.now + seconds))
    return clock


class DeliveryContext(Context):
    def __init__(self, frames):
        super().__init__()
        self.frames = frames
        self.frame = -1
        self.requests = []

    def run_task(self, node):
        detail = super().run_task(node)
        self.frame += 1
        self.tasker.controller.cached_image = np.zeros((720, 1280, 3), dtype=np.uint8)
        return detail

    def run_recognition(self, node, image):
        assert image is self.tasker.controller.cached_image
        self.requests.append((self.frame, node))
        state = self.frames[min(self.frame, len(self.frames) - 1)]
        if node == "RoomMarker":
            return SimpleNamespace(hit=state == "delivered", box=None)
        assert node == "EntryTemplate"
        box = SimpleNamespace(x=state[0], y=state[1], w=80, h=40) if isinstance(state, tuple) else None
        return SimpleNamespace(hit=box is not None, box=box)


def delivery_argv(**overrides):
    params = dict(
        expected="协力演出", template_node="EntryTemplate", click=True,
        delivery_node="RoomMarker", delivery_timeout_ms=2500,
        delivery_retry_interval_ms=1000, delivery_max_clicks=3, interval_ms=500,
    )
    params.update(overrides)
    return argv(**params)


def test_foreground_guard_can_invalidate_controller_before_click(monkeypatch):
    context = Context(template_hit=True)
    stale = context.tasker.controller
    fresh = Controller()

    def replace_controller(controller):
        assert controller is stale
        stale.post_click = lambda *_: pytest.fail("clicked stale controller after foreground guard")
        context.tasker.controller = fresh

    monkeypatch.setattr(live_select, "require_game_foreground", replace_controller)
    assert live_select.LiveSelectFind().run(context, argv(template_node="EntryTemplate"))
    assert fresh.clicks == [(140, 220)]


def test_delivery_retries_ignored_click_using_current_box(delivery_clock):
    context = DeliveryContext([(100, 200), (200, 300), (300, 300), (400, 400), "delivered"])
    assert live_select.LiveSelectFind().run(context, delivery_argv())
    assert context.tasker.controller.clicks == [(140, 220), (440, 420)]
    assert context.requests == [(0, "EntryTemplate"), (1, "RoomMarker"), (2, "RoomMarker"), (3, "RoomMarker"), (3, "EntryTemplate"), (4, "RoomMarker")]


def test_delivery_unknown_loading_never_repeats_old_click(delivery_clock):
    context = DeliveryContext([(100, 200), "loading"])
    assert not live_select.LiveSelectFind().run(context, delivery_argv())
    assert context.tasker.controller.clicks == [(140, 220)]


def test_delivery_rechecks_same_source_instead_of_switching_to_ocr(delivery_clock):
    context = DeliveryContext([(100, 200), "loading"])
    context.ocr_hits = {"协力演出": True}
    assert not live_select.LiveSelectFind().run(context, delivery_argv())
    assert context.tasker.controller.clicks == [(140, 220)]
    assert context.ocr_requests == []
    assert len([node for _, node in context.requests if node == "EntryTemplate"]) > 1


def test_delivery_budget_and_click_limit_do_not_report_false_success(delivery_clock, capsys):
    context = DeliveryContext([(100, 200)])
    assert not live_select.LiveSelectFind().run(context, delivery_argv(delivery_timeout_ms=4000, delivery_max_clicks=99))
    assert len(context.tasker.controller.clicks) == 3
    assert "未确认房间选择页" in capsys.readouterr().out
    assert delivery_clock.now == 4.0


@pytest.mark.parametrize("stage", ["before", "guard", "after_click", "delivery", "retry_source"])
def test_delivery_stop_has_priority_over_next_input(monkeypatch, delivery_clock, stage):
    context = DeliveryContext([(100, 200)])
    controller = context.tasker.controller
    if stage == "before":
        context.tasker.stopping = True
    elif stage == "guard":
        monkeypatch.setattr(live_select, "require_game_foreground", lambda _: setattr(context.tasker, "stopping", True))
    elif stage == "after_click":
        click = controller.post_click
        def stop_after_click(x, y):
            job = click(x, y)
            context.tasker.stopping = True
            return job
        controller.post_click = stop_after_click
    else:
        recognize = context.run_recognition
        def stop_during_recognition(node, image):
            result = recognize(node, image)
            if (stage == "delivery" and node == "RoomMarker") or (stage == "retry_source" and node == "EntryTemplate" and context.frame > 0):
                context.tasker.stopping = True
            return result
        context.run_recognition = stop_during_recognition
    assert live_select.LiveSelectFind().run(context, delivery_argv())
    assert len(controller.clicks) == (0 if stage in {"before", "guard"} else 1)


def test_failed_initial_click_fails_before_delivery(monkeypatch, delivery_clock, capsys):
    context = DeliveryContext([(100, 200), "delivered"])
    context.tasker.controller.post_click = lambda *_: Job(succeeded=False)
    assert not live_select.LiveSelectFind().run(context, delivery_argv())
    assert context.requests == [(0, "EntryTemplate")]
    assert "控制器未确认输入成功" in capsys.readouterr().out
