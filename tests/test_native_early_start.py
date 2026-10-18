from dataclasses import replace
import numpy as np
import pytest

from agent.realtime.early_start_gate import (
    EarlyStartGate,
    CALIBRATED_ENVIRONMENT,
    first_group_topology,
)
from agent.realtime.native_play import NativeStartPhotogate, NativeMinitouchBackend
from agent.realtime import runtime_flags
from types import SimpleNamespace


def actions():
    return [
        dict(kind="flick", lane=lane, due_s=3.0, note_index=i)
        for i, lane in enumerate((0, 6))
    ] + [dict(kind="tap", lane=3, due_s=3.5, note_index=2)]


def test_only_calibrated_environment_and_first_group_are_eligible():
    assert EarlyStartGate(CALIBRATED_ENVIRONMENT, actions()).eligible
    assert not EarlyStartGate(
        replace(CALIBRATED_ENVIRONMENT, note_speed=6), actions()
    ).eligible
    assert not EarlyStartGate(None, actions()).eligible
    bad = actions()
    bad[0]["kind"] = "down"
    assert first_group_topology(bad)["supported"] is False


def test_crossing_owns_upper_evidence_and_delay_already_contains_190():
    gate = EarlyStartGate(CALIBRATED_ENVIRONMENT, actions())
    gate.detect = lambda _: dict(
        y=180.0,
        normalized_span=180.0,
        centers=[500.0, 780.0],
        group_extent=[175, 185],
        lanes=[0, 6],
    )
    image = np.zeros((720, 1280, 3), np.uint8)
    assert gate.observe(image, 10.0, dict(sequence=1)) is None
    gate.detect = lambda _: dict(
        y=220.0,
        normalized_span=180.0,
        centers=[490.0, 790.0],
        group_extent=[215, 225],
        lanes=[0, 6],
    )
    assert gate.observe(image, 10.2, dict(sequence=2)) == pytest.approx(
        10.1 + 0.89487408445
    )
    assert gate.evidence["roi"] == [0, 100, 1280, 201]
    assert gate.rois[0].shape == (201, 1280, 3)


def test_missing_head_or_first_observation_beyond_line_cannot_reacquire_later_pair():
    image = np.zeros((720, 1280, 3), np.uint8)
    gate = EarlyStartGate(CALIBRATED_ENVIRONMENT, actions())
    gate.detect = lambda _: dict(y=220.0, normalized_span=180.0)
    assert gate.observe(image, 10.0, {}) is None
    gate.detect = lambda _: dict(y=180.0, normalized_span=180.0)
    assert gate.observe(image, 11.0, {}) is None
    assert gate.rejected_reason == "first-head-already-past-line"


def test_duplicate_sequence_does_not_mutate_upper_tracking():
    gate = EarlyStartGate(CALIBRATED_ENVIRONMENT, actions())
    gate.detect = lambda _: dict(y=180.0, normalized_span=180.0)
    image = np.zeros((720, 1280, 3), np.uint8)
    gate.observe(image, 10.0, dict(sequence=1))
    prior = gate.previous
    gate.observe(image, 11.0, dict(sequence=1, reused=True))
    assert gate.previous is prior


def configured_gate():
    gate = NativeStartPhotogate(
        mode="cooperative-playfield-confirmed",
        stable_duration_ms=120,
        grace_ms=500,
        playfield_detector=lambda _: True,
        popup_detector=lambda _: False,
    )
    gate.configure_early_start(CALIBRATED_ENVIRONMENT, actions())
    gate._early_gate.detect = lambda _: None
    return gate


def test_life_noise_unreadable_and_real_drop_have_distinct_behavior():
    gate = configured_gate()
    reading = SimpleNamespace(visible=True, value=1000)
    gate._life_detector = SimpleNamespace(detect=lambda _: reading)
    image = np.zeros((720, 1280, 3), np.uint8)
    gate.observe(image, 10.0)
    reading.value = 995
    changed = image.copy()
    changed[510:536, :200] = 50
    assert gate.observe(changed, 10.02) is None
    reading.visible = False
    reading.value = 0
    assert gate.observe(image, 10.04) is None
    reading.visible = True
    reading.value = 960
    assert gate.observe(changed, 10.06) == pytest.approx(10.06 + 0.190)
    assert gate.trigger_source == "life-drop-current-frame"
    assert gate.report()["photogate_life_trigger"]["confidence"].startswith("low")
    assert gate._trigger_evidence is None


