"""Official chart timeline for chart-backed realtime play.

The current test song is fixed to SAVIOR OF SONG (BestDori song 306, Hard
chart).  The raw BestDori chart JSON lives at
``resource/charts/song-306-hard.json``.  This module converts the raw note
list into a flat, lane-indexed judgement timeline (tap / hold-head /
hold-tail) using the chart's BPM, so the realtime planner can predict
occluded notes and schedule hold releases at the exact game times.
"""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ChartJudgement:
    time_s: float
    lane: int
    kind: str
    note_index: int


class ChartTimeline:
    """Sorted, lane-indexed chart judgements."""

    LANE_COUNT = 7

    def __init__(
        self,
        judgements: list[ChartJudgement],
        *,
        bpm: float = 192.0,
    ) -> None:
        if bpm <= 0:
            raise ValueError("bpm must be positive")
        self.bpm = float(bpm)
        self.judgements = sorted(
            judgements,
            key=lambda item: (item.time_s, item.lane),
        )
        self._by_lane: list[list[ChartJudgement]] = [
            [item for item in self.judgements if item.lane == lane]
            for lane in range(self.LANE_COUNT)
        ]
        self._lane_times: list[list[float]] = [
            [item.time_s for item in items]
            for items in self._by_lane
        ]

    @classmethod
    def from_json(cls, path: str | Path) -> "ChartTimeline":
        raw = json.loads(
            Path(path).read_text(encoding="utf-8-sig")
        )
        bpm = 192.0
        judgements: list[ChartJudgement] = []
        note_index = 0
        for note in raw:
            note_type = note.get("type")
            if note_type == "BPM":
                bpm = float(note["bpm"])
                continue
            if note_type == "System":
                continue
            if note_type == "Single":
                judgements.append(ChartJudgement(
                    _beat_to_seconds(float(note["beat"]), bpm),
                    int(note["lane"]),
                    "tap",
                    note_index,
                ))
                note_index += 1
                continue
            if note_type in {"Long", "Slide"}:
                connections = note.get("connections", [])
                visible = [
                    connection for connection in connections
                    if not connection.get("hidden")
                ]
                if not visible:
                    continue
                head = min(visible, key=lambda item: float(item["beat"]))
                tail = max(visible, key=lambda item: float(item["beat"]))
                judgements.append(ChartJudgement(
                    _beat_to_seconds(float(head["beat"]), bpm),
                    int(head["lane"]),
                    "hold-head",
                    note_index,
                ))
                judgements.append(ChartJudgement(
                    _beat_to_seconds(float(tail["beat"]), bpm),
                    int(tail["lane"]),
                    "hold-tail",
                    note_index,
                ))
                note_index += 1
                continue
            raise ValueError(f"unsupported chart note type: {note_type}")
        return cls(judgements, bpm=bpm)

    def next_judgement(
        self,
        lane: int,
        after_time_s: float,
    ) -> ChartJudgement | None:
        """Return the first judgement on ``lane`` at or after ``after_time_s``."""
        if not 0 <= lane < self.LANE_COUNT:
            return None
        times = self._lane_times[lane]
        index = bisect.bisect_left(times, after_time_s)
        if index >= len(times):
            return None
        return self._by_lane[lane][index]

    def judgement_near(
        self,
        lane: int,
        time_s: float,
        *,
        window_s: float,
    ) -> ChartJudgement | None:
        """Return the nearest judgement on ``lane`` within ``window_s``."""
        if not 0 <= lane < self.LANE_COUNT:
            return None
        times = self._lane_times[lane]
        index = bisect.bisect_left(times, time_s)
        candidates: list[ChartJudgement] = []
        if index < len(times):
            candidates.append(self._by_lane[lane][index])
        if index > 0:
            candidates.append(self._by_lane[lane][index - 1])
        best = min(
            candidates,
            key=lambda item: abs(item.time_s - time_s),
        )
        if abs(best.time_s - time_s) <= window_s:
            return best
        return None

    def hold_tail_for_head(
        self,
        head: ChartJudgement,
    ) -> ChartJudgement | None:
        """Return the paired hold-tail judgement for a hold-head."""
        if head.kind != "hold-head":
            return None
        for judgement in self.judgements:
            if (
                judgement.note_index == head.note_index
                and judgement.kind == "hold-tail"
            ):
                return judgement
        return None

    def expected_hold_tail_time(
        self,
        lane: int,
        engine_time_s: float,
        song_offset_s: float,
        *,
        window_s: float = 0.35,
    ) -> float | None:
        """Tail time for a hold whose head was pressed near ``engine_time_s``."""
        song_time = engine_time_s + song_offset_s
        head = self.judgement_near(
            lane,
            song_time,
            window_s=window_s,
        )
        if head is None or head.kind != "hold-head":
            return None
        tail = self.hold_tail_for_head(head)
        return None if tail is None else tail.time_s


def _beat_to_seconds(beat: float, bpm: float) -> float:
    return beat * 60.0 / bpm
