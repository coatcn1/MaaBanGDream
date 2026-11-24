from types import SimpleNamespace
import numpy as np
import pytest
from agent.realtime.engine import RealtimeEngine
from agent.realtime import native_play
from agent.realtime.native_play import (
    NativeMinitouchBackend,
    NativeStaleStartCaptureError,
    NativeStartPhotogate,
)

def backend():
    b = object.__new__(NativeMinitouchBackend)
    b._photogate = NativeStartPhotogate(stable_duration_ms=120, grace_ms=500,
        playfield_detector=lambda _: True, suppress_prepare_popup=False)
    b._start_last_sequence = None
    b._frozen_timing_offset_ms = 51
    b._first_read_delay_s = .001
    b._device_error = None
    b._device_thread = SimpleNamespace(is_alive=lambda: False)
    b._device = SimpleNamespace(connected=True)
    return b

def observe(b, image, now, **metadata):
    b.observe_start_frame_metadata(metadata)
    return b.observe_start_frame(image, now)

@pytest.mark.parametrize('duplicates', [0, 1, 20])
def test_duplicate_polling_cannot_retime_first_note(duplicates):
    b = backend()
    image = np.zeros((720, 1280, 3), np.uint8)
    note = image.copy()
    note[510:536, :320] = 8
    for seq, now in [(1, 10.), (2, 10.13), (3, 10.7)]:
        observe(b, image, now, sequence=seq, reused=False)
    for i in range(duplicates):
        assert observe(b, image, 10.71 + i * .01, sequence=3, reused=True) is None
    anchor = observe(b, note, 11., sequence=4, reused=False)
    # 两张新帧的变化量为 6，阈值 3 对应 10.7 到 11.0 的中点。
    assert anchor == pytest.approx(10.85 + .190 - .051 - .001)

def test_stale_frames_are_rejected_before_metadata_setter_or_gate_mutation():
    b = backend()
    image = np.zeros((720, 1280, 3), np.uint8)
    observe(b, image, 10., sequence=5, reused=False)
    before = dict(b._photogate.__dict__)
    for metadata in [dict(sequence=5), dict(sequence=4), dict(sequence=6, reused=True)]:
        assert observe(b, image, 20., **metadata) is None
        assert b._photogate.__dict__ == before
    assert b._start_last_sequence == 5
    observe(b, image, 10.13, sequence=6, reused=False)
    assert b._photogate.frozen

def test_duplicate_frames_cannot_satisfy_stability():
    b = backend()
    image = np.zeros((720, 1280, 3), np.uint8)
    observe(b, image, 10., sequence=1, reused=False)
    for i in range(20):
        observe(b, image, 10.1 + i * .1, sequence=1, reused=True)
    assert b._photogate.stable_since_s is None
    assert not b._photogate.frozen
    observe(b, image, 12.1, sequence=2, reused=False)
    assert b._photogate.frozen

@pytest.mark.parametrize('sequence', [None, True, False, '1', 1.5])
def test_invalid_sequence_preserves_direct_call_compatibility(sequence):
    b = backend()
    image = np.zeros((720, 1280, 3), np.uint8)
    observe(b, image, 10., sequence=sequence)
    observe(b, image, 10.13, sequence=sequence)
    assert b._photogate.frozen
    assert b._start_last_sequence is None

def test_direct_call_without_metadata_remains_compatible():
    b = backend()
    image = np.zeros((720, 1280, 3), np.uint8)
    b.observe_start_frame(image, 10.)
    b.observe_start_frame(image, 10.13)
    assert b._photogate.frozen


