from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np

from agent.realtime import random_song_action
from agent.realtime.random_song_action import (
    SONG_FILTER_BUTTON,
    SONG_FILTER_CLOSE_BUTTON,
    SONG_FILTER_RESET_BUTTON,
    RandomSongSelect,
)
from agent.realtime.song_identity import SONG_ID_ROI, identify_song


def song_frame(seed: int):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    rng = np.random.default_rng(seed)
    x, y, width, height = SONG_ID_ROI
    image[y:y + height, x:x + width] = rng.integers(
        0, 256, size=(height, width, 3), dtype=np.uint8,
    )
    return image


class ImmediateJob:
    def __init__(self, result=None):
        self.result = result

    def wait(self):
        return self

    def get(self):
        return self.result


class Controller:
    def __init__(self, images):
        self.images = list(images)
        self.clicks = []
        self.screencaps = 0

    def post_click(self, x, y):
        self.clicks.append((x, y))
        return ImmediateJob()

    def post_screencap(self):
        self.screencaps += 1
        return ImmediateJob(self.images.pop(0) if self.images else None)


def context_for(controller):
    return SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=controller),
    )


def argv_for(params=None):
    return SimpleNamespace(
        custom_action_param=json.dumps(params or {}),
        box=SimpleNamespace(x=687, y=642, w=1, h=1),
    )


def run(monkeypatch, controller, params=None):
    monkeypatch.setattr(random_song_action, "require_game_foreground", lambda _: None)
    monkeypatch.setattr(random_song_action.time, "sleep", lambda _: None)
    return RandomSongSelect().run(context_for(controller), argv_for(params))


def test_random_song_select_succeeds_when_first_click_changes_song(monkeypatch):
    controller = Controller([song_frame(1), song_frame(2)])

    assert run(monkeypatch, controller) is True
    assert controller.clicks == [(687, 642)]
    assert controller.screencaps == 2


def test_random_song_select_resets_filter_when_first_click_keeps_song(monkeypatch):
    same = song_frame(1)
    controller = Controller([same, same, song_frame(2)])

    assert run(monkeypatch, controller, {"max_attempts": 2}) is True
    assert controller.clicks == [
        (687, 642),
        SONG_FILTER_BUTTON,
        SONG_FILTER_RESET_BUTTON,
        SONG_FILTER_CLOSE_BUTTON,
        (687, 642),
    ]
    assert controller.screencaps == 3


def test_random_song_select_fails_when_song_never_changes(monkeypatch):
    same = song_frame(1)
    controller = Controller([same, same, same])

    assert run(monkeypatch, controller, {"max_attempts": 2}) is False
    assert controller.clicks == [
        (687, 642),
        SONG_FILTER_BUTTON,
        SONG_FILTER_RESET_BUTTON,
        SONG_FILTER_CLOSE_BUTTON,
        (687, 642),
    ]


def test_random_song_select_uses_param_coordinates_for_filter_reset(monkeypatch):
    same = song_frame(1)
    params = {
        "max_attempts": 2,
        "filter_button": [1, 2],
        "filter_reset_button": [3, 4],
        "filter_close_button": [5, 6],
    }
    controller = Controller([same, same, song_frame(2)])

    assert run(monkeypatch, controller, params) is True
    assert controller.clicks == [(687, 642), (1, 2), (3, 4), (5, 6), (687, 642)]


def test_random_song_select_cannot_verify_unknown_identity(monkeypatch):
    blank = np.zeros((720, 1280, 3), dtype=np.uint8)
    controller = Controller([blank, song_frame(2), song_frame(3)])

    assert run(monkeypatch, controller, {"max_attempts": 2}) is False


def test_calibration_random_preserves_filter_and_skips_used_song(monkeypatch):
    before = song_frame(1)
    used = song_frame(2)
    fresh = song_frame(3)
    used_id = identify_song(used).song_id
    controller = Controller([before, used, fresh])

    assert run(monkeypatch, controller, {
        "max_attempts": 2,
        "preserve_filter": True,
        "excluded_song_ids": [used_id],
    }) is True
    assert controller.clicks == [(687, 642), (687, 642)]


def test_random_song_select_callback_reports_failure_instead_of_leaking(monkeypatch):
    class BrokenController:
        def post_screencap(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(random_song_action, "require_game_foreground", lambda _: None)
    monkeypatch.setattr(random_song_action.time, "sleep", lambda _: None)

    action = RandomSongSelect()
    assert action.run(context_for(BrokenController()), argv_for()) is False
