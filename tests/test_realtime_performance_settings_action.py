from __future__ import annotations

import json
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from agent.realtime import performance_settings_action
from agent.realtime.live_session import reset_live_run, update_live_run
from agent.realtime.performance_settings_action import (
    DEFAULT_COORDINATES,
    RealtimePerformanceSettingsGate,
    _digit_templates,
    _expected_speed,
    _close_settings_dialog,
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


@pytest.fixture(autouse=True)
def _isolate_native_prearm(monkeypatch):
    monkeypatch.setattr(
        performance_settings_action,
        "prepare_native_for_settings_gate",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        performance_settings_action,
        "discard_prearmed_backend",
        lambda reason: False,
    )


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
    prearm_calls = []
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

    def prepare_prearm(**kwargs):
        # 预武装只能发生在设置弹窗确认关闭之后。
        assert clicks[-1] == DEFAULT_COORDINATES["close"]
        prearm_calls.append(kwargs)

    monkeypatch.setattr(
        performance_settings_action,
        "prepare_native_for_settings_gate",
        prepare_prearm,
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
    assert prearm_calls[0]["difficulty"] == "Expert"
    verified = verified_settings("Expert")
    assert verified is not None
    assert verified.actual_note_speed == 5.0
    assert verified.profile == "expert.json"


def test_gate_fails_closed_when_native_prearm_fails_after_dialog_close(
    monkeypatch,
):
    clear_verified_settings()
    closed = []
    monkeypatch.setattr(
        performance_settings_action,
        "_expected_speed",
        lambda context, params, image: (5.0, "expert.json"),
    )
    monkeypatch.setattr(
        performance_settings_action,
        "_select_first_tab_and_read",
        lambda *args, **kwargs: 5.0,
    )
    monkeypatch.setattr(
        performance_settings_action,
        "_adjust_speed",
        lambda *args, **kwargs: (True, 5.0),
    )
    monkeypatch.setattr(
        performance_settings_action,
        "_click",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        performance_settings_action,
        "_close_settings_dialog",
        lambda *args, **kwargs: closed.append(True),
    )

    def fail_prearm(**kwargs):
        assert closed == [True]
        raise RuntimeError("simulated prearm failure")

    monkeypatch.setattr(
        performance_settings_action,
        "prepare_native_for_settings_gate",
        fail_prearm,
    )
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=_Controller()),
    )

    with pytest.raises(RuntimeError, match="simulated prearm failure"):
        RealtimePerformanceSettingsGate()._run(
            context,
            {"difficulty": "Expert", "require_profile": True},
        )


def test_gate_can_defer_native_prearm_until_final_cover(monkeypatch):
    clear_verified_settings()
    monkeypatch.setattr(
        performance_settings_action,
        "_expected_speed",
        lambda context, params, image: (5.0, "expert.json"),
    )
    monkeypatch.setattr(
        performance_settings_action,
        "_select_first_tab_and_read",
        lambda *args, **kwargs: 5.0,
    )
    monkeypatch.setattr(
        performance_settings_action,
        "_adjust_speed",
        lambda *args, **kwargs: (True, 5.0),
    )
    monkeypatch.setattr(
        performance_settings_action,
        "_click",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        performance_settings_action,
        "_close_settings_dialog",
        lambda *args, **kwargs: None,
    )
    prepared = []
    discarded = []
    monkeypatch.setattr(
        performance_settings_action,
        "prepare_native_for_settings_gate",
        lambda **kwargs: prepared.append(kwargs),
    )
    monkeypatch.setattr(
        performance_settings_action,
        "discard_prearmed_backend",
        discarded.append,
    )
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=_Controller()),
    )

    assert RealtimePerformanceSettingsGate()._run(context, {
        "difficulty": "Expert",
        "require_profile": True,
        "defer_native_prearm": True,
    })
    assert prepared == []
    assert discarded == ["deferred-until-final-cover"]


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
    assert clicks.count(DEFAULT_COORDINATES["close"]) == 2
    assert verified_settings("Easy").actual_note_speed == 1.0


def test_settings_close_retries_until_speed_display_disappears(monkeypatch):
    clicks = []
    readings = iter([5.0, RuntimeError("speed display absent")])
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action._click",
        lambda controller, point: clicks.append(point),
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action._read_speed",
        lambda image, roi: (
            (_ for _ in ()).throw(value) if isinstance(value := next(readings), Exception)
            else value
        ),
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action.time.sleep",
        lambda seconds: None,
    )

    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=_Controller()),
    )

    _close_settings_dialog(
        context,
        context.tasker.controller,
        DEFAULT_COORDINATES,
        attempts=3,
        delay_seconds=0,
    )

    assert clicks == [
        DEFAULT_COORDINATES["close"],
        DEFAULT_COORDINATES["close"],
    ]


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


