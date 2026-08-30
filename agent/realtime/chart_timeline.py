"""Validated Bestdori chart timing for local, chart-backed realtime play.

The runtime never downloads charts. ``ChartTimeline`` accepts either the
legacy Bestdori note-list JSON or the repository's schema-v1 wrapper and
converts beats through a piecewise BPM map. Complete long/slide paths are
retained so a blind chart-backed hold can follow every connection instead of
linearly guessing from head to tail.
"""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class TempoChange:
    beat: float
    bpm: float
    time_s: float


@dataclass(frozen=True, slots=True)
class ChartPathPoint:
    beat: float
    time_s: float
    lane: float
    hidden: bool = False
    flick: bool = False
    direction: str | None = None


@dataclass(frozen=True, slots=True)
class ChartHoldPath:
    note_index: int
    note_type: str
    points: tuple[ChartPathPoint, ...]

    @property
    def head(self) -> ChartPathPoint:
        return self.points[0]

    @property
    def tail(self) -> ChartPathPoint:
        return self.points[-1]


@dataclass(frozen=True, slots=True)
class ChartJudgement:
    time_s: float
    lane: int
    kind: str
    note_index: int
    flick: bool = False
    direction: str | None = None
    tail_flick: bool = False


class TempoMap:
    """Piecewise-constant BPM conversion from absolute beat to seconds."""

    def __init__(self, changes: list[TempoChange]) -> None:
        if not changes:
            raise ValueError("chart must contain at least one BPM event")
        self.changes = tuple(changes)
        self._beats = [change.beat for change in changes]

    @classmethod
    def from_events(cls, events: list[dict[str, Any]]) -> "TempoMap":
        by_beat: dict[float, float] = {}
        for event in events:
            beat = _finite_float(event.get("beat"), "BPM beat")
            bpm = _finite_float(event.get("bpm"), "BPM value")
            if bpm <= 0:
                raise ValueError("BPM must be positive")
            by_beat[beat] = bpm
        if not by_beat:
            raise ValueError("chart must contain at least one BPM event")
        ordered = sorted(by_beat.items())
        if ordered[0][0] > 0:
            ordered.insert(0, (0.0, ordered[0][1]))

        changes: list[TempoChange] = []
        time_s = 0.0
        previous_beat, previous_bpm = ordered[0]
        changes.append(TempoChange(previous_beat, previous_bpm, time_s))
        for beat, bpm in ordered[1:]:
            time_s += (beat - previous_beat) * 60.0 / previous_bpm
            changes.append(TempoChange(beat, bpm, time_s))
            previous_beat, previous_bpm = beat, bpm
        return cls(changes)

    def seconds_at(self, beat: float) -> float:
        value = _finite_float(beat, "note beat")
        index = bisect.bisect_right(self._beats, value) - 1
        change = self.changes[max(0, index)]
        return change.time_s + (value - change.beat) * 60.0 / change.bpm


