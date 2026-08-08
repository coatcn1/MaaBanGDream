from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.realtime.note_detector import NoteKind, ObservedNote
from agent.realtime.touch_planner import (
    ActionKind,
    RealtimePlanner,
    TouchAction,
    sliding_holds_enabled,
)


def trace_replay_metadata(path: Path) -> dict[str, object]:
    """Read replay construction values recorded beside a modern trace."""
    summary_path = path.with_name("summary.json")
    if not summary_path.is_file():
        return {}
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    session = summary.get("session")
    timing = summary.get("timing_feedback")
    metadata: dict[str, object] = {}
    if isinstance(session, dict):
        difficulty = session.get("difficulty")
        if isinstance(difficulty, str) and difficulty:
            metadata["difficulty"] = difficulty
    if isinstance(timing, dict):
        initial_offset = timing.get("initial_offset_ms")
        if isinstance(initial_offset, int) and not isinstance(initial_offset, bool):
            metadata["timing_offset_ms"] = initial_offset
    return metadata


def duplicate_judgements(actions: list[TouchAction], window: float = .12) -> int:
    transients = [
        action for action in actions
        if action.kind in (ActionKind.TAP, ActionKind.FLICK)
    ]
    return sum(
        current.timestamp - previous.timestamp < window
        and abs(current.lane - previous.lane) <= 1
        for previous, current in zip(transients, transients[1:])
    )


def post_release_rescues(actions: list[TouchAction], window: float = .65) -> int:
    total = 0
    for index, released in enumerate(actions):
        if released.kind != ActionKind.UP:
            continue
        for action in actions[index + 1:]:
            delay = action.timestamp - released.timestamp
            if delay > window:
                break
            if (
                action.lane == released.lane
                and action.kind in (ActionKind.TAP, ActionKind.DOWN, ActionKind.FLICK)
                and action.reason == "rescue"
            ):
                total += 1
    return total


