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


def test_native_life_drop_keeps_bounded_before_and_after_evidence(tmp_path):
    recorder = RealtimeDebugRecorder(tmp_path, video_enabled=False)
    frame = np.zeros((72, 128, 3), dtype=np.uint8)
    # 低频监控保留掉血前画面，不能等到归零弹窗才开始取证。
    for index, value in enumerate([1000] * 12 + [967, 769, 575, 476, 132, 0] + [0] * 15):
        metadata = {"sequence": index, "request_s": index * .2 - .04,
                    "reused": False, "observed_s": index * .2}
        recorder.record_native_life(frame, index * 0.2, value, visible=True,
                                    alive_confirmed=True, frame_metadata=metadata)
        metadata["sequence"] = -1
        # 测试中等待单项消费，避免用人工瞬间灌满队列冒充真实的 5Hz 输入。
        deadline = time.monotonic() + 2
        while not recorder._record_queue.empty() and time.monotonic() < deadline:
            time.sleep(0.001)
    recorder.close()
    events = [json.loads(line) for line in (recorder.output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert 11 <= len(events) <= 21
    assert events[0]["timestamp"] < 2.4
    assert events[-1]["timestamp"] <= 4.6 + 1e-9
    assert len({event["screenshot"] for event in events}) == len(events)
    assert all((recorder.output_dir / event["screenshot"]).is_file() for event in events)
    assert any("life=967" in event["reason"] for event in events)
    assert any("life=0" in event["reason"] for event in events)
    assert all(event["frame_metadata"]["sequence"] >= 0 for event in events)
    assert all(event["frame_metadata"]["observed_s"] == event["timestamp"] for event in events)
    assert all(event["frame_metadata"]["request_s"] == pytest.approx(event["timestamp"] - .04)
               for event in events)
    assert not (recorder.output_dir / "trace.jsonl").read_text(encoding="utf-8")


def test_native_feedback_roi_is_bounded_and_keeps_image_metadata(tmp_path):
    recorder = RealtimeDebugRecorder(tmp_path, video_enabled=False)
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    frame[514:556, 570:710] = 123
    metadata = {"sequence": 9, "request_s": 1.01, "observed_s": 1.05, "reused": False}
    recorder.record_native_feedback(frame, 1.05, metadata, 1.0)
    recorder.record(frame, 1.05, [], [], "alive", frame_metadata=metadata)
    metadata["sequence"] = 10
    frame[:] = 0
    recorder.close()
    event = json.loads((recorder.output_dir / "events.jsonl").read_text(encoding="utf-8"))
    assert event["frame_metadata"]["sequence"] == 9
    assert event["frame_metadata"]["roi"] == [570, 514, 710, 556]
    from agent.realtime.vision_io import imread_unicode
    crop = imread_unicode(recorder.output_dir / event["screenshot"])
    assert crop.shape == (42, 140, 3)
    assert np.all(crop == 123)
    trace = json.loads((recorder.output_dir / "trace.jsonl").read_text(encoding="utf-8"))
    assert trace["frame_metadata"]["sequence"] == 9
    recorder._closed = False
    recorder._record_queue = queue.Queue(maxsize=200)
    for index in range(200):
        recorder.record_native_feedback(frame, index / 60, metadata, 1.0)
    assert recorder._record_queue.qsize() == 19
    recorder._discard_pending_records()
    assert recorder._dropped_native_feedback_frames == 200
    recorder._closed = True
    recorder.record_native_feedback(frame, 9, metadata, 1.0)
    assert recorder._record_queue.empty()


def test_native_life_evidence_ignores_unconfirmed_and_stale_life(tmp_path):
    recorder = RealtimeDebugRecorder(tmp_path, video_enabled=False)
    frame = np.zeros((72, 128, 3), dtype=np.uint8)
    for timestamp, value, visible, alive in [(0, 1000, True, False), (1, 0, True, False),
            (2, 1000, True, True), (3, None, False, True), (4, 0, True, True),
            (5, 1000, True, True), (8, 0, True, True)]:
        recorder.record_native_life(frame, timestamp, value, visible=visible, alive_confirmed=alive)
    recorder.close()
    assert not (recorder.output_dir / "events.jsonl").read_text(encoding="utf-8")


def test_native_life_evidence_queue_saturation_never_blocks(tmp_path):
    recorder = RealtimeDebugRecorder(tmp_path, video_enabled=False)
    recorder.close()
    recorder._closed = False
    recorder._record_queue = queue.Queue(maxsize=1)
    frame = np.zeros((72, 128, 3), dtype=np.uint8)
    recorder.record_native_life(frame, 1, 1000, visible=True, alive_confirmed=True)
    recorder.record_native_life(frame, 2, 0, visible=True, alive_confirmed=True)
    assert recorder._dropped_native_life_frames == 1
    assert recorder._dropped_trace_frames == 0
    recorder._discard_pending_records()
    assert recorder._dropped_native_life_frames == 2
    assert recorder._dropped_trace_frames == 0
    recorder._closed = True


def test_debug_recorder_directory_prefix_reflects_session_kind(tmp_path):
    recorder = RealtimeDebugRecorder(
        tmp_path, video_enabled=False, session_kind="single-rehearsal"
    )
    assert recorder.output_dir.name.startswith("single-rehearsal-")
    recorder.close()


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
        "phase": "engine",
        "frame_metadata": {},
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


def test_debug_recorder_saves_requested_life_candidate_evidence_async(tmp_path):
    recorder = RealtimeDebugRecorder(tmp_path, video_enabled=False)
    frame = np.full((72, 128, 3), 127, dtype=np.uint8)
    recorder.record(
        frame,
        1.0,
        [],
        [],
        "critical",
        [{
            "event": "life_low_candidate",
            "life_value": 15,
            "zero_streak": 1,
            "evidence_screenshot": True,
        }],
    )
    recorder.close()

    events = [
        json.loads(line)
        for line in (recorder.output_dir / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(events) == 1
    event = events[0]
    assert event["frame"] == 0
    assert event["timestamp"] == 1.0
    assert event["kind"] == "life_low_candidate"
    assert event["lane"] == -1
    assert event["reason"] == "life_low_candidate"
    assert event["delay_seconds"] == 0.0
    assert (recorder.output_dir / event["screenshot"]).is_file()


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


def _opening_record(recorder,image,timestamp,phase="engine",**kwargs):
    if "diagnostics" in kwargs:
        kwargs["diagnostics"] = [dict(item, gate_event_observed_s=item.get("gate_event_observed_s",timestamp)) for item in kwargs["diagnostics"]]
    recorder.record_phase(image,timestamp,phase,**kwargs)
    deadline=time.monotonic()+2
    while not recorder._record_queue.empty() and time.monotonic()<deadline:time.sleep(.001)


def test_opening_screenshots_keep_real_timestamps_and_trace_after_cutoff(tmp_path):
    recorder=RealtimeDebugRecorder(tmp_path,opening_seconds=20)
    assert recorder.video_enabled is False
    image=np.zeros((24,32,3),np.uint8)
    _opening_record(recorder,image,10,"native-first-note-gate",frame_metadata={"sequence":1})
    recorder.mark_first_note(11)
    _opening_record(recorder,image,11,frame_metadata={"sequence":2})
    _opening_record(recorder,image,30.9,frame_metadata={"sequence":3})
    _opening_record(recorder,image,31.1,frame_metadata={"sequence":4})
    assert not list(recorder.output_dir.glob("opening/*.png"))
    recorder.close()
    summary=json.loads((recorder.output_dir/'summary.json').read_text(encoding='utf-8'))
    assert summary['recording_mode']=='opening-screenshots+trace'
    assert summary['trace_frames']==4 and summary['video_frames']==0
    assert summary['opening_screenshots_written']==3
    assert summary['opening_window_reached'] and not summary['opening_truncated']
    assert summary['opening_flush_status']=='complete'
    mapping=[json.loads(line) for line in (recorder.output_dir/'opening_frames.jsonl').read_text(encoding='utf-8').splitlines()]
    assert [row['observed_timestamp'] for row in mapping]==[10,11,30.9]
    assert mapping[0]['first_note_relative_ms']==-1000
    assert all((recorder.output_dir/row['path']).is_file() for row in mapping)
    assert not list(recorder.output_dir.glob('*.mkv'))
    assert not (recorder.output_dir/'video_frames.jsonl').exists()
    assert all('held' not in row for row in mapping)


def test_opening_buffer_preserves_early_rejected_candidate_and_recent_trajectory(tmp_path):
    recorder=RealtimeDebugRecorder(tmp_path,opening_seconds=20)
    image=np.zeros((12,16,3),np.uint8)
    _opening_record(recorder,image,0,'preparation-identity')
    _opening_record(recorder,image,1,'native-first-note-gate',diagnostics=[{'event':'native-start-observation','gate_event':{'event':'ignored-prelude'}}])
    for stamp in range(2,160):_opening_record(recorder,image,float(stamp),'native-first-note-gate')
    recorder.mark_first_note(160)
    for index in range(101):_opening_record(recorder,image,160+index*.2)
    _opening_record(recorder,image,181)
    recorder.close()
    mapping=[json.loads(line) for line in (recorder.output_dir/'opening_frames.jsonl').read_text(encoding='utf-8').splitlines()]
    assert len(mapping)<=128
    assert any(row['observed_timestamp']==1 and 'gate-event:ignored-prelude' in row['retention_reasons'] for row in mapping)
    assert mapping[0]['phase']=='preparation-identity'
    assert any(row['observed_timestamp']==159 for row in mapping)
    assert any(row['observed_timestamp']>=179 for row in mapping)
    summary=json.loads((recorder.output_dir/'summary.json').read_text(encoding='utf-8'))
    assert summary['opening_capacity_evictions']>0 and summary['opening_sampled_out']>0


def test_opening_reused_or_duplicate_sequence_never_writes_fake_png(tmp_path):
    recorder=RealtimeDebugRecorder(tmp_path,opening_seconds=20)
    image=np.zeros((12,16,3),np.uint8)
    for stamp,metadata in [(1,{'sequence':1}),(2,{'sequence':1}),(3,{'sequence':2,'reused':True}),(4,{'sequence':3,'fresh':False})]:
        _opening_record(recorder,image,stamp,'native-first-note-gate',frame_metadata=metadata)
    recorder.close()
    summary=json.loads((recorder.output_dir/'summary.json').read_text(encoding='utf-8'))
    assert summary['opening_screenshots_written']==1
    assert summary['opening_duplicate_frames']==3
    assert summary['opening_truncated'] is True


def test_opening_png_failure_is_reported_only_after_flush(monkeypatch,tmp_path):
    recorder=RealtimeDebugRecorder(tmp_path,opening_seconds=20)
    _opening_record(recorder,np.zeros((12,16,3),np.uint8),1)
    monkeypatch.setattr('agent.realtime.debug_recorder.imwrite_unicode',lambda *a:False)
    with pytest.raises(RuntimeError):recorder.close()
    summary=json.loads((recorder.output_dir/'summary.json').read_text(encoding='utf-8'))
    assert summary['opening_flush_status']=='failed'


def test_opening_priority_pins_survive_event_kind_pressure_and_flush_is_deferred(tmp_path):
    recorder=RealtimeDebugRecorder(tmp_path,opening_seconds=20)
    image=np.zeros((12,16,3),np.uint8)
    checkpoint=recorder.save_checkpoint(image,'cached-preparation','unknown-time')
    for index in range(20):
        _opening_record(recorder,image,float(index),'native-first-note-gate',diagnostics=[{'gate_event':{'event':f'event-{index}'}}])
    _opening_record(recorder,image,20,'native-first-note-gate',diagnostics=[{'gate_event':{'event':'ignored-prelude'}}])
    _opening_record(recorder,image,21,'native-first-note-gate',frame_metadata={'black':True})
    _opening_record(recorder,image,22,'native-first-note-gate',diagnostics=[{'triggered':True}])
    for stamp in range(23,100):_opening_record(recorder,image,float(stamp),'native-first-note-gate')
    assert not checkpoint.exists()
    assert not list(recorder.output_dir.rglob('*.png'))
    recorder.close()
    rows=[json.loads(line) for line in (recorder.output_dir/'opening_frames.jsonl').read_text(encoding='utf-8').splitlines()]
    timestamps={row['observed_timestamp'] for row in rows}
    assert {20,21,22}.issubset(timestamps)
    assert checkpoint.exists()
    assert len(rows)+1<=128


def test_opening_index_failure_does_not_publish_complete(monkeypatch,tmp_path):
    recorder=RealtimeDebugRecorder(tmp_path,opening_seconds=20)
    _opening_record(recorder,np.zeros((12,16,3),np.uint8),1)
    monkeypatch.setattr('agent.realtime.debug_recorder.imwrite_unicode',lambda *a:False)
    with pytest.raises(RuntimeError):recorder.close()
    summary=json.loads((recorder.output_dir/'summary.json').read_text(encoding='utf-8'))
    assert summary['opening_flush_status']=='failed'
    assert summary['recorder_error']


def test_opening_checkpoint_after_close_keeps_low_frequency_api(tmp_path):
    recorder=RealtimeDebugRecorder(tmp_path,opening_seconds=20)
    recorder.close()
    path=recorder.save_checkpoint(np.zeros((12,16,3),np.uint8),'result','complete')
    assert path.exists()


def test_opening_old_gate_event_state_does_not_pin_an_unrelated_current_png(tmp_path):
    recorder=RealtimeDebugRecorder(tmp_path,opening_seconds=20)
    image=np.zeros((12,16,3),np.uint8)
    _opening_record(recorder,image,2,'native-first-note-gate',diagnostics=[{
        'gate_event':{'event':'ignored-prelude'},'gate_event_observed_s':1.0}])
    for stamp in range(3,50):_opening_record(recorder,image,float(stamp),'native-first-note-gate')
    recorder.close()
    rows=[json.loads(line) for line in (recorder.output_dir/'opening_frames.jsonl').read_text(encoding='utf-8').splitlines()]
    assert not any('gate-event:ignored-prelude' in row['retention_reasons'] for row in rows)


def test_opening_keeps_full_life_window_and_feedback_separate_from_checkpoints(tmp_path):
    from agent.realtime.vision_io import imread_unicode

    recorder = RealtimeDebugRecorder(tmp_path, opening_seconds=20)
    for index in range(21):
        image = np.full((720, 1280, 3), index, np.uint8)
        recorder.record_native_life(
            image, index * .2, 1000 if index < 10 else 900,
            visible=True, alive_confirmed=True, frame_metadata={"pixel": index},
        )
        deadline = time.monotonic() + 2
        while not recorder._record_queue.empty() and time.monotonic() < deadline:
            time.sleep(.001)
    for index in range(5):
        recorder.record_native_feedback(
            np.full((720, 1280, 3), 30 + index, np.uint8), 21 + index,
            {"pixel": 30 + index}, 20,
        )
    checkpoint = recorder.save_checkpoint(
        np.zeros((12, 16, 3), np.uint8), "result", "pending",
    )
    assert not list(recorder.output_dir.rglob("*.png"))
    recorder.close()
    events = [json.loads(line) for line in
              (recorder.output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(events) == 26
    for event in events:
        assert event["screenshot"] is not None
        pixels = imread_unicode(recorder.output_dir / event["screenshot"])
        assert np.all(pixels == event["frame_metadata"]["pixel"])
    assert checkpoint.is_file()
    summary = json.loads((recorder.output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["native_life_evidence_frames"] == 21
    assert summary["native_feedback_evidence_frames"] == 5
    assert summary["event_screenshots"] == 26
    assert summary["native_feedback_evidence_complete"] is True


def test_opening_event_and_checkpoint_overflow_have_no_false_paths(tmp_path):
    recorder = RealtimeDebugRecorder(tmp_path, opening_seconds=20)
    image = np.zeros((12, 16, 3), np.uint8)
    for index in range(67):
        _opening_record(recorder, image, index, diagnostics=[{
            "event": f"native-feedback-00-{index:03d}", "evidence_screenshot": True,
        }])
    paths = [recorder.save_checkpoint(image, "gate", str(index)) for index in range(10)]
    assert paths[8:] == [None, None]
    assert not list(recorder.output_dir.rglob("*.png"))
    recorder.close()
    events = [json.loads(line) for line in
              (recorder.output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    checkpoints = [json.loads(line) for line in
                   (recorder.output_dir / "checkpoints.jsonl").read_text(encoding="utf-8").splitlines()]
    for row in events + checkpoints:
        if row["screenshot"] is not None:
            assert (recorder.output_dir / row["screenshot"]).is_file()
        else:
            assert row["screenshot_status"] == "dropped-capacity"
    assert sorted(row["event_index"] for row in events) == list(range(67))
    summary = json.loads((recorder.output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["event_screenshot_capacity"] == 64
    assert summary["event_screenshots_requested"] == 67
    assert summary["event_screenshots"] == 64
    assert summary["event_screenshot_drop_reasons"] == {"dropped-capacity": 3}
    assert summary["checkpoint_requested"] == 10
    assert summary["checkpoint_count"] == 8
    assert summary["opening_checkpoint_dropped"] == 2
    assert summary["dropped_native_feedback_frames"] == 3
    assert summary["native_feedback_evidence_complete"] is False


def test_opening_event_write_failure_has_null_path_and_incomplete_summary(tmp_path, monkeypatch):
    recorder = RealtimeDebugRecorder(tmp_path, opening_seconds=20)
    recorder.record_native_feedback(np.zeros((720, 1280, 3), np.uint8), 21, {}, 20)
    monkeypatch.setattr("agent.realtime.debug_recorder.imwrite_unicode", lambda *args: False)
    with pytest.raises(RuntimeError):
        recorder.close()
    event = json.loads((recorder.output_dir / "events.jsonl").read_text(encoding="utf-8"))
    assert event["screenshot"] is None
    assert event["screenshot_status"] == "write-failed"
    summary = json.loads((recorder.output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["event_screenshots"] == 0
    assert summary["native_feedback_evidence_frames"] == 0
    assert summary["event_screenshot_drop_reasons"] == {"write-failed": 1}
    assert summary["event_screenshot_flush_status"] == "failed"
    assert summary["opening_flush_status"] == "failed"
    assert summary["native_feedback_evidence_complete"] is False


def test_opening_event_flush_continues_after_one_png_failure(tmp_path, monkeypatch):
    from agent.realtime.debug_recorder import imwrite_unicode

    recorder = RealtimeDebugRecorder(tmp_path, opening_seconds=20)
    for pixel in range(20, 23):
        recorder.record_native_feedback(
            np.full((720, 1280, 3), pixel, np.uint8), pixel, {"pixel": pixel}, 20,
        )
    def selective_write(path, image):
        if path.parent.name == "events" and np.all(image == 20):
            return False
        return imwrite_unicode(path, image)
    monkeypatch.setattr("agent.realtime.debug_recorder.imwrite_unicode", selective_write)
    with pytest.raises(RuntimeError):
        recorder.close()
    events = [json.loads(line) for line in
              (recorder.output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["event_index"] for row in events] == [0, 1, 2]
    assert events[0]["screenshot"] is None
    assert events[0]["screenshot_status"] == "write-failed"
    assert all((recorder.output_dir / row["screenshot"]).is_file() for row in events[1:])
    summary = json.loads((recorder.output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["event_screenshots"] == 2
    assert summary["native_feedback_evidence_frames"] == 2
    assert summary["event_screenshot_flush_status"] == "failed"


def test_opening_late_worker_flush_keeps_evidence_incomplete_after_timeout(tmp_path, monkeypatch):
    recorder = RealtimeDebugRecorder(tmp_path, opening_seconds=20, close_timeout_seconds=.02)
    entered, release = threading.Event(), threading.Event()
    original = recorder._flush_opening_events

    def delayed_flush():
        entered.set()
        assert release.wait(2)
        original()

    monkeypatch.setattr(recorder, "_flush_opening_events", delayed_flush)
    recorder.record_native_feedback(np.zeros((720, 1280, 3), np.uint8), 21, {}, 20)
    with pytest.raises(RuntimeError):
        recorder.close()
    assert entered.is_set()
    provisional = json.loads((recorder.output_dir / "summary.json").read_text(encoding="utf-8"))
    assert provisional["record_worker_finalized"] is False
    assert provisional["native_feedback_evidence_complete"] is False
    release.set()
    recorder._summary_finalizer_thread.join(timeout=2)
    path = recorder.save_checkpoint(np.zeros((12, 16, 3), np.uint8), "result", "complete")
    assert path.is_file()
    final = json.loads((recorder.output_dir / "summary.json").read_text(encoding="utf-8"))
    assert final["record_worker_finalized"] is True
    assert final["native_feedback_evidence_frames"] == 1
    assert final["native_feedback_evidence_complete"] is False
    assert final["checkpoint_count"] == 1
    assert final["recorder_error"]


def test_final_summary_snapshot_cannot_overwrite_a_late_checkpoint(tmp_path, monkeypatch):
    recorder = RealtimeDebugRecorder(tmp_path, opening_seconds=20)
    recorder.close()
    snapshot_ready, checkpoint_refresh_requested = threading.Event(), threading.Event()
    release_snapshot = threading.Event()
    snapshot_lock_held = []
    failures = []
    original_payload = recorder._summary_payload
    original_refresh = recorder._refresh_checkpoint_summary

    def paused_payload(**kwargs):
        payload = original_payload(**kwargs)
        snapshot_lock_held.append(recorder._summary_lock.locked())
        snapshot_ready.set()
        assert release_snapshot.wait(2)
        return payload

    def checkpoint_refresh():
        checkpoint_refresh_requested.set()
        original_refresh()

    def run_guarded(operation):
        try:
            operation()
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(recorder, "_summary_payload", paused_payload)
    monkeypatch.setattr(recorder, "_refresh_checkpoint_summary", checkpoint_refresh)
    finalizer = threading.Thread(target=lambda: run_guarded(lambda: recorder._write_summary(
        record_worker_finalized=True, encoder_finalized=True,
    )))
    checkpoint = threading.Thread(target=lambda: run_guarded(lambda: recorder.save_checkpoint(
        np.zeros((12, 16, 3), np.uint8), "result", "complete",
    )))
    finalizer.start()
    try:
        assert snapshot_ready.wait(2)
        checkpoint.start()
        assert checkpoint_refresh_requested.wait(2)
    finally:
        release_snapshot.set()
        finalizer.join(timeout=2)
        if checkpoint.ident is not None:
            checkpoint.join(timeout=2)
    assert not finalizer.is_alive() and not checkpoint.is_alive()
    assert not failures
    # 快照和替换必须共享锁；否则旧快照可在补写成功后覆盖最新计数。
    assert snapshot_lock_held == [True]
    summary = json.loads((recorder.output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["checkpoint_count"] == 1
    assert summary["checkpoint_requested"] == 1
    assert len(list(recorder.output_dir.glob("checkpoints/*.png"))) == 1
