import json
from types import SimpleNamespace

import numpy as np
import pytest

from agent.realtime.native_play import NativeMinitouchBackend, NativeStartPhotogate
from agent.realtime.native_start_evidence import NativeStartEvidenceWriter
from agent.realtime.vision_io import imread_unicode


def backend(writer):
    result = object.__new__(NativeMinitouchBackend)
    result._photogate = NativeStartPhotogate(stable_duration_ms=0, grace_ms=0,
        playfield_detector=lambda _image: True)
    result._start_evidence_writer = writer
    result._start_evidence_ticket = None
    result._start_frame_metadata = {}
    result._run_id = "test"
    result._frozen_timing_offset_ms = 13
    result._first_read_delay_s = .008
    result._actions = [{"due_s": .5}]
    result._device_error = None
    result._device_thread = SimpleNamespace(is_alive=lambda: False)
    result._device = SimpleNamespace(connected=True)
    result.life_feedback_enabled = False
    return result


def trigger(result):
    image = np.zeros((720, 1280, 3), np.uint8)
    for sequence, now, reused in [(1, 10, False), (1, 10.05, True), (2, 10.1, False)]:
        result.observe_start_frame_metadata({"request_s": now - .02, "sequence": sequence, "reused": reused})
        if sequence == 2:
            image[510:536] = 2
        anchor = result.observe_start_frame(image, now)
    return anchor


def test_debug_off_two_actual_roi_observations_and_interpolation_are_saved_in_background(tmp_path):
    writer = NativeStartEvidenceWriter(tmp_path / "中文", start_worker=False)
    result = backend(writer)
    anchor = trigger(result)
    assert anchor == pytest.approx(10.05 + .5 * .05 + .190 - .013 - .008)
    status = result._start_evidence_ticket.report()
    assert status["state"] == "queued" and not list(tmp_path.rglob("*.png"))
    writer._write_next()
    assert result._start_evidence_ticket.report()["state"] == "written"
    packet = json.loads(open(status["metadata_path"], encoding="utf-8").read())
    previous, current = packet["observations"]
    assert previous["sequence"] == 1 and previous["reused"] is True
    assert current["sequence"] == 2 and current["reused"] is False
    assert previous["reference_mean_bgr"] == [0, 0, 0]
    assert previous["reference_observed_s"] == 10
    assert packet["previous_change"] == 0 and packet["current_change"] == 6
    assert packet["crossing_fraction"] == pytest.approx(.5)
    assert packet["final_anchor_s"] == anchor
    assert packet["chart_first_due_s"] == .5
    assert packet["profile_timing_offset_ms"] == 13 and packet["probe_first_read_s"] == .008
    assert packet["latency_ms"] == 190
    assert [imread_unicode(path).shape for path in status["roi_paths"]] == [(26, 1280, 3)] * 2
    assert int(imread_unicode(status["roi_paths"][0]).max()) == 0
    assert int(imread_unicode(status["roi_paths"][1]).max()) == 2


def test_full_queue_and_io_failure_do_not_change_anchor(tmp_path, monkeypatch):
    writer = NativeStartEvidenceWriter(tmp_path, capacity=1, start_worker=False)
    first = backend(writer)
    expected = trigger(first)
    second = backend(writer)
    assert trigger(second) == expected
    assert second._start_evidence_ticket.report()["state"] == "queue-full"
    monkeypatch.setattr("agent.realtime.native_start_evidence.imwrite_unicode", lambda *_args: False)
    writer._write_next()
    assert first._start_evidence_ticket.report()["state"] == "failed"
    assert first._start_evidence_ticket.report()["warnings"]


def test_diagnostic_copy_failure_does_not_change_control_result(tmp_path, monkeypatch):
    result = backend(NativeStartEvidenceWriter(tmp_path, start_worker=False))
    monkeypatch.setattr(result._photogate, "_diagnostic_observation", lambda *_args: (_ for _ in ()).throw(OSError("copy")))
    assert trigger(result) == pytest.approx(10.244)
    assert result._start_evidence_ticket is None


def test_one_missing_previous_roi_cannot_be_replaced_with_an_older_successful_copy(tmp_path, monkeypatch):
    result = backend(NativeStartEvidenceWriter(tmp_path, start_worker=False))
    original = result._photogate._diagnostic_observation
    def observe(image, now):
        if now == 10.05:
            raise OSError("previous copy failed")
        return original(image, now)
    monkeypatch.setattr(result._photogate, "_diagnostic_observation", observe)
    assert trigger(result) == pytest.approx(10.244)
    assert result._start_evidence_ticket is None
    assert "missing-previous-roi" in result._photogate._diagnostic_warnings


def test_submit_exception_keeps_original_anchor_and_a_warning(tmp_path, monkeypatch):
    writer = NativeStartEvidenceWriter(tmp_path, start_worker=False)
    monkeypatch.setattr(writer, "submit", lambda *_args: (_ for _ in ()).throw(OSError("submit")))
    result = backend(writer)
    assert trigger(result) == pytest.approx(10.244)
    assert "OSError" in result._start_evidence_warning


def test_shared_writer_does_not_create_per_run_threads(tmp_path, monkeypatch):
    from agent.realtime import native_start_evidence as module
    writers = []
    monkeypatch.setattr(module, "_shared_writer", None)
    monkeypatch.setattr(module, "NativeStartEvidenceWriter",
                        lambda: writers.append(NativeStartEvidenceWriter(tmp_path, start_worker=False)) or writers[-1])
    assert module.shared_start_evidence_writer() is module.shared_start_evidence_writer()
    assert len(writers) == 1


def test_queue_full_and_submit_failure_do_not_log_on_capture_thread(tmp_path, monkeypatch):
    import logging
    monkeypatch.setattr(logging, "warning", lambda *_args: pytest.fail("截图线程不得输出同步警告"))
    writer = NativeStartEvidenceWriter(tmp_path, capacity=1, start_worker=False)
    assert trigger(backend(writer)) == pytest.approx(10.244)
    assert trigger(backend(writer)) == pytest.approx(10.244)


@pytest.mark.skipif(not __import__("agent.realtime.native_engine", fromlist=["available"]).available(), reason="Native 未构建")
def test_writer_thread_initialization_failure_does_not_prevent_backend_creation(monkeypatch):
    from pathlib import Path
    monkeypatch.setattr("agent.realtime.native_play.shared_start_evidence_writer",
                        lambda: (_ for _ in ()).throw(RuntimeError("thread start failed")))
    chart = Path(__file__).resolve().parents[1] / "resource/charts/bestdori/306/hard.json"
    result = NativeMinitouchBackend(chart, adb_path="adb", serial="test")
    assert result._start_evidence_writer is None
    assert "RuntimeError" in result._start_evidence_warning
