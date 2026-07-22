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

    recorder.record(frame, 1.25, [note], [action], "alive")
    recorder.record(frame, 1.27, [], [], "alive")
    recorder.close()

    trace = [json.loads(line) for line in
             (recorder.output_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    summary = json.loads((recorder.output_dir / "summary.json").read_text(encoding="utf-8"))
    assert len(trace) == 2
    assert trace[0]["notes"][0]["kind"] == "hold"
    assert trace[0]["actions"][0]["kind"] == "down"
    assert summary["trace_frames"] == 2
    assert summary["video_frames"] == 2
    assert (recorder.output_dir / "playfield.avi").stat().st_size > 0
