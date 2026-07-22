from __future__ import annotations

import cv2
import numpy as np

from agent.realtime.difficulty_action import DIFFICULTY_TARGETS, selected_difficulty


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
