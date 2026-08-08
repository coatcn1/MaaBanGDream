from __future__ import annotations

import json
from types import SimpleNamespace

import cv2
import numpy as np

from agent.realtime import difficulty_action
from agent.realtime.difficulty_action import (
    DIFFICULTY_TARGETS,
    RealtimeDifficultySelect,
    selected_difficulty,
)
from agent.realtime.live_session import (
    current_live_run,
    reset_live_run,
    update_live_run,
)
from agent.realtime.song_identity import SONG_ID_METHOD, UNKNOWN_SONG_ID


def difficulty_frame(selected: str | None):
    hsv = np.zeros((720, 1280, 3), dtype=np.uint8)
    hsv[:, :, 2] = 220
    if selected:
        x, y = DIFFICULTY_TARGETS[selected]
        hsv[y - 30:y + 20, x - 25:x + 25] = (20, 180, 255)
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


def test_all_five_difficulty_targets_are_distinct_and_confirmed():
    assert set(DIFFICULTY_TARGETS) == {"Easy", "Normal", "Hard", "Expert", "Special"}
    assert len(set(DIFFICULTY_TARGETS.values())) == 5
    for difficulty in DIFFICULTY_TARGETS:
        assert selected_difficulty(difficulty_frame(difficulty)) == difficulty


def test_hard_never_confirms_when_easy_is_selected():
    assert selected_difficulty(difficulty_frame("Easy")) != "Hard"


def test_no_coloured_selection_is_not_confirmed():
    assert selected_difficulty(difficulty_frame(None)) is None


class ImmediateJob:
    def __init__(self, result=None):
        self.result = result

    def wait(self):
        return self

    def get(self):
        return self.result


class DifficultyController:
    def __init__(self, image):
        self.image = image
        self.screencaps = 0
        self.clicks = []

    def post_click(self, x, y):
        self.clicks.append((x, y))
        return ImmediateJob()

    def post_screencap(self):
        self.screencaps += 1
        return ImmediateJob(self.image)


def test_successful_difficulty_verification_resets_and_identifies_the_round(monkeypatch):
    image = difficulty_frame("Expert")
    image[110:500, 40:450] = np.random.default_rng(7).integers(
        0, 256, size=(390, 410, 3), dtype=np.uint8,
    )
    controller = DifficultyController(image)
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=controller),
    )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Expert",
        "mode": "calibration-rehearsal",
        "verify_delay_seconds": 0,
    }))
    stale = reset_live_run(mode="formal", difficulty="Easy")
    monkeypatch.setattr(difficulty_action, "require_game_foreground", lambda _: None)
    monkeypatch.setattr(difficulty_action.time, "sleep", lambda _: None)

    assert RealtimeDifficultySelect().run(context, argv)

    current = current_live_run()
    assert current is not None
    assert current.run_id != stale.run_id
    assert current.mode == "calibration-rehearsal"
    assert current.difficulty == "Expert"
    assert current.song_id.startswith(f"{SONG_ID_METHOD}-")
    assert current.song_id_method == SONG_ID_METHOD
    assert controller.screencaps == 1


def test_formal_round_can_continue_with_unknown_song_without_stale_identity(monkeypatch):
    controller = DifficultyController(difficulty_frame("Hard"))
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=controller),
    )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Hard",
        "mode": "formal",
        "verify_delay_seconds": 0,
    }))
    reset_live_run(mode="formal", difficulty="Easy")
    update_live_run(
        song_id="song-phash-v1-0123456789abcdef",
        song_id_method=SONG_ID_METHOD,
    )
    monkeypatch.setattr(difficulty_action, "require_game_foreground", lambda _: None)
    monkeypatch.setattr(difficulty_action.time, "sleep", lambda _: None)

    assert RealtimeDifficultySelect().run(context, argv)

    current = current_live_run()
    assert current is not None
    assert current.song_id == UNKNOWN_SONG_ID
    assert current.song_id_method == "unknown"
