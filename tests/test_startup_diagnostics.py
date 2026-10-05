from __future__ import annotations

import json
import threading
import time

import numpy as np

from agent.realtime import startup_diagnostics as diagnostics
from agent.realtime.vision_io import imread_unicode


def observations(recorder):
    return [json.loads(row) for row in (recorder.output_dir / "observations.jsonl").read_text(encoding="utf-8").splitlines()]


def test_startup_recorder_keeps_original_frame_and_decision(tmp_path):
    recorder = diagnostics.CooperativeStartupRecorder(tmp_path, "run", close_timeout=1)
    image = np.full((32, 64, 3), 87, dtype=np.uint8)
    recorder.record(image, 10.0, {"phase": "ready", "member_loading": True})
    image[:] = 0
    recorder.close("stopped")
    rows = observations(recorder)
    assert rows[0]["member_loading"] is True
    assert rows[0]["image_status"] == "saved"
    assert np.all(imread_unicode(recorder.output_dir / rows[0]["image"]) == 87)
    summary = json.loads((recorder.output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["outcome"] == "stopped"
    assert summary["frames_saved"] == 1
    assert summary["finalized"] is True
    assert diagnostics.startup_recorder("run") is None


def test_bounded_queue_drops_images_but_keeps_all_decisions(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    real_write = diagnostics.imwrite_unicode

    def blocked_write(*args):
        started.set()
        release.wait(timeout=2)
        return real_write(*args)

    monkeypatch.setattr(diagnostics, "imwrite_unicode", blocked_write)
    recorder = diagnostics.CooperativeStartupRecorder(tmp_path, "run", queue_capacity=1,
                                                      close_timeout=0.01)
    image = np.zeros((16, 16, 3), dtype=np.uint8)
    recorder.record(image, 1, {"phase": "ready"})
    assert started.wait(timeout=1)
    recorder.record(image, 2, {"phase": "loading"})
    recorder.record(image, 3, {"phase": "cover"})
    before = time.monotonic()
    recorder.close("timeout")
    assert time.monotonic() - before < 0.2
    release.set()
    recorder._thread.join(timeout=2)
    rows = observations(recorder)
    assert [row["phase"] for row in rows] == ["ready", "loading", "cover"]
    assert rows[2]["image_status"] == "dropped"
    assert rows[1]["interval_ms"] == 1000


def test_write_error_is_recorded_and_does_not_stop_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "imwrite_unicode", lambda *_a: False)
    recorder = diagnostics.CooperativeStartupRecorder(tmp_path, "run", close_timeout=1)
    recorder.record(np.zeros((16, 16, 3), dtype=np.uint8), 1, {"phase": "cover"})
    recorder.close("failed")
    assert observations(recorder)[0]["image_status"] == "write-error"
    summary = json.loads((recorder.output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["write_errors"] == 2
    assert summary["last_frame"]["image_status"] == "write-error"
    assert summary["finalized"] is True


def test_storage_and_event_limits_are_explicit(tmp_path):
    recorder = diagnostics.CooperativeStartupRecorder(tmp_path, None, max_bytes=1,
                                                      max_events=1, close_timeout=1)
    image = np.zeros((16, 16, 3), dtype=np.uint8)
    recorder.record(image, 1, {"phase": "cover"})
    recorder.record(image + 99, 2, {"phase": "terminal"})
    recorder.close()
    assert observations(recorder)[0]["image_status"] == "dropped-storage-limit"
    summary = json.loads((recorder.output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["events_dropped"] == 1
    assert summary["frames_saved"] == 0
    assert summary["last_frame"]["image_status"] == "saved"
    assert summary["last_frame"]["phase"] == "terminal"
    assert summary["last_frame"]["frame_index"] == 1
    assert np.all(imread_unicode(recorder.output_dir / "last-frame.png") == 99)
    assert (recorder.output_dir / "last-frame.png").is_file()
