import cv2
import numpy as np
import pytest

from agent.realtime.native_play import NativeMinitouchBackend, NativeStartPhotogate
from agent.realtime.prepare_popup import CooperativePreparePopupDetector
from agent.realtime import runtime_flags


def gate(enabled=False):
    return NativeStartPhotogate(stable_duration_ms=0, grace_ms=0,
        mode="cooperative-playfield-confirmed", playfield_detector=lambda _: True,
        popup_detector=lambda _: False, spatial_transition_guard=enabled)


def freeze(result, image):
    result.observe(image, 1.)
    result.observe(image, 1.01)
    assert result.frozen


def test_partial_height_connected_first_notes_are_not_a_broad_transition():
    image = np.zeros((720, 1280, 3), np.uint8)
    note = image.copy()
    note[520:526, 170:1110] = 255
    old, candidate = gate(), gate(True)
    freeze(old, image)
    freeze(candidate, image)
    assert old.observe(note, 1.02) is None
    assert not old.frozen
    assert candidate.observe(note, 1.02) == pytest.approx(1.01 + 3 / (255*3*6/26*940/1280) * .01 + .190)
    assert candidate.triggered


@pytest.mark.parametrize("channels", [3, 4])
def test_full_height_change_still_blocks_and_clears_reference(channels):
    image = np.full((720, 1280, channels), 255, np.uint8)
    changed = image.copy()
    changed[510:536, :700, :3] = 0
    result = gate(True)
    freeze(result, image)
    assert result.observe(changed, 1.02) is None
    assert not result.frozen and result._frozen_band is None


def popup_image(solid):
    image = np.zeros((720, 1280, 3), np.uint8)
    cv2.rectangle(image, (400, 420), (880, 520), (255,255,255), -1 if solid else 3)
    cv2.rectangle(image, (430,450), (450,470), (180,80,255), -1)
    return image


def test_real_panel_kept_and_sparse_white_pink_shape_rejected():
    old = CooperativePreparePopupDetector()
    candidate = CooperativePreparePopupDetector(require_solid_panel=True)
    assert old(popup_image(True)) and candidate(popup_image(True))
    assert old(popup_image(False))
    assert not candidate(popup_image(False))


def test_trial_environment_is_opt_in(monkeypatch):
    monkeypatch.delenv("MAABANGDREAM_NATIVE_START_SHAPE_TRIAL", raising=False)
    assert not runtime_flags.native_start_shape_trial_enabled()
    monkeypatch.setenv("MAABANGDREAM_NATIVE_START_SHAPE_TRIAL", "true")
    assert not runtime_flags.native_start_shape_trial_enabled()
    monkeypatch.setenv("MAABANGDREAM_NATIVE_START_SHAPE_TRIAL", "1")
    assert runtime_flags.native_start_shape_trial_enabled()
    assert not NativeStartPhotogate(mode="cooperative-playfield-confirmed").report()["photogate_spatial_transition_guard"]


def test_trial_report_defines_actual_guard():
    report = gate(True).report()
    assert report["photogate_spatial_transition_guard"] is True
    assert report["photogate_broad_change_policy"] == "column-mean-then-all-band-rows"
    assert report["photogate_broad_pixel_change_min"] == 45
    assert report["photogate_broad_column_fraction"] == .35
    assert report["photogate_popup_min_panel_fill"] == .75


