"""Align a realtime trace to an official BestDori chart and report misses.

The realtime recorder starts when the engine starts, which can be a variable
number of seconds before/after the song's beat grid begins.  This tool maps
each dispatched TAP/FLICK action to the nearest chart judgement (head/tail
per lane) and reports:

- estimated engine-to-song offset (positive = actions happen later than the
  chart beat grid);
- matched judgements, missed judgements and spurious actions;
- per-lane and per-type statistics;
- the full list of missed judgements for later detector/planner analysis.

Usage:
    python scripts/align_trace_to_chart.py --chart debug/chart-306-hard.json \
        --trace debug/recordings/<run>/trace.jsonl [--json debug/out.json]
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

import numpy as np


MATCH_WINDOW_MS = 250.0


def beat_to_seconds(beat: float, bpm: float) -> float:
    return beat * 60.0 / bpm


def load_chart_judgements(path: Path) -> list[dict[str, object]]:
    """Return chart judgements as (time_s, lane, judgement_type)."""
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    judgements: list[dict[str, object]] = []
    for note in raw:
        note_type = note.get("type")
        if note_type == "BPM":
            continue
        if note_type == "System":
            continue
        if note_type == "Single":
            judgements.append({
                "time_s": beat_to_seconds(float(note["beat"]), 192.0),
                "lane": int(note["lane"]),
                "type": "tap",
            })
            continue
        if note_type in {"Long", "Slide"}:
            connections = note.get("connections", [])
            if not connections:
                continue
            visible = [
                connection
                for connection in connections
                if not connection.get("hidden")
            ]
            head_connection = visible[0]
            tail_connection = max(
                visible,
                key=lambda connection: float(connection["beat"]),
            )
            head_beat = float(head_connection["beat"])
            tail_beat = float(tail_connection["beat"])
            head_lane = int(head_connection["lane"])
            tail_lane = int(tail_connection["lane"])
            judgements.append({
                "time_s": beat_to_seconds(head_beat, 192.0),
                "lane": head_lane,
                "type": "hold-head",
            })
            judgements.append({
                "time_s": beat_to_seconds(tail_beat, 192.0),
                "lane": tail_lane,
                "type": "hold-tail",
            })
            continue
        raise ValueError(f"unhandled chart note type: {note_type}")
    judgements.sort(key=lambda item: (float(item["time_s"]), int(item["lane"])))
    return judgements


def load_trace_actions(trace_path: Path) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    with trace_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            for action in row.get("actions", []):
                if action.get("kind") not in {"tap", "flick", "down", "up"}:
                    continue
                actions.append({
                    "time_ms": float(row["elapsed_ms"]),
                    "lane": int(action["lane"]),
                    "kind": action["kind"],
                    "reason": action.get("reason", ""),
                    "contact": action.get("contact"),
                })
    actions.sort(key=lambda item: (float(item["time_ms"]), int(item["lane"])))
    return actions


def estimate_offset(
    actions: list[dict[str, object]],
    judgements: list[dict[str, object]],
    *,
    coarse_range_s: float = 6.0,
) -> float:
    """Estimate engine-elapsed to chart-time offset in seconds.

    The recorder starts a variable number of seconds before/after the song's
    beat grid, so a fixed window cannot find the offset.  Cross-correlating
    the action-time histogram with the judgement-time histogram (both lanes
    pooled) is robust to per-lane insertions/deletions from missed notes.
    """
    action_times = [float(action["time_ms"]) / 1000.0 for action in actions]
    judge_times = [float(judgement["time_s"]) for judgement in judgements]
    if not action_times or not judge_times:
        return 0.0
    bin_width = 0.04
    max_time = max(max(action_times), max(judge_times)) + 1.0
    bins = int(np.ceil(max_time / bin_width)) + 1
    action_hist = np.zeros(bins, dtype=np.float64)
    judge_hist = np.zeros(bins, dtype=np.float64)
    for time_s in action_times:
        action_hist[min(bins - 1, int(time_s / bin_width))] += 1.0
    for time_s in judge_times:
        judge_hist[min(bins - 1, int(time_s / bin_width))] += 1.0
    # Smooth both histograms so BPM jitter between engine frames and chart
    # beats does not destroy the correlation peak.
    kernel = np.ones(5, dtype=np.float64) / 5.0
    action_hist = np.convolve(action_hist, kernel, mode="same")
    judge_hist = np.convolve(judge_hist, kernel, mode="same")
    shift_bins = int(coarse_range_s / bin_width)
    best_shift = 0
    best_score = -1.0
    for shift in range(-shift_bins, shift_bins + 1):
        if shift >= 0:
            overlap = judge_hist[shift:]
            reference = action_hist[:bins - shift]
        else:
            overlap = judge_hist[:bins + shift]
            reference = action_hist[-shift:]
        if overlap.size == 0 or reference.size == 0:
            continue
        score = float(np.dot(overlap, reference))
        if score > best_score:
            best_score = score
            best_shift = shift
    # Refine around the winning bin with a quarter-bin step.
    best_offset = best_shift * bin_width
    candidates = [
        best_offset + delta
        for delta in (-0.02, -0.01, 0.0, 0.01, 0.02)
    ]
    return float(max(candidates, key=lambda offset: _alignment_score(
        action_times, judge_times, offset)))


def _alignment_score(
    action_times: list[float],
    judge_times: list[float],
    offset_s: float,
) -> int:
    """Count nearest-neighbour pairs within 120 ms for a candidate offset."""
    total = 0
    for action_time in action_times:
        shifted = action_time + offset_s
        nearest = min(
            judge_times,
            key=lambda judge_time: abs(judge_time - shifted),
        )
        if abs(nearest - shifted) <= 0.12:
            total += 1
    return total


def match_actions_to_judgements(
    actions: list[dict[str, object]],
    judgements: list[dict[str, object]],
    *,
    offset_s: float,
) -> dict[str, object]:
    """Greedy per-lane matching of actions (shifted by offset) to judgements."""
    # Hold heads are pressed with DOWN, hold tails are released with UP (or a
    # contact-carrying FLICK for tail-flick slides).  Ordinary notes are
    # judged with contact-free TAP/FLICK.
    action_kind_for = {
        "tap": {"tap", "flick"},
        "hold-head": {"down"},
        "hold-tail": {"up", "flick"},
    }

    def action_matches_judgement(
        action: dict[str, object],
        judgement_type: str,
    ) -> bool:
        if action["kind"] in action_kind_for[judgement_type]:
            if judgement_type == "hold-tail" and action["kind"] == "flick":
                return action.get("contact") is not None
            if judgement_type == "tap":
                return action.get("contact") is None
            return True
        return False

    matched: list[tuple[dict[str, object], dict[str, object], float]] = []
    missed: list[dict[str, object]] = []
    spurious: list[dict[str, object]] = []
    used_action_ids: set[int] = set()
    used_judgement_ids: set[int] = set()
    reported_spurious_ids: set[int] = set()
    for judgement_type in ("tap", "hold-head", "hold-tail"):
        judge_by_lane: dict[int, list[tuple[int, dict[str, object]]]] = defaultdict(list)
        for index, judgement in enumerate(judgements):
            if judgement["type"] != judgement_type:
                continue
            judge_by_lane[int(judgement["lane"])].append((index, judgement))
        action_by_lane: dict[int, list[tuple[int, dict[str, object]]]] = defaultdict(list)
        for index, action in enumerate(actions):
            if not action_matches_judgement(action, judgement_type):
                continue
            if index in used_action_ids:
                continue
            action_by_lane[int(action["lane"])].append((index, action))

        for lane in sorted(set(judge_by_lane) | set(action_by_lane)):
            judge_entries = judge_by_lane.get(lane, [])
            action_entries = action_by_lane.get(lane, [])
            i = j = 0
            while i < len(action_entries) and j < len(judge_entries):
                action_index, action = action_entries[i]
                judge_index, judgement = judge_entries[j]
                action_time = float(action["time_ms"]) / 1000.0 + offset_s
                judge_time = float(judgement["time_s"])
                delta = judge_time - action_time
                if delta > MATCH_WINDOW_MS / 1000.0:
                    # The action fires too early for this judgement: it is a
                    # spurious press on this lane/type.
                    spurious.append({**action, "judgement_type": judgement_type})
                    reported_spurious_ids.add(action_index)
                    i += 1
                elif delta < -MATCH_WINDOW_MS / 1000.0:
                    # The judgement predates the action: the note was missed.
                    missed.append(judgement)
                    j += 1
                else:
                    matched.append((action, judgement, abs(delta) * 1000.0))
                    used_action_ids.add(action_index)
                    used_judgement_ids.add(judge_index)
                    i += 1
                    j += 1
            for action_index, action in action_entries[i:]:
                if action_index not in used_action_ids:
                    spurious.append({**action, "judgement_type": judgement_type})
                    reported_spurious_ids.add(action_index)
            for judge_index, judgement in judge_entries[j:]:
                if judge_index not in used_judgement_ids:
                    missed.append(judgement)

    all_actions = {
        index: action for index, action in enumerate(actions)
    }
    # Actions that matched no judgement type are still reported once.
    for index, action in all_actions.items():
        if index in used_action_ids or index in reported_spurious_ids:
            continue
        spurious.append({**action, "judgement_type": "unmatched"})

    deduplicated_spurious: dict[str, dict[str, object]] = {}
    for item in spurious:
        key = (
            f"{item['time_ms']}-{item['lane']}-{item['kind']}-"
            f"{item['reason']}-{item['judgement_type']}"
        )
        deduplicated_spurious.setdefault(key, item)
    spurious = list(deduplicated_spurious.values())

    # The per-type loop above never removed a used judgement from its lane
    # lists, so re-scan to drop anything double counted as missed.
    missed = [
        judgement
        for index, judgement in enumerate(judgements)
        if index not in used_judgement_ids
    ]
    matched_deltas_ms = [delta for _, _, delta in matched]
    return {
        "judgements_total": len(judgements),
        "actions_total": len(actions),
        "matched": len(matched),
        "missed": len(missed),
        "spurious": len(spurious),
        "match_delta_median_ms": round(statistics.median(matched_deltas_ms), 1)
        if matched_deltas_ms else None,
        "match_delta_p90_ms": round(
            sorted(matched_deltas_ms)[
                min(len(matched_deltas_ms) - 1, int(len(matched_deltas_ms) * 0.9))
            ],
            1,
        ) if matched_deltas_ms else None,
        "missed_judgements": [
            {
                "time_s": round(float(item["time_s"]), 3),
                "lane": int(item["lane"]),
                "type": item["type"],
            }
            for item in missed
        ],
        "spurious_actions": [
            {
                "time_ms": round(float(item["time_ms"]), 1),
                "lane": int(item["lane"]),
                "kind": item["kind"],
                "reason": item.get("reason", ""),
                "judgement_type": item.get("judgement_type", ""),
            }
            for item in spurious
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chart", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--json", type=Path, help="write full report here")
    args = parser.parse_args()

    judgements = load_chart_judgements(args.chart)
    actions = load_trace_actions(args.trace)
    offset_s = estimate_offset(actions, judgements)
    report = match_actions_to_judgements(
        actions, judgements, offset_s=offset_s,
    )
    report["song_offset_s"] = round(offset_s, 3)
    report["chart"] = str(args.chart)
    report["trace"] = str(args.trace)
    if args.json:
        args.json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"report written to {args.json}")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
