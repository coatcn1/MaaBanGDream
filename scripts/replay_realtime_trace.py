from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.realtime.note_detector import NoteKind, ObservedNote
from agent.realtime.touch_planner import ActionKind, RealtimePlanner, TouchAction


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


def replay(path: Path) -> dict[str, int]:
    planner = RealtimePlanner(
        judgement_y=565,
        timing_offset_ms=0,
        rescue_first_visible=True,
    )
    recorded: list[TouchAction] = []
    replayed: list[TouchAction] = []
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
            ) for action in frame["actions"])
            replayed.extend(planner.update(notes, now))
    return {
        "recorded_actions": len(recorded),
        "replayed_actions": len(replayed),
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
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a realtime debug JSONL trace")
    parser.add_argument("trace", type=Path)
    args = parser.parse_args()
    print(json.dumps(replay(args.trace), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