def test_backend_wires_trial_only_for_native_cooperative_or_versus(monkeypatch):
    # 执行真实构造路径至门控建成，再用会话工厂截断，避免创建设备或后台线程。
    import agent.realtime.native_play as module
    from types import SimpleNamespace
    class Done(Exception): pass
    class Timeline:
        def compile_actions(self, _): return [{"due_s": 1.}]
    monkeypatch.setattr(module.native_engine, "available", lambda: True)
    monkeypatch.setattr(module.native_engine, "compile_chart", lambda _: Timeline())
    monkeypatch.setattr(module.native_engine, "touch_script_compiler", lambda **_: SimpleNamespace(execution_receipts=lambda: []))
    monkeypatch.setattr(module.native_engine, "latency_calibrator", lambda **_: None)
    monkeypatch.setattr(module, "shared_start_evidence_writer", lambda: None)
    def stop(**kwargs): raise Done()
    monkeypatch.setenv("MAABANGDREAM_NATIVE_START_SHAPE_TRIAL", "1")
    for mode, expected in [("cooperative",True),("versus",True),("realtime",False),("single",False)]:
        backend = object.__new__(NativeMinitouchBackend)
        with pytest.raises(Done):
            backend.__init__("unused", adb_path="unused", serial="unused", start_gate_mode=mode,
                device=SimpleNamespace(request_reset=lambda: None), session_factory=stop, timing_trial_enabled=False)
        assert backend._photogate.report()["photogate_spatial_transition_guard"] is expected
        assert backend._photogate._popup_detector is None or backend._photogate._popup_detector._require_solid_panel is expected
    monkeypatch.delenv("MAABANGDREAM_NATIVE_START_SHAPE_TRIAL")
    backend = object.__new__(NativeMinitouchBackend)
    with pytest.raises(Done):
        backend.__init__("unused", adb_path="unused", serial="unused", start_gate_mode="cooperative",
            device=SimpleNamespace(request_reset=lambda: None), session_factory=stop, timing_trial_enabled=False)
    assert not backend._photogate.report()["photogate_spatial_transition_guard"]


def test_shape_mismatch_fails_closed_without_array_broadcasting():
    result = gate(True)
    freeze(result, np.zeros((720,1280,3),np.uint8))
    assert result.observe(np.full((720,1000,3),255,np.uint8),1.02) is None
    assert not result.frozen


def test_shape_change_is_rejected_even_below_old_broad_threshold():
    result = gate(True)
    freeze(result, np.zeros((720,1280,3),np.uint8))
    assert result.observe(np.full((720,1000,3),2,np.uint8),1.02) is None
    assert not result.frozen


@pytest.mark.parametrize("height", [721, 1080])
def test_same_width_height_change_resets_before_local_note_trigger(height):
    result = gate(True)
    freeze(result, np.zeros((720,1280,3),np.uint8))
    changed = np.zeros((height,1280,3),np.uint8)
    changed[round(510*height/720):round(535*height/720)+1,:320] = 255
    assert result.observe(changed,1.02) is None
    assert not result.frozen and result._frozen_frame_shape is None


def test_extra_alpha_channel_does_not_change_frozen_resolution():
    result = gate(True)
    freeze(result,np.zeros((720,1280,3),np.uint8))
    changed = np.zeros((720,1280,4),np.uint8)
    changed[:,:,3] = 255
    changed[510:536,:320,:3] = 255
    assert result.observe(changed,1.02) is not None


@pytest.mark.parametrize("columns,level,blocked", [(448,15,True),(447,15,False),(448,14,False)])
def test_broad_thresholds_keep_exact_inclusive_boundaries(columns,level,blocked):
    image = np.zeros((720,1280,3),np.uint8)
    changed = image.copy()
    changed[510:536,:columns] = level
    result = gate(True)
    freeze(result,image)
    anchor = result.observe(changed,1.02)
    assert (anchor is None) is blocked
    assert result.frozen is not blocked


def test_one_unchanged_row_cannot_be_called_full_height_transition():
    image = np.zeros((720,1280,3),np.uint8)
    changed = image.copy()
    changed[510:535,:940] = 255
    result = gate(True)
    freeze(result,image)
    assert result.observe(changed,1.02) is not None


def test_frozen_reference_owns_pixels_and_ignores_alpha():
    image = np.zeros((720,1280,4),np.uint8)
    result = gate(True)
    freeze(result,image)
    image[510:536,:,3] = 255
    assert not np.shares_memory(image,result._frozen_band)
    assert result._frozen_band.shape == (26,1280,3)
    assert result.observe(image,1.02) is None
    assert result.frozen


def test_playfield_loss_clears_spatial_reference():
    result = gate(True)
    image = np.zeros((720,1280,3),np.uint8)
    freeze(result,image)
    result._playfield_detector = lambda _: False
    result.observe(image,1.02)
    assert result._frozen_band is None and not result.frozen
