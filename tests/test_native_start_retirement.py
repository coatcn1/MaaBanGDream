import json
from types import SimpleNamespace

import numpy as np
import pytest

from agent.realtime.native_play import LANE_CENTERS, TOUCH_Y, NativeMinitouchBackend, NativeStartPhotogate
from agent.realtime.native_prearm import frozen_native_runtime_options
from agent.realtime.profile_store import RealtimeProfileStore


@pytest.mark.parametrize("legacy_value", [True, False, "legacy", None])
def test_retired_setting_does_not_prevent_loading_or_rewrite_profile(tmp_path, legacy_value):
    state = {"version": 1, "pinned": {"Expert": "accepted-expert.json"}, "runtime_options": {
        "native_early_start_enabled": legacy_value,
        "native_life_feedback_enabled": True,
        "native_wait_jitter_filter_enabled": True,
        "skip_result_check": True,
    }}
    path = tmp_path / "selection.json"
    original = json.dumps(state)
    path.write_text(original, encoding="utf-8")
    store = RealtimeProfileStore(tmp_path)
    options = store.runtime_options()
    assert "native_early_start_enabled" not in options
    assert options["native_life_feedback_enabled"] and options["native_wait_jitter_filter_enabled"]
    assert options["skip_result_check"]
    assert path.read_text(encoding="utf-8") == original
    store.update_runtime_options(options)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["pinned"] == state["pinned"]
    assert "native_early_start_enabled" not in saved["runtime_options"]


def test_retired_setting_in_old_in_memory_snapshot_cannot_reenable_experiment():
    old = SimpleNamespace(run_id="old-run", native_runtime_options={
        "native_early_start_enabled": True, "native_life_feedback_enabled": True,
    })
    frozen = frozen_native_runtime_options(old, {"native_early_start_enabled": True})
    assert "native_early_start_enabled" not in frozen
    assert frozen["native_life_feedback_enabled"]
    assert old.native_runtime_options["native_early_start_enabled"] is True
    assert "native_early_start_enabled" not in frozen_native_runtime_options(None, {
        "native_early_start_enabled": True,
    })


@pytest.mark.parametrize("valid_geometry", [False, True])
def test_environment_configuration_preserves_clock_recovery_without_starting_upper_gate(valid_geometry):
    backend = object.__new__(NativeMinitouchBackend)
    backend._config = {"judgement_y": TOUCH_Y if valid_geometry else 565,
                       "lane_centers": LANE_CENTERS}
    environment = object()
    backend._photogate = NativeStartPhotogate(mode="cooperative-playfield-confirmed")
    backend.configure_environment(environment)
    assert backend._game_clock_environment is (environment if valid_geometry else None)
    assert not hasattr(backend._photogate, "configure_early_start")
    assert backend._photogate.report()["photogate_popup_min_panel_fill"] == .75


def test_upper_only_motion_cannot_trigger_default_first_note():
    gate = NativeStartPhotogate(mode="cooperative-playfield-confirmed", stable_duration_ms=0,
                               grace_ms=0, playfield_detector=lambda _: True)
    baseline = np.zeros((720, 1280, 3), np.uint8)
    gate.observe(baseline, 1.)
    gate.observe(baseline, 1.01)
    upper = baseline.copy()
    upper[190:211, 450:480] = 255
    upper[190:211, 800:830] = 255
    assert gate.observe(upper, 1.02) is None
    assert gate.frozen and not gate.triggered
    first_note = upper.copy()
    first_note[520:526, 170:1110] = 255
    assert gate.observe(first_note, 1.03) is not None
    assert gate.trigger_source == "interpolated-threshold-crossing"
