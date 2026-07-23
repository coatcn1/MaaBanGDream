from __future__ import annotations

import argparse
import json
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


def post_release_rescues(actions: list[TouchAction], window: float = .4) -> int:
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


def replay(
    path: Path,
    *,
    timing_offset_ms: int = 0,
    difficulty: str = "Hard",
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
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            frame = json.loads(line)
            now = float(frame["timestamp"])
            notes = [ObservedNote(
                NoteKind(note["kind"]),
                int(note["lane"]),
                float(note["x"]),
                float(note["y"]),
                int(note["width"]),
                int(note["height"]),
                float(note["timestamp"]),
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
    hold_releases = [
        event for event in diagnostics
        if event.get("event") == "hold_release"
    ]
    return {
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
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a realtime debug JSONL trace")
    parser.add_argument("trace", type=Path)
    parser.add_argument("--timing-offset-ms", type=int, default=0)
    parser.add_argument("--difficulty", default="Hard")
    args = parser.parse_args()
    print(json.dumps(
        replay(
            args.trace,
            timing_offset_ms=args.timing_offset_ms,
            difficulty=args.difficulty,
        ),
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