def test_grace_life_drop_uses_observed_candidate_not_current_frame():
    gate = configured_gate()
    reading = SimpleNamespace(visible=True, value=1000)
    gate._life_detector = SimpleNamespace(detect=lambda _: reading)
    image = np.zeros((720, 1280, 3), np.uint8)
    gate.observe(image, 10.0)
    gate.observe(image, 10.13)
    assert gate.frozen
    note = image.copy()
    note[510:536, :200] = 50
    gate.observe(note, 10.15)
    reading.value = 960
    assert gate.observe(image, 10.2) == pytest.approx(10.15 + 0.190)
    assert gate.trigger_source == "grace-life-drop-observed-candidate"
    assert gate.triggered_at_s == pytest.approx(10.2)
    assert gate._life_trigger["reference_s"] == pytest.approx(10.15)


def test_popup_cannot_life_start_and_playfield_loss_resets_peak():
    gate = configured_gate()
    image = np.zeros((720, 1280, 3), np.uint8)
    reading = SimpleNamespace(visible=True, value=1000)
    gate._life_detector = SimpleNamespace(detect=lambda _: reading)
    gate.observe(image, 10.0)
    gate._popup_detector = lambda _: True
    reading.value = 900
    assert gate.observe(image, 10.02) is None
    gate._popup_detector = lambda _: False
    assert gate.observe(image, 10.04) is None
    assert gate.observe(image, 10.05) is None
    assert gate.observe(image, 10.055) is None
    assert not gate.triggered and gate._life_peak == 900
    gate._playfield_detector = lambda _: False
    gate.observe(image, 10.06)
    gate._playfield_detector = lambda _: True
    assert gate.observe(image, 10.08) is None
    assert gate._life_peak == 900


def test_broad_change_discards_old_grace_phase_candidate():
    gate = configured_gate()
    reading = SimpleNamespace(visible=True, value=1000)
    gate._life_detector = SimpleNamespace(detect=lambda _: reading)
    image = np.zeros((720, 1280, 3), np.uint8)
    gate.observe(image, 10.0)
    gate.observe(image, 10.13)
    narrow = image.copy()
    narrow[510:536, :100] = 50
    gate.observe(narrow, 10.15)
    assert gate._grace_stream_since_s == 10.15
    broad = image.copy()
    broad[510:536] = 50
    gate.observe(broad, 10.17)
    assert gate._grace_stream_since_s is None
    reading.value = 950
    changed = broad.copy()
    changed[510:536] = 80
    assert gate.observe(changed, 10.19) == pytest.approx(10.19 + 0.190)
    assert gate.trigger_source == "grace-life-drop-current-frame"


def test_unsupported_environment_preserves_old_guards_and_disables_life():
    gate = NativeStartPhotogate(mode="cooperative-playfield-confirmed")
    gate.configure_early_start(replace(CALIBRATED_ENVIRONMENT, note_speed=6), actions())
    assert not gate._life_fallback_enabled and not gate._spatial_transition_guard
    assert (
        gate.report()["photogate_early_start"]["rejected_reason"]
        == "environment-not-calibrated"
    )


def test_backend_upper_evidence_and_profile_read_are_applied_once():
    gate = configured_gate()
    gate._early_gate.detect = lambda image: dict(
        y=float(image[0, 0, 0]), normalized_span=180.0, lanes=[0, 6]
    )
    backend = object.__new__(NativeMinitouchBackend)
    backend._photogate = gate
    backend._start_last_sequence = None
    backend._frozen_timing_offset_ms = 51
    backend._first_read_delay_s = 0.00192
    backend._device_error = None
    backend._device_thread = SimpleNamespace(is_alive=lambda: False)
    backend._device = SimpleNamespace(connected=True)
    backend._start_evidence_ticket = None
    backend._run_id = "early-test"
    backend._actions = actions()
    submitted = []
    backend._start_evidence_writer = SimpleNamespace(
        submit=lambda *args: submitted.append(args)
    )
    image = np.zeros((720, 1280, 3), np.uint8)
    image[0, 0, 0] = 180
    backend.observe_start_frame_metadata(dict(sequence=1, reused=False))
    assert backend.observe_start_frame(image, 10.0) is None
    image[0, 0, 0] = 220
    backend.observe_start_frame_metadata(dict(sequence=2, reused=False))
    assert backend.observe_start_frame(image, 10.2) == pytest.approx(
        10.1 + 0.89487408445 - 0.051 - 0.00192
    )
    _, rois, packet = submitted[0]
    assert rois[0].shape == (201, 1280, 3)
    assert packet["source"] == "upper-first-pair-crossing"
    assert packet["observations"][0]["metadata"]["sequence"] == 1
    assert packet["remaining_first_action_budget_s"] == pytest.approx(
        packet["final_anchor_s"] - 10.2
    )