def test_fresh_popup_and_playfield_transitions_still_reset_gate():
    b = backend()
    b._photogate = NativeStartPhotogate(stable_duration_ms=120, grace_ms=500,
        mode='cooperative-playfield-confirmed',
        playfield_detector=lambda image: image[0, 0, 0] != 3,
        popup_detector=lambda image: image[0, 0, 0] == 2)
    image = np.zeros((720, 1280, 3), np.uint8)
    popup = image.copy()
    popup[0, 0] = 2
    lost = image.copy()
    lost[0, 0] = 3
    observe(b, image, 10., sequence=1)
    observe(b, image, 10.13, sequence=2)
    assert b._photogate.frozen
    observe(b, popup, 10.14, sequence=2, reused=True)
    assert b._photogate.frozen and b._photogate.prepare_popup_frames == 0
    observe(b, popup, 10.15, sequence=3)
    assert not b._photogate.frozen and b._photogate.prepare_popup_frames == 1
    observe(b, image, 10.2, sequence=4)
    assert b._photogate.prepare_popup_blocked_events == 2
    observe(b, image, 10.3, sequence=5)
    observe(b, image, 10.44, sequence=6)
    assert b._photogate.frozen
    observe(b, lost, 10.45, sequence=7)
    assert not b._photogate.frozen
    assert b._photogate.playfield_loss_events == 1


def lower_band_start():
    b = backend()
    b._state = "armed"
    b._stale_start_capture_guard_enabled = True
    image = np.zeros((720, 1280, 3), np.uint8)
    for sequence, now in enumerate((10., 10.13, 10.70), 1):
        assert observe(b, image, now, sequence=sequence, reused=False) is None
    return b, image


def test_stale_capture_with_overdue_lower_band_anchor_cannot_start():
    b, note = lower_band_start()
    note[510:536, :320] = 100
    with pytest.raises(NativeStaleStartCaptureError, match=r"743\.0 ms.+-582\.0 ms"):
        observe(b, note, 11.45, sequence=4, reused=False,
                request_s=10.703, first_done_observed_s=11.446)
    assert b._state == "failed"
    assert b._start_decision["source"] == "interpolated-threshold-crossing"
    assert b._start_decision["remaining_first_action_budget_s"] == pytest.approx(-.582)
    assert b._start_decision["capture_request_wait_ms"] == pytest.approx(743.)
    assert b._start_decision["rejection_reason"] == "stale-async-start-capture"


@pytest.mark.parametrize("request_s, note_width, note_value, expected_budget", [
    (11.440, 320, 100, -.582),
    (10.703, 160, 8, .138),
])
def test_guard_accepts_start_unless_capture_wait_and_budget_are_both_bad(
    request_s, note_width, note_value, expected_budget,
):
    b, note = lower_band_start()
    note[510:536, :note_width] = note_value
    anchor = observe(b, note, 11.45, sequence=4, reused=False,
                     request_s=request_s, first_done_observed_s=11.446)
    assert b._state == "ready"
    assert anchor == pytest.approx(11.45 + expected_budget)
    assert b._start_decision["capture_request_wait_ms"] == pytest.approx((11.446 - request_s) * 1000)
    assert "rejection_reason" not in b._start_decision


@pytest.mark.parametrize("metadata_override", [
    {"sequence": None}, {"sequence": 0}, {"reused": None}, {"request_s": None},
    {"request_s": "10.703"}, {"request_s": 11.447},
    {"first_done_observed_s": float("inf")}, {"first_done_observed_s": float("nan")},
    {"first_done_observed_s": 11.451},
])
def test_guard_requires_valid_fresh_capture_metadata(metadata_override):
    b, note = lower_band_start()
    # 尚未观测到真实正序号时，零值也保留原路径。
    if metadata_override.get("sequence") == 0:
        b._start_last_sequence = None
    note[510:536, :320] = 100
    metadata = {"sequence": 4, "reused": False,
                "request_s": 10.703, "first_done_observed_s": 11.446}
    metadata.update(metadata_override)
    assert observe(b, note, 11.45, **metadata) == pytest.approx(10.868)
    assert "capture_request_wait_ms" not in b._start_decision


def test_reused_frame_cannot_trigger_rejection():
    b, note = lower_band_start()
    note[510:536, :320] = 100
    assert observe(b, note, 11.45, sequence=4, reused=True,
                   request_s=10.703, first_done_observed_s=11.446) is None
    assert not b._photogate.triggered
    assert b._state == "armed"