class ChartTimeline:
    """Sorted, lane-indexed judgements plus complete hold/slide paths."""

    LANE_COUNT = 7

    def __init__(
        self,
        judgements: list[ChartJudgement],
        *,
        tempo_map: TempoMap | None = None,
        bpm: float | None = None,
        hold_paths: list[ChartHoldPath] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if tempo_map is None:
            fallback_bpm = 192.0 if bpm is None else float(bpm)
            tempo_map = TempoMap.from_events([
                {"type": "BPM", "beat": 0, "bpm": fallback_bpm}
            ])
        self.tempo_map = tempo_map
        self.tempo_changes = tempo_map.changes
        self.bpm = float(tempo_map.changes[0].bpm)
        self.metadata = dict(metadata or {})
        self.judgements = sorted(
            judgements,
            key=lambda item: (item.time_s, item.lane, item.note_index),
        )
        self.start_time_s = (
            self.judgements[0].time_s if self.judgements else 0.0
        )
        self.end_time_s = (
            self.judgements[-1].time_s if self.judgements else 0.0
        )
        if hold_paths is None:
            hold_paths = _paths_from_judgements(judgements, self.bpm)
        self.hold_paths = tuple(hold_paths)
        self._hold_paths_by_note = {
            path.note_index: path for path in self.hold_paths
        }
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
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        raw, metadata = _unwrap_chart(payload)
        bpm_events = [item for item in raw if item.get("type") == "BPM"]
        tempo_map = TempoMap.from_events(bpm_events)
        judgements: list[ChartJudgement] = []
        hold_paths: list[ChartHoldPath] = []
        note_index = 0

        for note in raw:
            if not isinstance(note, dict):
                raise ValueError("chart entries must be JSON objects")
            note_type = note.get("type")
            if note_type in {"BPM", "System"}:
                continue
            if note_type == "Single":
                lane = _lane(note.get("lane"))
                flick = bool(note.get("flick", False))
                judgements.append(ChartJudgement(
                    tempo_map.seconds_at(note.get("beat")),
                    lane,
                    "tap",
                    note_index,
                    flick=flick,
                    direction=(
                        None if note.get("direction") is None
                        else str(note.get("direction"))
                    ),
                ))
                note_index += 1
                continue
            if note_type == "Directional":
                direction = str(note.get("direction", "")).strip()
                if direction not in {"Left", "Right"}:
                    raise ValueError(
                        f"unsupported directional flick: {direction!r}"
                    )
                judgements.append(ChartJudgement(
                    tempo_map.seconds_at(note.get("beat")),
                    _lane(note.get("lane")),
                    "tap",
                    note_index,
                    flick=True,
                    direction=direction,
                ))
                note_index += 1
                continue
            if note_type in {"Long", "Slide"}:
                path = _parse_hold_path(
                    note,
                    note_index=note_index,
                    tempo_map=tempo_map,
                )
                if path is None:
                    continue
                if len(path.points) == 1:
                    # Bestdori repairs a one-point Long/Slide into an
                    # ordinary judgement.  Treating it as a zero-duration
                    # hold would issue DOWN+UP in one frame and can leave a
                    # backend contact out of sync.
                    point = path.points[0]
                    judgements.append(ChartJudgement(
                        point.time_s,
                        _lane(point.lane),
                        "tap",
                        note_index,
                        flick=point.flick,
                        direction=point.direction,
                    ))
                    note_index += 1
                    continue
                hold_paths.append(path)
                tail_flick = path.tail.flick
                judgements.append(ChartJudgement(
                    path.head.time_s,
                    _lane(path.head.lane),
                    "hold-head",
                    note_index,
                    tail_flick=tail_flick,
                ))
                judgements.append(ChartJudgement(
                    path.tail.time_s,
                    _lane(path.tail.lane),
                    "hold-tail",
                    note_index,
                    flick=tail_flick,
                    direction=path.tail.direction,
                    tail_flick=tail_flick,
                ))
                note_index += 1
                continue
            raise ValueError(f"unsupported chart note type: {note_type}")
        return cls(
            judgements,
            tempo_map=tempo_map,
            hold_paths=hold_paths,
            metadata=metadata,
        )

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
        if not times:
            return None
        index = bisect.bisect_left(times, time_s)
        candidates: list[ChartJudgement] = []
        if index < len(times):
            candidates.append(self._by_lane[lane][index])
        if index > 0:
            candidates.append(self._by_lane[lane][index - 1])
        if not candidates:
            return None
        best = min(candidates, key=lambda item: abs(item.time_s - time_s))
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

    def hold_path_for_head(
        self,
        head: ChartJudgement,
    ) -> ChartHoldPath | None:
        if head.kind != "hold-head":
            return None
        return self._hold_paths_by_note.get(head.note_index)

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
        head = self.judgement_near(lane, song_time, window_s=window_s)
        if head is None or head.kind != "hold-head":
            return None
        tail = self.hold_tail_for_head(head)
        return None if tail is None else tail.time_s


def _unwrap_chart(payload: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if isinstance(payload, list):
        return payload, {"schema_version": 0}
    if not isinstance(payload, dict):
        raise ValueError("chart JSON must be a list or schema-v1 object")
    if payload.get("schema_version") != 1:
        raise ValueError(f"unsupported chart schema: {payload.get('schema_version')!r}")
    raw = payload.get("chart")
    if not isinstance(raw, list):
        raise ValueError("schema-v1 chart field must be a list")
    metadata = {
        key: payload.get(key)
        for key in ("schema_version", "source", "song", "difficulty")
    }
    return raw, metadata


def _paths_from_judgements(
    judgements: list[ChartJudgement],
    bpm: float,
) -> list[ChartHoldPath]:
    """Compatibility paths for tests/clients constructing flat timelines."""
    by_note: dict[int, dict[str, ChartJudgement]] = {}
    for judgement in judgements:
        if judgement.kind in {"hold-head", "hold-tail"}:
            by_note.setdefault(judgement.note_index, {})[judgement.kind] = judgement
    result: list[ChartHoldPath] = []
    for note_index, pair in by_note.items():
        head = pair.get("hold-head")
        tail = pair.get("hold-tail")
        if head is None or tail is None:
            continue
        result.append(ChartHoldPath(
            note_index=note_index,
            note_type="synthetic",
            points=(
                ChartPathPoint(
                    beat=head.time_s * bpm / 60.0,
                    time_s=head.time_s,
                    lane=head.lane,
                ),
                ChartPathPoint(
                    beat=tail.time_s * bpm / 60.0,
                    time_s=tail.time_s,
                    lane=tail.lane,
                    flick=tail.tail_flick,
                ),
            ),
        ))
    return result


def _parse_hold_path(
    note: dict[str, Any],
    *,
    note_index: int,
    tempo_map: TempoMap,
) -> ChartHoldPath | None:
    raw_connections = note.get("connections")
    if not isinstance(raw_connections, list):
        raise ValueError("Long/Slide connections must be a list")
    ordered = sorted(
        raw_connections,
        key=lambda item: _finite_float(item.get("beat"), "connection beat"),
    )
    visible_indexes = [
        index for index, item in enumerate(ordered)
        if not bool(item.get("hidden", False))
    ]
    if not visible_indexes:
        return None
    # Match Bestdori's own chart repair: discard only hidden prefix/suffix
    # points while preserving every interior connection for path geometry.
    usable = ordered[visible_indexes[0]:visible_indexes[-1] + 1]
    points = tuple(
        ChartPathPoint(
            beat=_finite_float(item.get("beat"), "connection beat"),
            time_s=tempo_map.seconds_at(item.get("beat")),
            lane=_path_lane(
                item.get("lane"),
                hidden=bool(item.get("hidden", False)),
            ),
            hidden=bool(item.get("hidden", False)),
            flick=bool(item.get("flick", False) or item.get("direction")),
            direction=(
                None if item.get("direction") is None
                else str(item.get("direction"))
            ),
        )
        for item in usable
    )
    return ChartHoldPath(
        note_index=note_index,
        note_type=str(note.get("type")),
        points=points,
    )


def _finite_float(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if result != result or result in {float("inf"), float("-inf")}:
        raise ValueError(f"{label} must be finite")
    return result


def _lane(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("lane must be an integer from 0 to 6")
    try:
        lane = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("lane must be an integer from 0 to 6") from exc
    if isinstance(value, float) and lane != value:
        raise ValueError("lane must be an integer from 0 to 6")
    if not 0 <= lane < ChartTimeline.LANE_COUNT:
        raise ValueError("lane must be an integer from 0 to 6")
    return lane


def _path_lane(value: Any, *, hidden: bool) -> float:
    lane = _finite_float(value, "connection lane")
    if hidden:
        if not -0.5 <= lane <= 6.5:
            raise ValueError("hidden connection lane must be within -0.5..6.5")
        return lane
    return float(_lane(lane))