def test_flag_default_off_and_configure_rejects_running_backend(monkeypatch):
    monkeypatch.delenv("MAABANGDREAM_NATIVE_EARLY_START_TRIAL", raising=False)
    assert not runtime_flags.native_early_start_trial_enabled()
    monkeypatch.setenv("MAABANGDREAM_NATIVE_EARLY_START_TRIAL", "1")
    assert runtime_flags.native_early_start_trial_enabled()
    backend = object.__new__(NativeMinitouchBackend)
    backend._early_start_trial_requested = True
    backend._state = "running"
    with pytest.raises(RuntimeError):
        backend.configure_early_start(CALIBRATED_ENVIRONMENT)


def test_missing_detection_locks_first_group_instead_of_crossing_later_pair():
    gate = EarlyStartGate(CALIBRATED_ENVIRONMENT, actions())
    image = np.zeros((720, 1280, 3), np.uint8)
    gate.detect = lambda _: dict(y=180.0, normalized_span=180.0)
    gate.observe(image, 10.0, {})
    gate.detect = lambda _: None
    gate.observe(image, 10.1, {})
    gate.detect = lambda _: dict(y=220.0, normalized_span=180.0)
    assert gate.observe(image, 10.2, {}) is None
    assert gate.rejected_reason == "first-head-track-lost"


def test_actual_detector_rejects_dark_background_and_narrow_arrows():
    import cv2

    gate = EarlyStartGate(CALIBRATED_ENVIRONMENT, actions())
    image = np.zeros((720, 1280, 3), np.uint8)
    assert gate.detect(image) is None
    for x in (498, 782):
        cv2.line(image, (x - 5, 195), (x, 190), (180, 80, 255), 2)
        cv2.line(image, (x, 190), (x + 5, 195), (180, 80, 255), 2)
    assert gate.detect(image) is None


@pytest.mark.parametrize("jitter", [0.0, -0.5])
def test_quantized_motion_does_not_lose_first_pair(jitter):
    gate = EarlyStartGate(CALIBRATED_ENVIRONMENT, actions())
    image = np.zeros((720, 1280, 3), np.uint8)
    y = [180.0]
    gate.detect = lambda _: dict(y=y[0], normalized_span=180.0)
    gate.observe(image, 10.0, {})
    y[0] += jitter
    assert gate.observe(image, 10.02, {}) is None
    assert not gate.disabled
    y[0] = 220.0
    assert gate.observe(image, 10.2, {}) is not None


@pytest.mark.parametrize(
    "geometry",
    [
        (565.0, (190, 340, 490, 640, 790, 940, 1090)),
        (590.0, (180, 340, 490, 640, 790, 940, 1090)),
    ],
)
def test_custom_backend_geometry_is_rejected_without_enabling_life_or_shape(geometry):
    backend = object.__new__(NativeMinitouchBackend)
    backend._state = "armed"
    backend._early_start_trial_requested = True
    backend._actions = actions()
    backend._config = dict(judgement_y=geometry[0], lane_centers=geometry[1])
    backend._photogate = NativeStartPhotogate(mode="cooperative-playfield-confirmed")
    backend.configure_early_start(CALIBRATED_ENVIRONMENT)
    gate = backend._photogate
    assert gate._early_gate.rejected_reason == "geometry-not-calibrated"
    assert not gate._life_fallback_enabled and not gate._spatial_transition_guard


def test_compiled_duplicates_are_deduplicated_by_source_note():
    duplicated = actions()
    duplicated.append(dict(duplicated[0]))
    assert first_group_topology(duplicated)["supported"]
    duplicated.append(dict(kind="down", lane=0, due_s=3.0, note_index=0))
    assert not first_group_topology(duplicated)["supported"]


@pytest.mark.parametrize("direction", ["Left", "Right"])
def test_directional_flick_first_group_is_outside_calibration(direction):
    chart_actions = actions()
    chart_actions[0]["flick_direction"] = direction
    assert not first_group_topology(chart_actions)["supported"]
    chart_actions[0]["flick_direction"] = None
    assert first_group_topology(chart_actions)["supported"]
    conflict = dict(chart_actions[0], flick_direction=direction)
    chart_actions.insert(0, conflict)
    # 即使后一个同索引动作覆盖回普通 FLICK，方向冲突仍必须拒绝。
    assert not first_group_topology(chart_actions)["supported"]