def test_settings_gate_failure_writes_structured_preflight_result(
    monkeypatch, tmp_path,
):
    clear_verified_settings()
    clear_verified_game_visual_settings()
    _publish_verified_game_visual_settings(
        note_skin_type=6,
        tap_effect=4,
        judgement_assist_effect=False,
    )
    reset_live_run(
        mode="realtime",
        difficulty="Expert",
        expected_note_speed=5.0,
        actual_note_speed=8.88,
        prepared_for_play=True,
    )
    update_live_run(
        song_id="song-phash-v1-0011223344556677",
        song_id_method="song-phash-v1",
    )
    monkeypatch.setattr(performance_settings_action, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        RealtimePerformanceSettingsGate,
        "_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("speed readback failed")
        ),
    )
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Expert",
        "require_profile": True,
        "dpi": 240,
        "game_fps": 60,
        "render_quality": "standard",
        "visual_evaluation": True,
    }))

    assert RealtimePerformanceSettingsGate().run(context, argv) is False

    reports = list((tmp_path / "screencap").glob("realtime-result-*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["valid"] is False
    assert payload["result_status"] == "preflight_error"
    assert payload["terminal_stage"] == "performance_settings_gate"
    assert payload["run_id"] == payload["session"]["run_id"]
    assert payload["song_id"] == "song-phash-v1-0011223344556677"
    assert payload["mode"] == "visual-evaluation"
    assert payload["settings"] == {
        "expected_note_speed": 5.0,
        "actual_note_speed": None,
        "note_skin_type": 6,
        "tap_effect": 4,
        "judgement_assist": False,
    }
    assert payload["processed_frames"] == 0
    assert payload["dispatched_actions"] == 0
    assert payload["debug_recording_path"] is None
    assert payload["eligible_for_profile_acceptance"] is False
    assert not list(tmp_path.rglob("summary.json"))


def test_settings_readback_failure_keeps_expected_profile_snapshot(
    monkeypatch, tmp_path,
):
    clear_verified_settings()
    clear_verified_game_visual_settings()
    _publish_verified_game_visual_settings(
        note_skin_type=3,
        tap_effect=2,
        judgement_assist_effect=True,
    )
    live_run = reset_live_run(
        mode="challenge",
        difficulty="Expert",
        expected_note_speed=None,
        actual_note_speed=None,
        prepared_for_play=True,
    )
    update_live_run(
        song_id="song-phash-v1-8899aabbccddeeff",
        song_id_method="song-phash-v1",
    )
    monkeypatch.setattr(performance_settings_action, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        performance_settings_action,
        "_expected_speed",
        lambda *_args, **_kwargs: (5.0, "expert-accepted.json"),
    )
    def fail_readback(*_args, **_kwargs):
        reset_live_run(
            mode="formal",
            difficulty="Easy",
            prepared_for_play=True,
        )
        raise RuntimeError("speed digits unreadable")

    monkeypatch.setattr(
        performance_settings_action, "_read_speed", fail_readback,
    )
    monkeypatch.setattr(
        performance_settings_action, "_click", lambda *_args: None,
    )
    monkeypatch.setattr(performance_settings_action.time, "sleep", lambda _s: None)
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=_Controller()),
    )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Expert",
        "require_profile": True,
        "run_mode": "challenge",
        "first_tab_attempts": 1,
    }))

    assert RealtimePerformanceSettingsGate().run(context, argv) is False

    report = next((tmp_path / "screencap").glob("realtime-result-*.json"))
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["run_id"] == live_run.run_id
    assert payload["song_id"] == "song-phash-v1-8899aabbccddeeff"
    assert payload["mode"] == "challenge"
    assert payload["profile"] == "expert-accepted.json"
    assert payload["settings"]["expected_note_speed"] == 5.0
    assert payload["settings"]["actual_note_speed"] is None


def test_settings_gate_stop_during_failure_is_neutral_and_writes_nothing(
    monkeypatch, tmp_path,
):
    reset_live_run(mode="realtime", difficulty="Expert")
    monkeypatch.setattr(performance_settings_action, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        RealtimePerformanceSettingsGate,
        "_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("controller stopped")
        ),
    )

    class Tasker:
        reads = 0

        @property
        def stopping(self):
            self.reads += 1
            return self.reads >= 2

    context = SimpleNamespace(tasker=Tasker())
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Expert",
    }))

    assert RealtimePerformanceSettingsGate().run(context, argv) is True
    assert not list(tmp_path.rglob("realtime-result-*.json"))
