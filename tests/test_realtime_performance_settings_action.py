from __future__ import annotations

from types import SimpleNamespace

import cv2
import numpy as np

from agent.realtime.performance_settings_action import (
    DEFAULT_COORDINATES,
    RealtimePerformanceSettingsGate,
    _digit_templates,
    _expected_speed,
    _read_speed,
    _speed_click_plan,
    clear_verified_settings,
    verified_settings,
)
from agent.realtime.game_effect_settings_action import (
    _publish_verified_game_visual_settings,
    clear_verified_game_visual_settings,
)


class _Screenshot:
    def wait(self):
        return self

    def get(self):
        return np.zeros((720, 1280, 3), dtype=np.uint8)


class _Controller:
    def post_screencap(self):
        return _Screenshot()


def test_fixed_digit_template_reader_decodes_two_digit_upper_bound():
    image = np.full((720, 1280, 3), 240, dtype=np.uint8)
    x, y, width, height = DEFAULT_COORDINATES["speed_roi"]
    mask = np.zeros((height, width), dtype=np.uint8)
    dot_x = 58
    placements = (
        (1, dot_x - 30),
        (2, dot_x - 16),
        (0, dot_x + 5),
        (0, dot_x + 20),
    )
    for digit, left in placements:
        glyph = cv2.resize(
            _digit_templates()[digit].astype(np.uint8),
            (14, 25),
            interpolation=cv2.INTER_NEAREST,
        )
        mask[18:43, left:left + 14] = np.maximum(
            mask[18:43, left:left + 14],
            glyph,
        )
    mask[37:40, dot_x:dot_x + 3] = 1
    display = image[y:y + height, x:x + width]
    display[mask.astype(bool)] = 0

    assert _read_speed(image, DEFAULT_COORDINATES["speed_roi"]) == 12.0


def test_speed_click_plan_uses_half_tenth_cent_steps_without_wrapping():
    assert _speed_click_plan(12.0, 5.0) == [("decrease_050", 14)]
    assert _speed_click_plan(2.37, 5.0) == [
        ("increase_050", 5),
        ("increase_010", 1),
        ("increase_001", 3),
    ]


def test_visual_evaluation_uses_precheck_resolver_before_speed_read(monkeypatch):
    clear_verified_game_visual_settings()
    _publish_verified_game_visual_settings(
        note_skin_type=7,
        tap_effect=5,
        judgement_assist_effect=False,
    )
    calls = []
    settings = SimpleNamespace(
        note_speed=5.0,
        profile_path=SimpleNamespace(name="expert.json"),
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action.RealtimeProfileStore.runtime_options",
        lambda _store: {
            "note_skin_type": 1,
            "tap_effect": 1,
            "judgement_assist_effect": True,
        },
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action.RealtimeProfileStore.resolve_latest_for_visual_evaluation_environment",
        lambda _store, **kwargs: calls.append(kwargs) or settings,
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action.RealtimeProfileStore.resolve_latest_for_environment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("strict resolver must not run during evaluation precheck")
        ),
    )

    speed, profile = _expected_speed(
        SimpleNamespace(),
        {
            "difficulty": "Expert",
            "require_profile": True,
            "visual_evaluation": True,
        },
        np.zeros((720, 1280, 3), dtype=np.uint8),
    )

    assert (speed, profile) == (5.0, "expert.json")
    signature = calls[0]["current_signature"]
    assert signature.note_speed == 1.0
    assert signature.note_skin_type == 7
    assert signature.tap_effect == 5
    assert signature.judgement_assist_effect is False


def test_gate_reads_current_speed_and_uses_real_half_tenth_cent_buttons(monkeypatch):
    clear_verified_settings()
    clicks = []
    readings = iter([2.37, 5.0])
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action._expected_speed",
        lambda context, params, image: (5.0, "expert.json"),
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action._read_speed",
        lambda image, roi: next(readings),
        raising=False,
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action._click",
        lambda controller, point: clicks.append(point),
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action.time.sleep",
        lambda seconds: None,
    )
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=_Controller()),
    )

    assert RealtimePerformanceSettingsGate()._run(context, {
        "difficulty": "Expert",
        "require_profile": True,
    })

    # Gear first, then the first 演出设定 tab explicitly (the game remembers
    # the last-used tab), never the second tab. Then +2.63.
    assert clicks[:2] == [(960, 650), (297, 155)]
    assert (430, 155) not in clicks
    assert clicks.count((635, 312)) == 5  # +0.50
    assert clicks.count((575, 312)) == 1  # +0.10
    assert clicks.count((513, 312)) == 3  # +0.01
    assert clicks[-1] == (640, 600)
    verified = verified_settings("Expert")
    assert verified is not None
    assert verified.actual_note_speed == 5.0
    assert verified.profile == "expert.json"


