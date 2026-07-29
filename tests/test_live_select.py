from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np

from agent import live_select


class Job:
    def __init__(self, result=None):
        self.result = result

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