@pytest.mark.parametrize("source", ["direct-threshold", "upper-first-pair-crossing", "lower-gap-direct-current-frame"])
def test_guard_checks_only_normal_lower_band_sources(source):
    b, image = lower_band_start()
    b._photogate = SimpleNamespace(trigger_source=source, observe=lambda *_: 10.92)
    if source == "direct-threshold":
        with pytest.raises(NativeStaleStartCaptureError):
            observe(b, image, 11.45, sequence=4, reused=False,
                    request_s=10.703, first_done_observed_s=11.446)
    else:
        assert observe(b, image, 11.45, sequence=4, reused=False,
                       request_s=10.703, first_done_observed_s=11.446) == pytest.approx(10.868)
        assert "capture_request_wait_ms" not in b._start_decision


@pytest.mark.parametrize("env, mode, expected", [
    (None, "cooperative", False), ("true", "cooperative", False),
    ("1", "cooperative", True), ("1", "versus", False), ("1", "formal", False),
])
def test_guard_is_opt_in_and_cooperative_only(monkeypatch, env, mode, expected):
    class SessionFactoryReached(Exception):
        pass

    def stop_at_session_factory(**_):
        raise SessionFactoryReached

    if env is None:
        monkeypatch.delenv("MAABANGDREAM_NATIVE_STALE_START_GUARD", raising=False)
    else:
        monkeypatch.setenv("MAABANGDREAM_NATIVE_STALE_START_GUARD", env)
    timeline = SimpleNamespace(compile_actions=lambda _: [{"due_s": 0.0}])
    monkeypatch.setattr(native_play.native_engine, "available", lambda: True)
    monkeypatch.setattr(native_play.native_engine, "compile_chart", lambda _: timeline)
    monkeypatch.setattr(native_play.native_engine, "touch_script_compiler",
                        lambda **_: SimpleNamespace(execution_receipts=lambda: []))
    monkeypatch.setattr(native_play.native_engine, "latency_calibrator", lambda **_: None)
    monkeypatch.setattr(native_play, "shared_start_evidence_writer", lambda: None)
    b, note = lower_band_start()
    with pytest.raises(SessionFactoryReached):
        b.__init__("unused", adb_path="unused", serial="unused", start_gate_mode=mode,
                   device=SimpleNamespace(connected=True, request_reset=lambda: None),
                   photogate=b._photogate, press_bias_ms=51,
                   session_factory=stop_at_session_factory, timing_trial_enabled=False)
    assert b._stale_start_capture_guard_enabled is expected
    note[510:536, :320] = 100
    if expected:
        monkeypatch.delenv("MAABANGDREAM_NATIVE_STALE_START_GUARD")
        with pytest.raises(NativeStaleStartCaptureError):
            observe(b, note, 11.45, sequence=4, reused=False,
                    request_s=10.703, first_done_observed_s=11.446)
    else:
        assert observe(b, note, 11.45, sequence=4, reused=False,
                       request_s=10.703, first_done_observed_s=11.446) == pytest.approx(10.868)
        assert "capture_request_wait_ms" not in b._start_decision


def test_engine_cleans_up_without_starting_native_session_on_rejection():
    b, note = lower_band_start()
    note[510:536, :320] = 100
    events = []
    b.arm = lambda: events.append("arm")
    b.start = lambda _: events.append("start")
    b.stop = lambda: events.append("native-stop")
    b.set_terminal_reason = lambda _: None
    b.report = lambda: {"sent": 0, "start_decision": b._start_decision}
    touch = SimpleNamespace(close=lambda: events.append("touch-close"))
    recorder = SimpleNamespace(close=lambda: events.append("recorder-close"))
    clock = SimpleNamespace(now=10.71)
    engine = RealtimeEngine(None, None, touch, lambda: clock.now, native_backend=b,
                            debug_recorder=recorder)

    def capture():
        clock.now = 11.45
        return note

    capture.frame_metadata = {"sequence": 4, "reused": False,
                              "request_s": 10.703, "first_done_observed_s": 11.446}
    with pytest.raises(NativeStaleStartCaptureError) as error:
        engine.run(capture, lambda: False, duration_seconds=2, target_fps=60)
    assert events == ["arm", "native-stop", "touch-close", "recorder-close"]
    assert error.value.realtime_stats.dispatched_actions == 0
    assert error.value.realtime_stats.native_report["start_decision"]["rejection_reason"] == "stale-async-start-capture"
    assert not error.value.realtime_stats.cleanup_failed