def test_gate_rejects_speed_outside_game_range(monkeypatch):
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action._expected_speed",
        lambda context, params, image: (12.01, None),
    )
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=_Controller()),
    )

    try:
        RealtimePerformanceSettingsGate()._run(context, {"difficulty": "Easy"})
    except ValueError as exc:
        assert "12.01" in str(exc)
    else:
        raise AssertionError("expected an out-of-range speed to be rejected")


def test_gate_never_blindly_decrements_across_wrapping_minimum(monkeypatch):
    clear_verified_settings()
    clicks = []
    readings = iter([1.0, 1.0])
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action._expected_speed",
        lambda context, params, image: (1.0, None),
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action._read_speed",
        lambda image, roi: next(readings),
        raising=False,
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action._click",
        lambda controller, point: clicks.append(point),
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action.time.sleep",
        lambda seconds: None,
    )
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=_Controller()),
    )

    assert RealtimePerformanceSettingsGate()._run(context, {
        "difficulty": "Easy",
    })

    assert clicks.count((207, 312)) == 0
    assert clicks.count((268, 312)) == 0
    assert clicks.count((330, 312)) == 0
    assert verified_settings("Easy").actual_note_speed == 1.0


def _patch_gate_io(monkeypatch, readings, clicks, expected=2.0):
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action._expected_speed",
        lambda context, params, image: (expected, None),
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action._read_speed",
        lambda image, roi: next(readings),
        raising=False,
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action._click",
        lambda controller, point: clicks.append(point),
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action.time.sleep",
        lambda seconds: None,
    )


def test_gate_replans_from_fresh_reading_after_dropped_clicks(monkeypatch):
    clear_verified_settings()
    clicks = []
    # 5.00 -> 2.00 needs -3.00. Two -0.50 clicks get dropped, so the first
    # reread shows 3.00; the closed loop must click -1.00 more.
    readings = iter([5.0, 3.0, 2.0])
    _patch_gate_io(monkeypatch, readings, clicks, expected=2.0)
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=_Controller()),
    )

    assert RealtimePerformanceSettingsGate()._run(context, {
        "difficulty": "Expert",
    })

    assert clicks.count((207, 312)) == 6 + 2  # -0.50 x6, then -0.50 x2
    assert verified_settings("Expert").actual_note_speed == 2.0


def test_gate_blocks_when_clicks_have_no_effect(monkeypatch):
    clear_verified_settings()
    clicks = []
    readings = iter([5.0] * 20)
    _patch_gate_io(monkeypatch, readings, clicks, expected=2.0)
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=_Controller()),
    )

    try:
        RealtimePerformanceSettingsGate()._run(context, {"difficulty": "Easy"})
    except RuntimeError as exc:
        assert "未生效" in str(exc) or "未收敛" in str(exc)
    else:
        raise AssertionError("expected the gate to block on dead buttons")


def test_gate_blocks_instead_of_blind_clicking_when_unreadable(monkeypatch):
    clear_verified_settings()
    clicks = []
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action._expected_speed",
        lambda context, params, image: (2.0, None),
    )

    def _always_fail(image, roi):
        raise RuntimeError("glyph unreadable")

    monkeypatch.setattr(
        "agent.realtime.performance_settings_action._read_speed",
        _always_fail,
        raising=False,
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action._click",
        lambda controller, point: clicks.append(point),
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action.time.sleep",
        lambda seconds: None,
    )
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=_Controller()),
    )

    try:
        RealtimePerformanceSettingsGate()._run(context, {"difficulty": "Easy"})
    except RuntimeError as exc:
        assert "不可识别" in str(exc)
    else:
        raise AssertionError("expected the gate to block on unreadable digits")
    assert clicks.count((207, 312)) == 0


def test_gate_reclicks_first_tab_when_the_initial_tab_switch_is_dropped(
    monkeypatch,
):
    clear_verified_settings()
    clicks = []
    attempts = iter([
        RuntimeError("wrong settings tab"),
        RuntimeError("wrong settings tab"),
        RuntimeError("wrong settings tab"),
        2.0,
    ])
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action._expected_speed",
        lambda context, params, image: (2.0, None),
    )

    def _read_after_retry(_image, _roi):
        value = next(attempts)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(
        "agent.realtime.performance_settings_action._read_speed",
        _read_after_retry,
        raising=False,
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action._click",
        lambda controller, point: clicks.append(point),
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action.time.sleep",
        lambda seconds: None,
    )
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=_Controller()),
    )

    assert RealtimePerformanceSettingsGate()._run(context, {
        "difficulty": "Easy",
    })
    assert clicks.count(DEFAULT_COORDINATES["first_tab"]) == 2
    assert verified_settings("Easy").actual_note_speed == 2.0
