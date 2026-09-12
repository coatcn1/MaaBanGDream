from __future__ import annotations

import json
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from agent.realtime import performance_settings_action
from agent.realtime.live_session import (
    current_live_run,
    reset_live_run,
    update_live_run,
)
from agent.realtime.performance_settings_action import (
    DEFAULT_COORDINATES,
    RealtimePerformanceSettingsGate,
    _adjust_speed,
    _digit_templates,
    _expected_speed,
    _close_settings_dialog,
    _read_speed,
    _read_speed_stable,
    _select_first_tab_and_read,
    _speed_click_plan,
    clear_verified_settings,
    require_special_chart_for_settings_gate,
    verified_settings,
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
        performance_settings_action, "_ACTIVE_SPEED_TARGET", None
    )
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
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action.RealtimeProfileStore.runtime_options",
        lambda _store: {"note_speed_settings_enabled": True},
    )


def test_speed_gate_uses_current_task_home_result_without_opening_dialog(monkeypatch):
    clear_verified_settings()
    clicks = []
    options = {"note_speed_settings_enabled": True}
    target = performance_settings_action._speed_settings_target(note_speed=5.0)
    monkeypatch.setattr(
        performance_settings_action,
        "_expected_speed",
        lambda context, params, image: (5.0, "expert.json"),
    )
    monkeypatch.setattr(
        performance_settings_action.RealtimeProfileStore,
        "runtime_options",
        lambda _store: options,
    )
    monkeypatch.setattr(
        performance_settings_action,
        "_ACTIVE_SPEED_TARGET",
        target,
    )
    monkeypatch.setattr(
        performance_settings_action,
        "_click",
        lambda _controller, point: clicks.append(point),
    )
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=_Controller()),
    )

    assert RealtimePerformanceSettingsGate()._run(
        context,
        {"difficulty": "Expert", "require_profile": True},
    ) is True

    assert clicks == []
    assert verified_settings("Expert").actual_note_speed == 5.0


def test_speed_gate_never_opens_dialog_when_task_home_result_is_missing(monkeypatch):
    clear_verified_settings()
    options = {"note_speed_settings_enabled": True}
    monkeypatch.setattr(
        performance_settings_action,
        "_expected_speed",
        lambda context, params, image: (5.0, "expert.json"),
    )
    monkeypatch.setattr(
        performance_settings_action.RealtimeProfileStore,
        "runtime_options",
        lambda _store: options,
    )
    monkeypatch.setattr(
        performance_settings_action,
        "_click",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("准备页不得再打开设置弹窗")
        ),
    )
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=_Controller()),
    )

    with pytest.raises(RuntimeError, match="必须先在主页完成流速校验"):
        RealtimePerformanceSettingsGate()._run(
            context,
            {"difficulty": "Expert", "require_profile": True},
        )


def test_enabled_gate_fails_closed_when_home_gate_was_not_run(monkeypatch):
    clear_verified_settings()
    clicks = []
    options = {"note_speed_settings_enabled": True}
    monkeypatch.setattr(
        performance_settings_action,
        "_expected_speed",
        lambda context, params, image: (5.0, "expert.json"),
    )
    monkeypatch.setattr(
        performance_settings_action.RealtimeProfileStore,
        "runtime_options",
        lambda _store: options,
    )
    monkeypatch.setattr(
        performance_settings_action,
        "_click",
        lambda _controller, point: clicks.append(point),
    )
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=_Controller()),
    )

    with pytest.raises(RuntimeError, match="必须先在主页"):
        RealtimePerformanceSettingsGate()._run(
            context,
            {"difficulty": "Expert", "require_profile": True},
        )

    assert clicks == []


def test_special_chart_gate_rejects_missing_reliable_local_chart(monkeypatch):
    reset_live_run(
        mode="challenge",
        difficulty="Special",
        requested_difficulty="Special",
        prepared_for_play=True,
    )
    update_live_run(
        song_id="unknown",
        song_level=30,
        song_title="Special Song",
    )

    class Repository:
        def resolve(self, *_args, **_kwargs):
            return SimpleNamespace(
                selection=None,
                reason="no local special chart for confirmed song",
            )

    monkeypatch.setattr(
        performance_settings_action,
        "LocalChartRepository",
        lambda _root: Repository(),
    )

    with pytest.raises(RuntimeError, match="禁止按视觉回退开演"):
        require_special_chart_for_settings_gate("Special")


