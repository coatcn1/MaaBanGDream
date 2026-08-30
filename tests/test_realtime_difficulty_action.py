from __future__ import annotations

import json
from types import SimpleNamespace

import cv2
import numpy as np

from agent.realtime import difficulty_action
from agent.realtime.difficulty_action import (
    DIFFICULTY_TARGETS,
    RealtimeDifficultySelect,
    read_song_level,
    selected_difficulty,
)
from agent.realtime.live_session import (
    current_live_run,
    reset_live_run,
    update_live_run,
)
from agent.realtime.song_identity import (
    SONG_ID_METHOD,
    SONG_ID_ROI,
    UNKNOWN_SONG_ID,
)


def difficulty_frame(selected: str | None):
    hsv = np.zeros((720, 1280, 3), dtype=np.uint8)
    hsv[:, :, 2] = 220
    if selected:
        x, y = DIFFICULTY_TARGETS[selected]
        hsv[y - 30:y + 20, x - 25:x + 25] = (20, 180, 255)
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


def difficulty_frame_with_level(selected: str, level: int):
    image = difficulty_frame(selected)
    cv2.putText(
        image,
        str(level),
        (1205, 474),
        cv2.FONT_HERSHEY_DUPLEX,
        0.58,
        (20, 20, 20),
        1,
        cv2.LINE_AA,
    )
    return image


def test_all_five_difficulty_targets_are_distinct_and_confirmed():
    assert set(DIFFICULTY_TARGETS) == {"Easy", "Normal", "Hard", "Expert", "Special"}
    assert len(set(DIFFICULTY_TARGETS.values())) == 5
    for difficulty in DIFFICULTY_TARGETS:
        assert selected_difficulty(difficulty_frame(difficulty)) == difficulty


def test_hard_never_confirms_when_easy_is_selected():
    assert selected_difficulty(difficulty_frame("Easy")) != "Hard"


def test_no_coloured_selection_is_not_confirmed():
    assert selected_difficulty(difficulty_frame(None)) is None


def test_song_level_reader_recognizes_high_contrast_two_digit_level():
    assert read_song_level(difficulty_frame_with_level("Expert", 25)) == 25
    assert read_song_level(difficulty_frame_with_level("Expert", 26)) == 26
    assert read_song_level(difficulty_frame_with_level("Expert", 28)) == 28


def test_song_level_reader_fails_closed_without_digits():
    assert read_song_level(difficulty_frame("Expert")) is None


class ImmediateJob:
    def __init__(self, result=None):
        self.result = result

    def wait(self):
        return self

    def get(self):
        return self.result


class DifficultyController:
    def __init__(self, image):
        self.images = list(image) if isinstance(image, (list, tuple)) else [image]
        self.screencaps = 0
        self.clicks = []

    def post_click(self, x, y):
        self.clicks.append((x, y))
        return ImmediateJob()

    def post_screencap(self):
        self.screencaps += 1
        index = min(self.screencaps - 1, len(self.images) - 1)
        return ImmediateJob(self.images[index])


def test_successful_difficulty_verification_resets_and_identifies_the_round(monkeypatch):
    image = difficulty_frame("Expert")
    x, y, width, height = SONG_ID_ROI
    image[y:y + height, x:x + width] = np.random.default_rng(7).integers(
        0, 256, size=(height, width, 3), dtype=np.uint8,
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
    monkeypatch.setattr(difficulty_action, "recognize_song_title", lambda _: None)

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
    monkeypatch.setattr(difficulty_action, "recognize_song_title", lambda _: None)

    assert RealtimeDifficultySelect().run(context, argv)

    current = current_live_run()
    assert current is not None
    assert current.song_id == UNKNOWN_SONG_ID
    assert current.song_id_method == "unknown"


def test_level_disambiguated_expert_does_not_fallback(monkeypatch):
    image = difficulty_frame_with_level("Expert", 25)
    x, y, width, height = SONG_ID_ROI
    image[y:y + height, x:x + width] = np.random.default_rng(13).integers(
        0, 256, size=(height, width, 3), dtype=np.uint8,
    )
    controller = DifficultyController(image)
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=controller),
    )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Expert",
        "mode": "formal",
        "verify_delay_seconds": 0,
    }))
    calls = []

    def resolve(song_id, difficulty, song_level):
        calls.append((song_id, difficulty, song_level, None))
        return SimpleNamespace(
            selection=SimpleNamespace(bestdori_song_id=24),
            reason="confirmed local chart by song level",
        )

    monkeypatch.setattr(difficulty_action, "require_game_foreground", lambda _: None)
    monkeypatch.setattr(difficulty_action.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        difficulty_action,
        "resolve_chart_for_selected_song",
        lambda song_id, difficulty, song_level, song_title: resolve(
            song_id, difficulty, song_level
        ),
    )
    monkeypatch.setattr(difficulty_action, "recognize_song_title", lambda _: None)

    assert RealtimeDifficultySelect().run(context, argv)

    current = current_live_run()
    assert current is not None
    assert current.difficulty == "Expert"
    assert current.song_level == 25
    assert calls[0][1:] == ("Expert", 25, None)
    assert controller.clicks == [DIFFICULTY_TARGETS["Expert"]]


def test_ambiguous_shared_jacket_retries_level_and_title_without_reclick(
    monkeypatch,
):
    loading = difficulty_frame("Expert")
    settled = difficulty_frame_with_level("Expert", 26)
    controller = DifficultyController([loading, settled])
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=controller),
    )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Expert",
        "mode": "formal",
        "verify_delay_seconds": 0,
        "identity_read_attempts": 3,
        "identity_retry_delay_seconds": 0,
    }))
    calls = []

    def resolve(song_id, difficulty, song_level, song_title):
        calls.append((song_id, difficulty, song_level, song_title))
        if song_level == 26 and song_title == "ON YOUR MARK":
            return SimpleNamespace(
                selection=SimpleNamespace(bestdori_song_id=184),
                reason="confirmed local chart by song level",
            )
        return SimpleNamespace(
            selection=None,
            reason="song fingerprint mapping is ambiguous",
        )

    monkeypatch.setattr(difficulty_action, "require_game_foreground", lambda _: None)
    monkeypatch.setattr(difficulty_action.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        difficulty_action,
        "resolve_chart_for_selected_song",
        resolve,
    )
    monkeypatch.setattr(
        difficulty_action,
        "recognize_song_title",
        lambda image: (
            SimpleNamespace(text="ON YOUR MARK", confidence=0.95)
            if read_song_level(image) == 26 else None
        ),
    )

    assert RealtimeDifficultySelect().run(context, argv)

    current = current_live_run()
    assert current is not None
    assert current.song_level == 26
    assert current.song_title == "ON YOUR MARK"
    assert controller.clicks == [DIFFICULTY_TARGETS["Expert"]]
    assert controller.screencaps == 2
    assert [call[2:] for call in calls] == [
        (None, None),
        (26, "ON YOUR MARK"),
    ]