def test_wrong_signature_components_are_all_rejected():
    for changes in (
        dict(dpi=320),
        dict(game_fps=120),
        dict(render_quality="high"),
        dict(note_skin_type=2),
        dict(tap_effect=1),
        dict(judgement_assist_effect=True),
        dict(engine="legacy"),
        dict(resolution=(1920, 1080)),
    ):
        assert not EarlyStartGate(
            replace(CALIBRATED_ENVIRONMENT, **changes), actions()
        ).eligible


@pytest.mark.parametrize("original_guard", [False, True])
def test_real_frame_resolution_disables_life_and_restores_original_shape_policy(
    original_guard,
):
    gate = NativeStartPhotogate(
        mode="cooperative-playfield-confirmed",
        spatial_transition_guard=original_guard,
        playfield_detector=lambda _: True,
        popup_detector=lambda _: False,
    )
    original_popup = gate._popup_detector
    gate.configure_early_start(CALIBRATED_ENVIRONMENT, actions())
    gate._early_gate.detect = lambda _: None
    reading = SimpleNamespace(visible=True, value=1000)
    gate._life_detector = SimpleNamespace(detect=lambda _: reading)
    gate.observe(np.zeros((720, 1280, 3), np.uint8), 10.0)
    reading.value = 500
    assert gate.observe(np.zeros((1080, 1280, 3), np.uint8), 10.02) is None
    assert not gate._life_fallback_enabled and not gate.triggered
    assert gate._spatial_transition_guard is original_guard
    assert gate._popup_detector is original_popup
    assert gate._early_gate.rejected_reason == "frame-resolution-mismatch"


@pytest.mark.parametrize(
    "mode,enabled",
    [("cooperative", True), ("versus", True), ("realtime", False), ("formal", False)],
)
def test_real_backend_configuration_wires_new_trial_only_in_cooperative_modes(
    monkeypatch, mode, enabled
):
    import agent.realtime.native_play as module

    monkeypatch.setenv("MAABANGDREAM_NATIVE_EARLY_START_TRIAL", "1")
    monkeypatch.delenv("MAABANGDREAM_NATIVE_START_SHAPE_TRIAL", raising=False)
    monkeypatch.setattr(module.native_engine, "available", lambda: True)
    monkeypatch.setattr(
        module.native_engine,
        "compile_chart",
        lambda _: SimpleNamespace(compile_actions=lambda _: actions()),
    )
    monkeypatch.setattr(
        module.native_engine,
        "touch_script_compiler",
        lambda **_: SimpleNamespace(execution_receipts=lambda: []),
    )
    monkeypatch.setattr(module.native_engine, "latency_calibrator", lambda: None)
    monkeypatch.setattr(module, "shared_start_evidence_writer", lambda: None)
    backend = NativeMinitouchBackend(
        "unused",
        adb_path="unused",
        serial="unused",
        start_gate_mode=mode,
        device=SimpleNamespace(request_reset=lambda: None),
        session_factory=lambda **_: SimpleNamespace(),
        timing_trial_enabled=False,
    )
    backend.configure_early_start(CALIBRATED_ENVIRONMENT)
    assert (backend._photogate._early_gate is not None) is enabled
    assert backend._photogate._life_fallback_enabled is enabled


def test_after_grace_trusted_life_drop_takes_over_with_explicit_low_confidence():
    gate = configured_gate()
    reading = SimpleNamespace(visible=True, value=1000)
    gate._life_detector = SimpleNamespace(detect=lambda _: reading)
    image = np.zeros((720, 1280, 3), np.uint8)
    gate.observe(image, 10.0)
    gate.observe(image, 10.13)
    reading.value = 900
    assert gate.observe(image, 11.1) == pytest.approx(11.1 + 0.190)
    assert gate.trigger_source == "after-grace-life-drop-current-frame"
    assert gate._trigger_evidence is None


def test_long_gap_lower_threshold_is_current_frame_not_interpolated_history():
    image = np.zeros((720, 1280, 3), np.uint8)
    changed = image.copy()
    changed[510:536, :200] = 50
    candidate = configured_gate()
    candidate.observe(image, 10.0)
    candidate.observe(image, 10.13)
    assert candidate.observe(changed, 11.1) == pytest.approx(11.1 + 0.190)
    assert candidate.trigger_source == "lower-gap-direct-current-frame"
    assert candidate._trigger_evidence is None
    old = NativeStartPhotogate(
        mode="cooperative-playfield-confirmed",
        stable_duration_ms=120,
        grace_ms=500,
        playfield_detector=lambda _: True,
        popup_detector=lambda _: False,
    )
    old.observe(image, 10.0)
    old.observe(image, 10.13)
    assert old.observe(changed, 11.1) < 11.1
    assert old.trigger_source == "interpolated-threshold-crossing"