def test_special_chart_gate_accepts_exact_special_selection(monkeypatch):
    reset_live_run(
        mode="cooperative",
        difficulty="Special",
        requested_difficulty="Special",
        prepared_for_play=True,
    )
    update_live_run(
        song_id="unknown",
        song_level=30,
        song_title="Special Song",
    )
    selection = SimpleNamespace(
        bestdori_song_id=30,
        difficulty="special",
    )

    class Repository:
        def resolve(self, *_args, **_kwargs):
            return SimpleNamespace(selection=selection, reason="confirmed")

    monkeypatch.setattr(
        performance_settings_action,
        "LocalChartRepository",
        lambda _root: Repository(),
    )

    assert require_special_chart_for_settings_gate("Special") is selection


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


def test_speed_adjustment_uses_real_half_tenth_cent_buttons(monkeypatch):
    clicks = []
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action.time.sleep",
        lambda seconds: None,
    )
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False),
    )

    completed, confirmed = _adjust_speed(
        context,
        None,
        DEFAULT_COORDINATES,
        2.37,
        5.0,
        button_delay_seconds=0,
        settle_delay_seconds=0,
        round_limit=3,
        read_current=lambda: 5.0,
        click_point=clicks.append,
    )

    assert completed is True
    assert confirmed == 5.0
    assert clicks.count((635, 312)) == 5  # +0.50
    assert clicks.count((575, 312)) == 1  # +0.10
    assert clicks.count((513, 312)) == 3  # +0.01


def test_gate_skips_speed_read_when_game_effect_settings_disabled(monkeypatch):
    monkeypatch.setenv("MAABANGDREAM_ORDERED_STARTUP", "1")
    clear_verified_settings()
    clicks = []
    prepared = []
    events = []
    monkeypatch.setattr(
        "agent.realtime.preparation_identity.confirm_preparation_identity",
        lambda image, difficulty: events.append("identity"),
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action._expected_speed",
        lambda context, params, image: (5.0, "expert.json"),
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action._click",
        lambda controller, point: clicks.append(point),
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action.RealtimeProfileStore.runtime_options",
        lambda _store: {"note_speed_settings_enabled": False},
    )
    monkeypatch.setattr(
        performance_settings_action,
        "prepare_native_for_settings_gate",
        lambda **kwargs: (events.append("prearm"), prepared.append(kwargs)),
    )
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=_Controller()),
    )
    assert RealtimePerformanceSettingsGate()._run(context, {
        "difficulty": "Expert",
        "require_profile": True,
        "confirm_preparation_identity": True,
    })
    assert events == ["identity", "prearm"]
    # 关闭演出特效设置后整类跳过：不打开齿轮，不读流速。
    assert clicks == []
    # 但 Native 预武装仍必须生成，否则单人正式演奏会零输入失败。
    assert len(prepared) == 1
    assert prepared[0]["difficulty"] == "Expert"
    verified = verified_settings("Expert")
    assert verified is not None
    assert verified.actual_note_speed == 5.0
    assert verified.expected_note_speed == 5.0


