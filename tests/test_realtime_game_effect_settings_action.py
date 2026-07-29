from __future__ import annotations

import cv2
import numpy as np

from agent.realtime.game_effect_settings_action import (
    _find_bottom_binary_choice,
    _find_tap_effect,
    _read_binary_choice,
    _read_tap_effect,
    _tap_effect_click_plan,
)


def test_find_bottom_binary_choice_uses_last_visible_radio_row():
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.circle(image, (202, 280), 10, (110, 20, 245), -1)
    cv2.circle(image, (302, 430), 10, (110, 20, 245), -1)

    assert _find_bottom_binary_choice(
        image,
        enabled_x=202,
        disabled_x=302,
    ) == (False, 430)


def test_read_binary_choice_uses_selected_magenta_fill():
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.circle(image, (302, 217), 10, (110, 20, 245), -1)

    assert _read_binary_choice(
        image,
        enabled_point=(202, 216),
        disabled_point=(302, 217),
    ) is False


def test_read_binary_choice_rejects_page_without_selected_toggle():
    image = np.zeros((720, 1280, 3), dtype=np.uint8)

    assert _read_binary_choice(
        image,
        enabled_point=(202, 216),
        disabled_point=(302, 217),
    ) is None


def test_read_tap_effect_uses_fixed_roi_and_range_guard():
    image = np.full((720, 1280, 3), 255, dtype=np.uint8)

    assert _read_tap_effect(
        image,
        roi=(400, 460, 35, 35),
        classify=lambda _: 4,
    ) == 4


def test_find_tap_effect_returns_topmost_digit_and_dynamic_row():
    image = np.full((720, 1280, 3), 255, dtype=np.uint8)
    cv2.rectangle(image, (392, 310), (399, 331), (0, 0, 0), -1)

    assert _find_tap_effect(
        image,
        search_roi=(360, 180, 100, 360),
        classify=lambda _: 4,
    ) == (4, 321)


def test_tap_effect_click_plan_uses_shortest_wrapped_path():
    assert _tap_effect_click_plan(1, 4) == ("left", 2)
    assert _tap_effect_click_plan(4, 1) == ("right", 2)
    assert _tap_effect_click_plan(3, 3) == ("right", 0)
