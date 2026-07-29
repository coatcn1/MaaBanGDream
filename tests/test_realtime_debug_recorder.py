from __future__ import annotations

import json

import numpy as np

from agent.realtime.debug_recorder import RealtimeDebugRecorder
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
    assert len(trace) == 2
    assert trace[0]["notes"][0]["kind"] == "hold"
    assert trace[0]["actions"][0]["kind"] == "down"
    assert trace[0]["diagnostics"][0]["event"] == "hold_start"
    assert trace[0]["timing_feedback"]["current_offset_ms"] == 1
    assert summary["trace_frames"] == 2
    assert summary["video_frames"] == 1
    assert summary["diagnostic_counts"] == {"hold_start": 1}
    assert summary["timing_feedback"]["current_offset_ms"] == 1
    assert (recorder.output_dir / "playfield.avi").stat().st_size > 0


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
    assert summary["video_fps"] == 30
