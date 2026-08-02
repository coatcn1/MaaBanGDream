"""Benchmark NoteDetector.detect() on sampled recording frames.

Decodes every K-th frame of a playfield.avi into memory once, then times the
detector over those frames. Video decode is excluded from the timings (the
live hot path receives frames from the controller, not from a file).
"""
from __future__ import annotations

import argparse
import cProfile
import io
import json
import pstats
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2

from agent.realtime.note_detector import NoteDetector


def sample_frames(video: Path, max_frames: int) -> tuple[list, int]:
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise SystemExit(f"cannot open video: {video}")
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    stride = max(1, total // max_frames)
    frames = []
    index = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        if index % stride == 0:
            frames.append(frame)
        index += 1
    capture.release()
    return frames, stride


def run_detection(frames: list) -> list[list[dict]]:
    detector = NoteDetector()
    per_frame = []
    for index, frame in enumerate(frames):
        notes = detector.detect(frame, index / 30.0)
        per_frame.append([
            {
                "kind": note.kind.value,
                "lane": note.lane,
                "x": note.x,
                "y": note.y,
                "width": note.width,
                "height": note.height,
                "timestamp": note.timestamp,
                "hold_body_confidence": note.hold_body_confidence,
                "hold_tail_flick": note.hold_tail_flick,
            }
            for note in notes
        ])
    return per_frame


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--max-frames", type=int, default=150)
    parser.add_argument("--dump-detections", type=Path, default=None)
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()

    frames, stride = sample_frames(args.video, args.max_frames)
    print(f"sampled {len(frames)} frames (stride {stride}) from {args.video.name}")

    times = []
    detections = None
    for _ in range(max(1, args.repeat)):
        started = time.perf_counter()
        detections = run_detection(frames)
        times.append(time.perf_counter() - started)
    per_frame_ms = [elapsed / len(frames) * 1000 for elapsed in times]
    report = {
        "video": args.video.name,
        "sampled_frames": len(frames),
        "stride": stride,
        "repeat": len(times),
        "detect_ms_per_frame": {
            "min": round(min(per_frame_ms), 3),
            "median": round(statistics.median(per_frame_ms), 3),
            "mean": round(statistics.fmean(per_frame_ms), 3),
        },
        "detect_notes_total": sum(len(frame) for frame in detections),
    }
    print(json.dumps(report, indent=2))

    if args.dump_detections is not None:
        args.dump_detections.parent.mkdir(parents=True, exist_ok=True)
        args.dump_detections.write_text(
            json.dumps(
                {"video": args.video.name, "stride": stride, "frames": detections},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"detections written to {args.dump_detections}")

    if args.profile:
        profiler = cProfile.Profile()
        profiler.enable()
        run_detection(frames)
        profiler.disable()
        stream = io.StringIO()
        pstats.Stats(profiler, stream=stream).sort_stats("tottime").print_stats(20)
        print(stream.getvalue())


if __name__ == "__main__":
    main()
