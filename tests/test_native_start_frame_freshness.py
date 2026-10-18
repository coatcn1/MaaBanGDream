from types import SimpleNamespace
import numpy as np
import pytest
from agent.realtime.native_play import NativeMinitouchBackend, NativeStartPhotogate

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
