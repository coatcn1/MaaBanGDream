from __future__ import annotations

import json
import queue
import threading
import time

import numpy as np
import pytest

from agent.realtime.debug_recorder import (
    RealtimeDebugRecorder,
    append_lifecycle_event,
)
from agent.realtime.note_detector import NoteKind, ObservedNote
from agent.realtime.touch_planner import ActionKind, TouchAction


def test_debug_recorder_writes_lossless_trace_and_replay_summary(tmp_path):
    recorder = RealtimeDebugRecorder(tmp_path, video_fps=30)
    frame = np.zeros((72, 128, 3), dtype=np.uint8)
    note = ObservedNote(NoteKind.HOLD, 4, 790, 400, 70, 200, 1.25)
    action = TouchAction(ActionKind.DOWN, 4, 1.25, contact=4, reason="hold")

    recorder.record(
        frame,
        1.25,
        [note],
        [action],
        "alive",
        [{
            "event": "hold_start",
            "lane": 4,
            "body_confirmed": True,
            "timestamp": 1.25,
        }],
        {
            "initial_offset_ms": 0,
            "current_offset_ms": 1,
            "valid_samples": 12,
            "ignored_samples": 3,
            "ignored_reasons": {"active_hold": 3},
        },
    )
    recorder.record(frame, 1.27, [], [], "alive")
    recorder.close()

    trace = [json.loads(line) for line in
             (recorder.output_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    summary = json.loads((recorder.output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["schema_version"] == 2
    assert len(trace) == 2
    assert trace[0]["notes"][0]["kind"] == "hold"
    assert trace[0]["actions"][0]["kind"] == "down"
    assert trace[0]["diagnostics"][0]["event"] == "hold_start"
    assert trace[0]["timing_feedback"]["current_offset_ms"] == 1
    assert summary["trace_frames"] == 2
    assert summary["video_frames"] == 1
    assert summary["record_worker_finalized"] is True
    assert summary["encoder_finalized"] is True
    assert summary["recorder_error"] is None
    assert summary["diagnostic_counts"] == {"hold_start": 1}
    assert summary["timing_feedback"]["current_offset_ms"] == 1
    assert (recorder.output_dir / "playfield.mkv").stat().st_size > 0
    assert not (recorder.output_dir / "playfield.partial.mkv").exists()
    assert summary["video_container"] == "matroska"
    assert summary["video_codec"] == "MJPG"
    assert summary["video_seek_verified"] is True
    mapping = [
        json.loads(line)
        for line in (recorder.output_dir / "video_frames.jsonl")
        .read_text(encoding="utf-8").splitlines()
    ]
    assert mapping == [{
        "encoded_frame": 0,
        "trace_frame": 0,
        "monotonic_timestamp": 1.25,
        "elapsed_ms": 0.0,
    }]


def test_trace_only_recorder_keeps_replay_evidence_without_video(tmp_path):
    recorder = RealtimeDebugRecorder(tmp_path, video_enabled=False)
    frame = np.zeros((72, 128, 3), dtype=np.uint8)
    note = ObservedNote(NoteKind.TAP, 3, 640, 520, 44, 20, 2.0)
    action = TouchAction(ActionKind.TAP, 3, 2.0, reason="chart-predict")

    recorder.record(
        frame,
        2.0,
        [note],
        [action],
        "alive",
        [{"event": "chart_prediction_press", "lane": 3}],
        life_value=742,
        touch_state={
            "active_contacts": [8],
            "active_positions": {"8": 640},
            "contact_aliases": {"3": 8},
            "pending_taps": [8],
            "pending_flicks": [],
        },
    )
    recorder.close()

    trace = [
        json.loads(line)
        for line in (recorder.output_dir / "trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    summary = json.loads(
        (recorder.output_dir / "summary.json").read_text(encoding="utf-8")
    )
    assert trace[0]["actions"][0]["reason"] == "chart-predict"
    assert trace[0]["life_value"] == 742
    assert trace[0]["touch_state"]["contact_aliases"] == {"3": 8}
    assert summary["recording_mode"] == "trace-only"
    assert summary["video_enabled"] is False
    assert summary["video_frames"] == 0
    assert summary["video_finalize_status"] == "disabled"
    assert summary["complete_frame_evidence"] is True
    assert not (recorder.output_dir / "playfield.mkv").exists()
    assert not (recorder.output_dir / "playfield.partial.mkv").exists()
    assert not (recorder.output_dir / "video_frames.jsonl").exists()


def test_debug_recorder_marks_preflight_and_engine_phases(tmp_path):
    recorder = RealtimeDebugRecorder(tmp_path, video_enabled=False)
    frame = np.zeros((72, 128, 3), dtype=np.uint8)

    recorder.record_phase(
        frame,
        1.0,
        "final-cover",
        diagnostics=[{"event": "cover_wait", "status": "observing"}],
    )
    recorder.record(frame, 1.1, [], [], "alive")
    recorder.close()

    trace = [
        json.loads(line)
        for line in (recorder.output_dir / "trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    summary = json.loads(
        (recorder.output_dir / "summary.json").read_text(encoding="utf-8")
    )

    assert [row["phase"] for row in trace] == ["final-cover", "engine"]
    assert summary["phase_counts"] == {"final-cover": 1, "engine": 1}


def test_debug_recorder_can_refresh_session_metadata_before_close(tmp_path):
    recorder = RealtimeDebugRecorder(
        tmp_path,
        video_enabled=False,
        session_metadata={"run_id": "run-123", "final_cover": {"confirmed": False}},
    )
    recorder.update_session_metadata(
        {"run_id": "run-123", "final_cover": {"confirmed": True}}
    )
    recorder.close()

    summary = json.loads(
        (recorder.output_dir / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["session"]["final_cover"] == {"confirmed": True}


def test_debug_recorder_checkpoints_survive_engine_recorder_close(tmp_path):
    recorder = RealtimeDebugRecorder(tmp_path, video_enabled=False)
    frame = np.full((72, 128, 3), 80, dtype=np.uint8)
    recorder.save_checkpoint(
        frame,
        "final-cover",
        "degraded",
        details={"reason": "not visible"},
    )
    recorder.close()
    result_path = recorder.save_checkpoint(frame, "result", "stable")

    checkpoints = [
        json.loads(line)
        for line in (recorder.output_dir / "checkpoints.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    summary = json.loads(
        (recorder.output_dir / "summary.json").read_text(encoding="utf-8")
    )
    assert result_path.is_file()
    assert [item["phase"] for item in checkpoints] == ["final-cover", "result"]
    assert summary["checkpoint_count"] == 2
    assert summary["checkpoint_index"] == "checkpoints.jsonl"


def test_lifecycle_events_can_be_appended_after_engine_close(tmp_path):
    recorder = RealtimeDebugRecorder(tmp_path, video_enabled=False)
    output_dir = recorder.output_dir
    recorder.close()

    path = append_lifecycle_event(
        output_dir,
        "retry",
        "scheduled",
        details={"attempt": 2, "reason": "temporary capture failure"},
    )

    events = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert events[0]["phase"] == "retry"
    assert events[0]["status"] == "scheduled"
    assert events[0]["details"]["attempt"] == 2


def test_debug_recorder_persists_one_session_and_relative_elapsed_time(tmp_path):
    recorder = RealtimeDebugRecorder(
        tmp_path,
        video_fps=30,
        session_metadata={"run_id": "run-123", "song_id": "song-phash-v1-abcd"},
    )
    frame = np.zeros((72, 128, 3), dtype=np.uint8)

    recorder.record(frame, 41.25, [], [], "alive")
    recorder.record(frame, 41.375, [], [], "alive")
    recorder.close()

    trace = [
        json.loads(line)
        for line in (recorder.output_dir / "trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    summary = json.loads(
        (recorder.output_dir / "summary.json").read_text(encoding="utf-8")
    )
    assert [row["elapsed_ms"] for row in trace] == [0.0, 125.0]
    assert summary["session"] == {
        "run_id": "run-123",
        "song_id": "song-phash-v1-abcd",
    }


def test_debug_recorder_rejects_replacing_session_metadata(tmp_path):
    recorder = RealtimeDebugRecorder(tmp_path)
    recorder.set_session_metadata({"run_id": "first"})

    with pytest.raises(RuntimeError, match="session metadata"):
        recorder.set_session_metadata({"run_id": "second"})

    recorder.close()


def test_debug_recorder_snapshots_nested_session_metadata(tmp_path):
    metadata = {"run_id": "stable", "settings": {"tap_effect": 1}}
    recorder = RealtimeDebugRecorder(tmp_path, session_metadata=metadata)
    metadata["settings"]["tap_effect"] = 5

    recorder.close()

    summary = json.loads(
        (recorder.output_dir / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["session"]["settings"] == {"tap_effect": 1}


def test_debug_recorder_saves_screenshot_for_post_release_rescue(tmp_path):
    recorder = RealtimeDebugRecorder(tmp_path, video_fps=30)
    frame = np.full((72, 128, 3), 127, dtype=np.uint8)
    recorder.record(frame, 1.0, [], [
        TouchAction(ActionKind.UP, 4, 1.0, contact=4, reason="tail-ring")
    ], "alive")
    recorder.record(frame, 1.3, [], [
        TouchAction(ActionKind.TAP, 4, 1.3, reason="rescue", track_id=9)
    ], "alive")
    recorder.close()

    events = [json.loads(line) for line in
              (recorder.output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[0]["kind"] == "post-release-rescue"
    assert events[0]["lane"] == 4
    assert events[0]["delay_seconds"] == 0.3
    assert (recorder.output_dir / events[0]["screenshot"]).stat().st_size > 0


def test_debug_video_is_sampled_at_thirty_fps_without_losing_trace(tmp_path):
    recorder = RealtimeDebugRecorder(tmp_path, video_fps=30)
    frame = np.zeros((72, 128, 3), dtype=np.uint8)
    for index in range(6):
        recorder.record(frame, 1.0 + index / 60, [], [], "alive")
    recorder.close()

    summary = json.loads(
        (recorder.output_dir / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["trace_frames"] == 6
    assert summary["video_frames"] == 3
    assert summary["dropped_video_frames"] == 0
    assert summary["video_fps"] == 30


def test_default_debug_video_is_sixty_fps_mjpg_mkv_and_random_seekable(tmp_path):
    recorder = RealtimeDebugRecorder(tmp_path)
    frame = np.zeros((72, 128, 3), dtype=np.uint8)
    for index in range(12):
        frame[:] = index * 10
        recorder.record(frame.copy(), 10.0 + index / 60, [], [], "alive")
        time.sleep(.002)
    recorder.close()

    summary = json.loads(
        (recorder.output_dir / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["video_fps"] == 60
    assert abs(summary["video_actual_fps"] - 60) < 0.1
    assert summary["video_container"] == "matroska"
    assert summary["video_codec"] == "MJPG"
    assert summary["video_seek_verified"] is True
    assert summary["video_finalize_status"] == "verified"
    assert summary["dropped_video_frames"] == 0

    capture = __import__("cv2").VideoCapture(
        str(recorder.output_dir / "playfield.mkv")
    )
    try:
        for index in (0, summary["video_frames"] // 2, summary["video_frames"] - 1):
            assert capture.set(__import__("cv2").CAP_PROP_POS_FRAMES, index)
            ok, image = capture.read()
            assert ok and image is not None
    finally:
        capture.release()


def test_record_returns_while_background_worker_is_still_processing(
    tmp_path, monkeypatch,
):
    recorder = RealtimeDebugRecorder(tmp_path, video_fps=30)
    entered = threading.Event()
    release = threading.Event()

    def slow_process(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(RealtimeDebugRecorder, "_process_record", slow_process)
    frame = np.zeros((72, 128, 3), dtype=np.uint8)

    recorder.record(frame, 1.0, [], [], "alive")

    assert entered.wait(1)
    release.set()
    recorder.close()


def test_slow_record_worker_drops_trace_frames_instead_of_blocking(tmp_path, monkeypatch):
    recorder = RealtimeDebugRecorder(tmp_path, video_fps=30)
    entered = threading.Event()
    release = threading.Event()

    def slow_process(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(RealtimeDebugRecorder, "_process_record", slow_process)
    frame = np.zeros((72, 128, 3), dtype=np.uint8)
    recorder.record(frame, 1.0, [], [], "alive")
    assert entered.wait(1)

    for index in range(200):
        recorder.record(frame, 1.1 + index / 60, [], [], "alive")

    assert recorder._dropped_trace_frames > 0
    release.set()
    recorder.close()
    summary = json.loads(
        (recorder.output_dir / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["dropped_trace_frames"] > 0


def test_elapsed_origin_is_first_attempted_frame_even_when_queue_drops(tmp_path):
    recorder = RealtimeDebugRecorder(tmp_path, video_fps=30)

    recorder._first_timestamp = 10.0
    recorder._process_record(
        np.zeros((72, 128, 3), dtype=np.uint8),
        10.25,
        [],
        [],
        "alive",
        None,
        None,
    )
    recorder.close()

    row = json.loads(
        (recorder.output_dir / "trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert row["elapsed_ms"] == 250.0


def test_close_discards_backlog_and_times_out_without_long_block(tmp_path, monkeypatch):
    recorder = RealtimeDebugRecorder(
        tmp_path,
        video_fps=30,
        close_timeout_seconds=.05,
    )
    entered = threading.Event()
    release = threading.Event()
    original_process = RealtimeDebugRecorder._process_record

    def blocked_process(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_process(self, *args, **kwargs)

    monkeypatch.setattr(RealtimeDebugRecorder, "_process_record", blocked_process)
    frame = np.zeros((72, 128, 3), dtype=np.uint8)
    recorder.record(frame, 1.0, [], [], "alive")
    assert entered.wait(1)
    for index in range(12):
        recorder.record(frame, 1.1 + index / 60, [], [], "alive")

    started = time.perf_counter()
    with pytest.raises(RuntimeError, match="实时调试录像写入失败"):
        recorder.close()
    elapsed = time.perf_counter() - started

    assert elapsed < .5
    assert recorder._record_queue.qsize() <= 1
    summary_path = recorder.output_dir / "summary.json"
    provisional = json.loads(summary_path.read_text(encoding="utf-8"))
    assert provisional["record_worker_finalized"] is False
    assert provisional["encoder_finalized"] is False
    assert "recorder worker did not stop" in provisional["recorder_error"]

    release.set()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        final = json.loads(summary_path.read_text(encoding="utf-8"))
        if final["record_worker_finalized"] and final["encoder_finalized"]:
            break
        time.sleep(.01)

    assert final["record_worker_finalized"] is True
    assert final["encoder_finalized"] is True
    assert final["trace_frames"] == 1
    assert final["video_frames"] == 1
    assert final["dropped_trace_frames"] == 12


def test_encoder_backlog_can_be_discarded_without_blocking(tmp_path):
    recorder = RealtimeDebugRecorder(
        tmp_path,
        video_fps=30,
        close_timeout_seconds=.05,
    )
    recorder._frames.put_nowait(None)
    recorder._encode_thread.join(timeout=1)
    assert not recorder._encode_thread.is_alive()
    frame = np.zeros((72, 128, 3), dtype=np.uint8)
    while True:
        try:
            recorder._frames.put_nowait(frame.copy())
        except queue.Full:
            break

    recorder._discard_pending_video_frames()

    assert recorder._frames.empty()
    assert recorder._dropped_video_frames > 0
    recorder.close()


def test_encoder_timeout_summary_is_provisional_then_atomically_finalized(
    tmp_path, monkeypatch,
):
    entered = threading.Event()
    release = threading.Event()

    class BlockingWriter:
        def isOpened(self):
            return True

        def write(self, _frame):
            entered.set()
            assert release.wait(5)

        def release(self):
            pass

    monkeypatch.setattr(
        "agent.realtime.debug_recorder.cv2.VideoWriter",
        lambda *_args, **_kwargs: BlockingWriter(),
    )
    recorder = RealtimeDebugRecorder(
        tmp_path,
        video_fps=30,
        close_timeout_seconds=.05,
    )
    recorder.record(
        np.zeros((72, 128, 3), dtype=np.uint8),
        1.0,
        [],
        [],
        "alive",
    )
    assert entered.wait(1)

    started = time.perf_counter()
    with pytest.raises(RuntimeError, match="实时调试录像写入失败"):
        recorder.close()
    elapsed = time.perf_counter() - started

    assert elapsed < .5
    summary_path = recorder.output_dir / "summary.json"
    provisional = json.loads(summary_path.read_text(encoding="utf-8"))
    assert provisional["record_worker_finalized"] is True
    assert provisional["encoder_finalized"] is False
    assert "video encoder did not stop" in provisional["recorder_error"]

    release.set()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        final = json.loads(summary_path.read_text(encoding="utf-8"))
        if final["record_worker_finalized"] and final["encoder_finalized"]:
            break
        time.sleep(.01)

    assert final["record_worker_finalized"] is True
    assert final["encoder_finalized"] is True
    assert final["video_frames"] == 1
    assert "video encoder did not stop" in final["recorder_error"]


def test_record_write_failure_is_reported_at_close(tmp_path, monkeypatch):
    recorder = RealtimeDebugRecorder(tmp_path, video_fps=30)

    def boom(self, *args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(RealtimeDebugRecorder, "_process_record", boom)
    frame = np.zeros((72, 128, 3), dtype=np.uint8)

    recorder.record(frame, 1.0, [], [], "alive")

    with pytest.raises(RuntimeError, match="实时调试录像写入失败"):
        recorder.close()
