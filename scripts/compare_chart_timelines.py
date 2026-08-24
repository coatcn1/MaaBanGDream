"""Compare two realtime traces to decide whether they played the same chart.

The game's song-perceptual hash is computed from the song-select list UI and
is not reliable enough to prove that two runs used the same chart (it can
collide across songs and difficulties).  This tool instead compares the
timeline of dispatched TAP/FLICK actions per lane between two trace.jsonl
files:

1. extract per-lane transient-action times (elapsed_ms);
2. match each action in A to the nearest unmatched action in B within a
   window, per lane;
3. estimate a global start-offset as the median of matched deltas;
4. report residual spread after removing that offset.

Same-chart runs show a nearly constant offset: median |residual| of a few
milliseconds and high match rates.  Different charts (or charts with many
missed notes) show large, spread-out residuals and many unmatched actions.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path


MATCH_WINDOW_MS = 400.0


def load_trace_actions(trace_path: Path) -> dict[int, list[float]]:
    """Return {lane: [elapsed_ms, ...]} for dispatched TAP/FLICK actions."""
    per_lane: dict[int, list[float]] = defaultdict(list)
    with trace_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            for action in row.get("actions", []):
                if action.get("kind") not in {"tap", "flick"}:
                    continue
                lane = int(action["lane"])
                per_lane[lane].append(float(row["elapsed_ms"]))
    for lane in per_lane:
        per_lane[lane].sort()
    return per_lane


def match_sequences(
    left: list[float],
    right: list[float],
    *,
    window_ms: float = MATCH_WINDOW_MS,
) -> list[tuple[float, float]]:
    """Greedy nearest-neighbour matching preserving order on both sides."""
    pairs: list[tuple[float, float]] = []
    i = j = 0
    while i < len(left) and j < len(right):
        delta = right[j] - left[i]
        if delta < -window_ms:
            j += 1
        elif delta > window_ms:
            i += 1
        else:
            pairs.append((left[i], right[j]))
            i += 1
            j += 1
    return pairs


def compare_traces(left: Path, right: Path) -> dict[str, object]:
    left_actions = load_trace_actions(left)
    right_actions = load_trace_actions(right)
    total_left = sum(len(values) for values in left_actions.values())
    total_right = sum(len(values) for values in right_actions.values())
    all_deltas: list[float] = []
    all_residuals: list[float] = []
    matched = 0
    per_lane: dict[str, object] = {}
    for lane in sorted(set(left_actions) | set(right_actions)):
        pairs = match_sequences(
            left_actions.get(lane, []),
            right_actions.get(lane, []),
        )
        deltas = [right_time - left_time for left_time, right_time in pairs]
        offset = statistics.median(deltas) if deltas else 0.0
        residuals = [abs(delta - offset) for delta in deltas]
        all_deltas.extend(deltas)
        all_residuals.extend(residuals)
        matched += len(pairs)
        per_lane[str(lane)] = {
            "left_actions": len(left_actions.get(lane, [])),
            "right_actions": len(right_actions.get(lane, [])),
            "matched": len(pairs),
            "offset_ms": round(offset, 1),
            "residual_median_ms": round(statistics.median(residuals), 1)
            if residuals else None,
            "residual_p90_ms": round(
                sorted(residuals)[min(len(residuals) - 1, int(len(residuals) * 0.9))]
                , 1,
            ) if residuals else None,
        }
    return {
        "left": str(left),
        "right": str(right),
        "left_actions": total_left,
        "right_actions": total_right,
        "matched": matched,
        "unmatched_left": total_left - matched,
        "unmatched_right": total_right - matched,
        "match_rate_left": round(matched / total_left, 3) if total_left else None,
        "match_rate_right": round(matched / total_right, 3) if total_right else None,
        "global_offset_ms": round(statistics.median(all_deltas), 1)
        if all_deltas else None,
        "residual_median_ms": round(statistics.median(all_residuals), 1)
        if all_residuals else None,
        "residual_p90_ms": round(
            sorted(all_residuals)[
                min(len(all_residuals) - 1, int(len(all_residuals) * 0.9))
            ],
            1,
        ) if all_residuals else None,
        "per_lane": per_lane,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path, help="left trace.jsonl")
    parser.add_argument("right", type=Path, help="right trace.jsonl")
    args = parser.parse_args()
    result = compare_traces(args.left, args.right)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