def test_gate_uses_confirmed_expert_when_special_button_was_unavailable(
    monkeypatch,
):
    clear_verified_settings()
    reset_live_run(
        mode="formal",
        difficulty="Expert",
        requested_difficulty="Special",
        prepared_for_play=True,
    )
    identity_difficulties = []
    expected_params = []
    prearm_calls = []
    monkeypatch.setattr(
        "agent.realtime.preparation_identity.confirm_preparation_identity",
        lambda image, difficulty: identity_difficulties.append(difficulty),
    )
    monkeypatch.setattr(
        performance_settings_action,
        "_expected_speed",
        lambda context, params, image: (
            expected_params.append(dict(params)) or (5.0, "expert.json")
        ),
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action.RealtimeProfileStore.runtime_options",
        lambda _store: {"note_speed_settings_enabled": False},
    )
    monkeypatch.setattr(
        performance_settings_action,
        "prepare_native_for_settings_gate",
        lambda **kwargs: prearm_calls.append(kwargs),
    )
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=_Controller()),
    )

    assert RealtimePerformanceSettingsGate()._run(context, {
        "difficulty": "Special",
        "require_profile": True,
        "confirm_preparation_identity": True,
    })

    assert identity_difficulties == ["Expert"]
    assert expected_params[0]["difficulty"] == "Expert"
    assert prearm_calls[0]["difficulty"] == "Expert"
    assert verified_settings("Special") is None
    assert verified_settings("Expert") is not None
    run = current_live_run()
    assert run is not None
    assert run.requested_difficulty == "Special"
    assert run.difficulty == "Expert"


def test_skipped_gate_still_defers_native_prearm_when_requested(monkeypatch):
    clear_verified_settings()
    discarded = []
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action._expected_speed",
        lambda context, params, image: (5.0, "expert.json"),
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action.RealtimeProfileStore.runtime_options",
        lambda _store: {"note_speed_settings_enabled": False},
    )
    monkeypatch.setattr(
        performance_settings_action,
        "discard_prearmed_backend",
        discarded.append,
    )
    monkeypatch.setattr(
        performance_settings_action,
        "prepare_native_for_settings_gate",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("deferred 流程不得在此预武装")
        ),
    )
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=_Controller()),
    )

    assert RealtimePerformanceSettingsGate()._run(context, {
        "difficulty": "Expert",
        "require_profile": True,
        "defer_native_prearm": True,
    })
    assert discarded == ["deferred-until-final-cover"]


def test_skipped_gate_caches_fresh_cooperative_preparation_image(monkeypatch):
    clear_verified_settings()
    frame = np.full((720, 1280, 3), 37, dtype=np.uint8)

    class Controller:
        def post_screencap(self):
            return SimpleNamespace(
                wait=lambda: SimpleNamespace(get=lambda: frame),
            )

    reset_live_run(
        mode="cooperative",
        difficulty="Expert",
        prepared_for_play=True,
    )
    monkeypatch.setattr(
        performance_settings_action,
        "_expected_speed",
        lambda context, params, image: (5.0, "expert.json"),
    )
    monkeypatch.setattr(
        "agent.realtime.performance_settings_action.RealtimeProfileStore.runtime_options",
        lambda _store: {"note_speed_settings_enabled": False},
    )
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=Controller()),
    )

    assert RealtimePerformanceSettingsGate()._run(context, {
        "difficulty": "Expert",
        "require_profile": True,
        "defer_native_prearm": True,
        "cache_preparation_image": True,
    })

    run = current_live_run()
    assert run is not None
    assert run.cooperative_prestart_image is not frame
    assert np.array_equal(run.cooperative_prestart_image, frame)


def test_gate_fails_closed_when_native_prearm_fails_after_home_result(
    monkeypatch,
):
    clear_verified_settings()
    options = {"note_speed_settings_enabled": True}
    target = performance_settings_action._speed_settings_target(note_speed=5.0)
    monkeypatch.setattr(
        performance_settings_action.RealtimeProfileStore,
        "runtime_options",
        lambda _store: options,
    )
    monkeypatch.setattr(performance_settings_action, "_ACTIVE_SPEED_TARGET", target)
    monkeypatch.setattr(
        performance_settings_action,
        "_expected_speed",
        lambda context, params, image: (5.0, "expert.json"),
    )

    def fail_prearm(**kwargs):
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
    options = {"note_speed_settings_enabled": True}
    target = performance_settings_action._speed_settings_target(note_speed=5.0)
    monkeypatch.setattr(
        performance_settings_action.RealtimeProfileStore,
        "runtime_options",
        lambda _store: options,
    )
    monkeypatch.setattr(performance_settings_action, "_ACTIVE_SPEED_TARGET", target)
    monkeypatch.setattr(
        performance_settings_action,
        "_expected_speed",
        lambda context, params, image: (5.0, "expert.json"),
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


def test_speed_adjustment_never_decrements_across_wrapping_minimum(monkeypatch):
    clicks = []
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False),
    )

    completed, confirmed = _adjust_speed(
        context,
        None,
        DEFAULT_COORDINATES,
        1.0,
        1.0,
        button_delay_seconds=0,
        settle_delay_seconds=0,
        round_limit=3,
        read_current=lambda: 1.0,
        click_point=clicks.append,
    )

    assert completed is True
    assert confirmed == 1.0
    assert clicks == []


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


