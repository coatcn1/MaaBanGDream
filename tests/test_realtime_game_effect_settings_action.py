from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import agent.realtime.game_effect_settings_action as action_module

from agent.realtime.game_effect_settings_action import (
    RealtimeGameEffectSettingsGate,
    _classify_note_skin_digit,
    _find_note_skin_rows,
    _find_bottom_binary_choice,
    _find_tap_effect,
    _publish_verified_game_visual_settings,
    _read_binary_choice,
    _read_tap_effect,
    _tap_effect_click_plan,
    clear_verified_game_visual_settings,
    verified_game_visual_settings,
)
from agent.realtime.performance_settings_action import (
    _type_digit_templates,
    _type_label_templates,
)


ROOT = Path(__file__).resolve().parents[1]


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


def test_find_note_skin_rows_uses_label_templates_and_pink_radio():
    image = np.full((720, 1280, 3), 255, dtype=np.uint8)
    row_y = 470
    label = dict(_type_label_templates())[7]
    height, width = label.shape[:2]
    image[row_y - 22:row_y - 22 + height, 215:215 + width] = label
    cv2.circle(image, (205, row_y), 10, (110, 20, 245), -1)

    rows = _find_note_skin_rows(
        image,
        search_roi=(220, 180, 90, 390),
        radio_x=205,
    )

    assert [(row.value, row.row_y, row.selected) for row in rows] == [
        (7, row_y, True)
    ]


def test_local_real_settings_frame_reads_type1_and_type2_rows():
    path = ROOT / "debug" / "game-effect-tap-effect-readback.png"
    if not path.is_file():
        pytest.skip("local ignored game settings readback is unavailable")
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)

    rows = _find_note_skin_rows(
        image,
        search_roi=(220, 180, 90, 390),
        radio_x=205,
    )

    assert [(row.value, row.row_y, row.selected) for row in rows] == [
        (1, 469, True),
        (2, 538, False),
    ]


def test_note_skin_classifier_accepts_only_fixed_type_digit_templates():
    assert [
        _classify_note_skin_digit(_type_digit_templates()[value])
        for value in range(1, 8)
    ] == list(range(1, 8))


def test_verified_visual_settings_are_task_scoped_not_short_lived():
    clear_verified_game_visual_settings()
    _publish_verified_game_visual_settings(
        note_skin_type=4,
        tap_effect=3,
        judgement_assist_effect=False,
        clock=lambda: 100.0,
    )

    assert verified_game_visual_settings(clock=lambda: 800.0) is not None
    assert verified_game_visual_settings(
        max_age_seconds=600.0,
        clock=lambda: 800.0,
    ) is None


def test_new_stopped_gate_clears_previous_visual_readback():
    _publish_verified_game_visual_settings(
        note_skin_type=2,
        tap_effect=1,
        judgement_assist_effect=True,
    )
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=True))
    argv = SimpleNamespace(custom_action_param="{}")

    assert RealtimeGameEffectSettingsGate().run(context, argv) is True
    assert verified_game_visual_settings() is None


def test_disabled_gate_reads_and_publishes_actual_settings_without_changing(
    monkeypatch,
):
    image = np.full((720, 1280, 3), 255, dtype=np.uint8)
    clicks = []
    options = {
        "game_effect_settings_enabled": False,
        "note_skin_type": 1,
        "tap_effect": 1,
        "judgement_assist_effect": True,
    }
    monkeypatch.setattr(
        action_module.RealtimeProfileStore,
        "runtime_options",
        lambda _store: options,
    )
    monkeypatch.setattr(
        action_module, "_click", lambda _context, point: clicks.append(point)
    )
    monkeypatch.setattr(action_module, "_wait", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        action_module, "_scroll_to_top", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        action_module, "_scroll_down", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(action_module, "_capture", lambda _context: image)
    monkeypatch.setattr(
        action_module,
        "_find_bottom_binary_choice",
        lambda *_args, **_kwargs: (False, 430),
    )
    monkeypatch.setattr(
        action_module,
        "_find_note_skin_on_page",
        lambda *_args, **_kwargs: (7, None, image),
    )
    monkeypatch.setattr(
        action_module,
        "_find_tap_effect",
        lambda *_args, **_kwargs: (4, 472),
    )
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=object())
    )
    clear_verified_game_visual_settings()

    assert RealtimeGameEffectSettingsGate()._run(context, {}) is True

    verified = verified_game_visual_settings()
    assert verified is not None
    assert verified.note_skin_type == 7
    assert verified.tap_effect == 4
    assert verified.judgement_assist_effect is False
    assert (202, 430) not in clicks
    assert (205, 470) not in clicks