def transformed_trace_frames(
    path: Path,
    *,
    inject_gap_ms: int = 0,
    drop_frames: int = 0,
    fault_after_frame: int | None = None,
):
    """Yield trace rows after deterministic clock-gap/frame-drop injection.

    The transformation is intentionally offline-only. It lets planner replay
    exercise the same long-frame boundary seen in production without adding
    sleeps or fault hooks to the realtime engine.
    """
    if inject_gap_ms < 0:
        raise ValueError("inject_gap_ms must be non-negative")
    if drop_frames < 0:
        raise ValueError("drop_frames must be non-negative")
    with path.open(encoding="utf-8") as stream:
        frames = [json.loads(line) for line in stream if line.strip()]
    if not frames:
        return
    if fault_after_frame is None:
        fault_after_frame = max(0, len(frames) // 2 - 1)
    if not 0 <= fault_after_frame < len(frames):
        raise ValueError("fault_after_frame is outside the trace")

    first_dropped = fault_after_frame + 1
    last_dropped = first_dropped + drop_frames
    shift_seconds = inject_gap_ms / 1000.0
    for index, source in enumerate(frames):
        if first_dropped <= index < last_dropped:
            continue
        if index <= fault_after_frame or shift_seconds == 0:
            yield source
            continue
        frame = dict(source)
        frame["timestamp"] = float(source["timestamp"]) + shift_seconds
        frame["notes"] = [
            {
                **note,
                "timestamp": float(note["timestamp"]) + shift_seconds,
            }
            for note in source.get("notes", [])
        ]
        frame["actions"] = [
            {
                **action,
                "timestamp": float(action["timestamp"]) + shift_seconds,
            }
            for action in source.get("actions", [])
        ]
        yield frame


def replay(
    path: Path,
    *,
    timing_offset_ms: int = 0,
    difficulty: str = "Hard",
    collect: bool = False,
    use_recorded_timing_feedback: bool = True,
    inject_gap_ms: int = 0,
    drop_frames: int = 0,
    fault_after_frame: int | None = None,
) -> dict[str, object]:
    planner = RealtimePlanner(
        judgement_y=565,
        timing_offset_ms=timing_offset_ms,
        rescue_first_visible=True,
        enable_slide=sliding_holds_enabled(difficulty),
    )
    recorded: list[TouchAction] = []
    replayed: list[TouchAction] = []
    diagnostics: list[dict[str, object]] = []
    last_now = 0.0
    applied_timing_offset_ms = timing_offset_ms
    recorded_timing_adjustments = 0
    for frame in transformed_trace_frames(
        path,
        inject_gap_ms=inject_gap_ms,
        drop_frames=drop_frames,
        fault_after_frame=fault_after_frame,
    ):
            now = float(frame["timestamp"])
            last_now = now
            timing_feedback = frame.get("timing_feedback")
            if use_recorded_timing_feedback and isinstance(timing_feedback, dict):
                recorded_offset = timing_feedback.get("current_offset_ms")
                if (
                    isinstance(recorded_offset, int)
                    and not isinstance(recorded_offset, bool)
                    and recorded_offset != applied_timing_offset_ms
                ):
                    planner.set_timing_offset_ms(recorded_offset)
                    applied_timing_offset_ms = recorded_offset
                    recorded_timing_adjustments += 1
            notes = [ObservedNote(
                NoteKind(note["kind"]),
                int(note["lane"]),
                float(note["x"]),
                float(note["y"]),
                int(note["width"]),
                int(note["height"]),
                float(note["timestamp"]),
                float(note.get("hold_body_confidence", 1.0)),
                bool(note.get("hold_tail_flick", False)),
            ) for note in frame["notes"]]
            recorded.extend(TouchAction(
                ActionKind(action["kind"]),
                int(action["lane"]),
                float(action["timestamp"]),
                action.get("contact"),
                str(action.get("reason", "")),
                action.get("track_id"),
                action.get("target_x"),
            ) for action in frame.get("actions", []))
            replayed.extend(planner.update(notes, now))
            diagnostics.extend(planner.drain_diagnostics())
    active_holds_after_replay = bool(planner.has_active_holds)
    cleanup_actions = planner.reset(last_now)
    hold_releases = [
        event for event in diagnostics
        if event.get("event") == "hold_release"
    ]
    result = {
        "recorded_actions": len(recorded),
        "replayed_actions": len(replayed),
        "recorded_structural_actions": {
            kind.value: sum(action.kind == kind for action in recorded)
            for kind in (ActionKind.DOWN, ActionKind.MOVE, ActionKind.UP)
        },
        "replayed_structural_actions": {
            kind.value: sum(action.kind == kind for action in replayed)
            for kind in (ActionKind.DOWN, ActionKind.MOVE, ActionKind.UP)
        },
        "recorded_transient_actions": sum(
            action.kind in (ActionKind.TAP, ActionKind.FLICK)
            for action in recorded
        ),
        "replayed_transient_actions": sum(
            action.kind in (ActionKind.TAP, ActionKind.FLICK)
            for action in replayed
        ),
        "recorded_duplicate_judgements": duplicate_judgements(recorded),
        "replayed_duplicate_judgements": duplicate_judgements(replayed),
        "recorded_post_release_rescues": post_release_rescues(recorded),
        "replayed_post_release_rescues": post_release_rescues(replayed),
        "active_holds_after_replay": active_holds_after_replay,
        "cleanup_actions": len(cleanup_actions),
        "cleanup_up_actions": sum(
            action.kind is ActionKind.UP for action in cleanup_actions
        ),
        "recorded_linked_tail_taps": sum(
            action.reason == "linked-tail" for action in recorded
        ),
        "replayed_linked_tail_taps": sum(
            action.reason == "linked-tail" for action in replayed
        ),
        "filtered_adjacent_artifacts": planner.filtered_adjacent_artifacts,
        "rejected_hold_candidates": planner.rejected_hold_candidates,
        "predicted_releases_under_200_ms": sum(
            event.get("event") == "hold_release"
            and event.get("release_method") == "predicted-tail"
            and int(event.get("duration_ms", 0)) < 200
            for event in diagnostics
        ),
        "replayed_releases_under_300_ms": sum(
            int(event.get("duration_ms", 0)) < 300
            for event in hold_releases
        ),
        "minimum_replayed_hold_duration_ms": min(
            (int(event.get("duration_ms", 0)) for event in hold_releases),
            default=None,
        ),
        "maximum_replayed_hold_duration_ms": max(
            (int(event.get("duration_ms", 0)) for event in hold_releases),
            default=None,
        ),
        "average_replayed_hold_duration_ms": (
            round(sum(
                int(event.get("duration_ms", 0))
                for event in hold_releases
            ) / len(hold_releases))
            if hold_releases else None
        ),
        "cross_lane_releases": sum(
            event.get("contact") != event.get("final_lane")
            for event in hold_releases
        ),
        "replayed_release_methods": {
            method: sum(
                event.get("release_method") == method
                for event in hold_releases
            )
            for method in sorted({
                str(event.get("release_method", "unknown"))
                for event in hold_releases
            })
        },
        "diagnostic_counts": {
            event: sum(item.get("event") == event for item in diagnostics)
            for event in sorted({
                str(item.get("event", "unknown")) for item in diagnostics
            })
        },
        "replay_timing": {
            "initial_offset_ms": timing_offset_ms,
            "final_offset_ms": applied_timing_offset_ms,
            "recorded_feedback_enabled": use_recorded_timing_feedback,
            "recorded_adjustments": recorded_timing_adjustments,
        },
    }
    if collect:
        result["actions_sequence"] = [
            {
                "kind": action.kind.value,
                "lane": action.lane,
                "timestamp": action.timestamp,
                "contact": action.contact,
                "reason": action.reason,
                "track_id": action.track_id,
                "target_x": action.target_x,
            }
            for action in replayed
        ]
        result["diagnostics_sequence"] = diagnostics
    if inject_gap_ms or drop_frames:
        result["fault_injection"] = {
            "inject_gap_ms": inject_gap_ms,
            "drop_frames": drop_frames,
            "fault_after_frame": fault_after_frame,
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a realtime debug JSONL trace")
    parser.add_argument("trace", type=Path)
    parser.add_argument(
        "--timing-offset-ms",
        type=int,
        default=None,
        help="Initial planner offset (default: adjacent summary metadata or 0)",
    )
    parser.add_argument(
        "--difficulty",
        default=None,
        help=(
            "Recorded difficulty (default: adjacent summary metadata; required "
            "for legacy traces without session metadata)"
        ),
    )
    parser.add_argument(
        "--fixed-timing-offset",
        action="store_true",
        help=(
            "Ignore per-frame timing_feedback values and keep the initial "
            "planner offset fixed"
        ),
    )
    parser.add_argument(
        "--inject-gap-ms",
        type=int,
        default=0,
        help="Shift the replay clock after the fault boundary by this amount",
    )
    parser.add_argument(
        "--drop-frames",
        type=int,
        default=0,
        help="Drop this many frames immediately after the fault boundary",
    )
    parser.add_argument(
        "--fault-after-frame",
        type=int,
        default=None,
        help="Zero-based frame after which to inject faults (default: midpoint)",
    )
    parser.add_argument(
        "--dump-actions",
        type=Path,
        default=None,
        help="Write the full replayed action and diagnostic sequences as JSON",
    )
    parser.add_argument(
        "--benchmark",
        type=int,
        default=0,
        metavar="N",
        help="Replay N times and report wall-time statistics instead",
    )
    args = parser.parse_args()
    metadata = trace_replay_metadata(args.trace)
    difficulty = args.difficulty or metadata.get("difficulty")
    if not isinstance(difficulty, str) or not difficulty:
        parser.error(
            "--difficulty is required for legacy traces without session metadata"
        )
    timing_offset_ms = (
        args.timing_offset_ms
        if args.timing_offset_ms is not None
        else int(metadata.get("timing_offset_ms", 0))
    )
    if args.benchmark > 0:
        times = []
        for _ in range(args.benchmark):
            started = time.perf_counter()
            replay(
                args.trace,
                timing_offset_ms=timing_offset_ms,
                difficulty=difficulty,
                use_recorded_timing_feedback=not args.fixed_timing_offset,
                inject_gap_ms=args.inject_gap_ms,
                drop_frames=args.drop_frames,
                fault_after_frame=args.fault_after_frame,
            )
            times.append(time.perf_counter() - started)
        print(json.dumps({
            "runs": args.benchmark,
            "min_seconds": round(min(times), 4),
            "median_seconds": round(statistics.median(times), 4),
            "mean_seconds": round(statistics.fmean(times), 4),
        }, indent=2))
        return
    result = replay(
        args.trace,
        timing_offset_ms=timing_offset_ms,
        difficulty=difficulty,
        collect=args.dump_actions is not None,
        use_recorded_timing_feedback=not args.fixed_timing_offset,
        inject_gap_ms=args.inject_gap_ms,
        drop_frames=args.drop_frames,
        fault_after_frame=args.fault_after_frame,
    )
    if args.dump_actions is not None:
        payload = {
            "trace": args.trace.name,
            "difficulty": difficulty,
            "timing_offset_ms": timing_offset_ms,
            "actions": result.pop("actions_sequence"),
            "diagnostics": result.pop("diagnostics_sequence"),
        }
        args.dump_actions.parent.mkdir(parents=True, exist_ok=True)
        args.dump_actions.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