def test_speed_adjustment_replans_after_dropped_clicks(monkeypatch):
    clicks = []
    # 5.00 调到 2.00 共需 -3.00；丢失两次 -0.50 点击后首次读回为 3.00，
    # 闭环必须据此重新规划并补点 -1.00。
    readings = iter([3.0, 2.0])
    monkeypatch.setattr(performance_settings_action.time, "sleep", lambda _: None)
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False),
    )

    completed, confirmed = _adjust_speed(
        context,
        None,
        DEFAULT_COORDINATES,
        5.0,
        2.0,
        button_delay_seconds=0,
        settle_delay_seconds=0,
        round_limit=3,
        read_current=lambda: next(readings),
        click_point=clicks.append,
    )

    assert completed is True
    assert confirmed == 2.0
    assert clicks.count((207, 312)) == 6 + 2  # -0.50 x6, then -0.50 x2


def test_speed_adjustment_blocks_when_clicks_have_no_effect(monkeypatch):
    clicks = []
    monkeypatch.setattr(performance_settings_action.time, "sleep", lambda _: None)
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False),
    )

    with pytest.raises(RuntimeError, match="未生效|未收敛"):
        _adjust_speed(
            context,
            None,
            DEFAULT_COORDINATES,
            5.0,
            2.0,
            button_delay_seconds=0,
            settle_delay_seconds=0,
            round_limit=3,
            read_current=lambda: 5.0,
            click_point=clicks.append,
        )


def test_first_tab_read_blocks_instead_of_blind_speed_clicking(monkeypatch):
    clicks = []
    def always_fail():
        raise RuntimeError("glyph unreadable")

    with pytest.raises(RuntimeError, match="仍不可读取"):
        _select_first_tab_and_read(
            None,
            DEFAULT_COORDINATES,
            always_fail,
            attempts=2,
            settle_delay_seconds=0,
            click_point=clicks.append,
        )

    assert clicks == [DEFAULT_COORDINATES["first_tab"]] * 2


def test_first_tab_read_reclicks_when_initial_switch_is_dropped(
    monkeypatch,
):
    clicks = []
    attempts = iter([
        RuntimeError("wrong settings tab"),
        RuntimeError("wrong settings tab"),
        RuntimeError("wrong settings tab"),
        2.0,
    ])
    def read_after_retry():
        value = next(attempts)
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(performance_settings_action.time, "sleep", lambda _: None)

    assert _select_first_tab_and_read(
        None,
        DEFAULT_COORDINATES,
        read_after_retry,
        attempts=2,
        settle_delay_seconds=0,
        click_point=clicks.append,
    ) == 2.0
    assert clicks.count(DEFAULT_COORDINATES["first_tab"]) == 2


def test_settings_gate_failure_writes_structured_preflight_result(
    monkeypatch, tmp_path,
):
    clear_verified_settings()
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
    assert payload["mode"] == "formal"
    assert payload["settings"]["expected_note_speed"] == 5.0
    assert payload["settings"]["actual_note_speed"] is None
    assert payload["processed_frames"] == 0
    assert payload["dispatched_actions"] == 0
    assert payload["debug_recording_path"] is None
    assert payload["eligible_for_profile_acceptance"] is False
    assert not list(tmp_path.rglob("summary.json"))


def test_settings_readback_failure_keeps_expected_profile_snapshot(
    monkeypatch, tmp_path,
):
    clear_verified_settings()
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
