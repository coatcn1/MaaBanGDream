from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from .note_detector import NoteDetector, NoteKind, ObservedNote


@dataclass
class _Track:
    track_id: int
    kind: NoteKind
    lane: int
    samples: list[ObservedNote] = field(default_factory=list)
    last_seen: float = 0.0
    fired: bool = False
    first_y: float = 0.0
    minimum_y: float = 0.0
    motion_samples: int = 1
    downward_motion_frames: int = 0
    fired_at: float | None = None


@dataclass(frozen=True)
class TrackedNote:
    track_id: int
    note: ObservedNote
    previous_y: float | None
    previous_x: float | None
    velocity_y: float
    sample_count: int
    fired: bool
    first_y: float
    minimum_y: float
    motion_samples: int
    downward_motion_frames: int
    last_fired_at: float | None
    last_seen: float


class MultiNoteTracker:
    """Track every ordinary falling note behind one small, stable interface.

    Detector fragments are clustered first.  Within each kind/lane stream an
    order-preserving assignment prevents two close notes from swapping IDs.
    Velocity is regressed over up to five unique frames rather than calculated
    from one noisy frame pair.
    """

    ORDINARY_KINDS = frozenset({NoteKind.TAP, NoteKind.SKILL, NoteKind.FLICK})

    def __init__(
        self,
        *,
        memory_seconds: float = .15,
        max_samples: int = 5,
        keep_downward_on_jitter: bool = False,
    ):
        self.memory_seconds = float(memory_seconds)
        self.max_samples = max(3, min(5, int(max_samples)))
        self.keep_downward_on_jitter = bool(keep_downward_on_jitter)
        self._tracks: dict[int, _Track] = {}
        self._current_ids: set[int] = set()
        self._next_id = 1

    @staticmethod
    def _same_component(a: ObservedNote, b: ObservedNote) -> bool:
        horizontal_limit = max(a.width, b.width) / 2 + 60
        vertical_delta = abs(a.y - b.y)
        horizontal_delta = abs(a.x - b.x)
        if horizontal_delta > horizontal_limit:
            return False
        if vertical_delta <= 4:
            return True
        # Split left/right fragments of one ring are offset horizontally.
        # Two genuinely dense same-lane heads are nearly co-centred and must
        # remain independent even when their vertical gap is only 8-20 px.
        fragment_offset = max(8.0, min(a.width, b.width) * .12)
        return vertical_delta <= 10 and horizontal_delta >= fragment_offset

    def _cluster(self, notes: list[ObservedNote]) -> list[ObservedNote]:
        remaining = list(notes)
        result: list[ObservedNote] = []
        while remaining:
            component = [remaining.pop(0)]
            changed = True
            while changed:
                changed = False
                for candidate in list(remaining):
                    if any(self._same_component(candidate, item) for item in component):
                        remaining.remove(candidate)
                        component.append(candidate)
                        changed = True
            # The largest component is the least jittery representation of a
            # segmented note head; merging bounding boxes shifts its centre.
            result.append(max(component, key=lambda item: item.width * item.height))
        return sorted(result, key=lambda item: item.y)

    @staticmethod
    def _velocity(samples: list[ObservedNote]) -> float:
        if len(samples) < 2:
            return 0.0
        mean_t = sum(item.timestamp for item in samples) / len(samples)
        mean_y = sum(item.y for item in samples) / len(samples)
        denominator = sum((item.timestamp - mean_t) ** 2 for item in samples)
        if denominator <= 1e-9:
            return 0.0
        velocity = sum(
            (item.timestamp - mean_t) * (item.y - mean_y) for item in samples
        ) / denominator
        return max(0.0, min(4000.0, velocity))

    @staticmethod
    def _lane_center_x(lane: int, y: float) -> float:
        progress = min(1.08, max(0.0, (y - NoteDetector.VANISHING_Y) / (
            NoteDetector.JUDGEMENT_Y - NoteDetector.VANISHING_Y
        )))
        return 640 + (
            NoteDetector.DEFAULT_LANE_CENTERS[lane] - 640
        ) * progress

    def _merge_cross_lane_fragments(
        self,
        notes: list[ObservedNote],
    ) -> list[ObservedNote]:
        """Merge one physical note split across an adjacent-lane boundary.

        Perspective segmentation can assign the head ring to one lane and the
        trail/glow fragment to the neighbouring lane.  Both fragments then
        become independent tracks and fire twice, which the game reads as a
        miss on the following note.  Real chord partners sit near their own
        lane centres; only fragments that hug the shared boundary are merged,
        keeping the head (lowest playable fragment).
        """
        if not self.keep_downward_on_jitter or len(notes) < 2:
            return notes
        remaining = list(notes)
        merged: list[ObservedNote] = []
        while remaining:
            note = remaining.pop(0)
            for other in list(remaining):
                if (
                    note.kind != other.kind
                    or abs(note.lane - other.lane) != 1
                ):
                    continue
                y_note = note.y + note.height / 2
                y_other = other.y + other.height / 2
                if abs(y_note - y_other) > 14:
                    continue
                center_note = self._lane_center_x(note.lane, y_note)
                center_other = self._lane_center_x(other.lane, y_other)
                spacing = max(
                    24.0,
                    abs(
                        self._lane_center_x(1, y_note)
                        - self._lane_center_x(0, y_note)
                    ),
                )
                if other.lane > note.lane:
                    toward_note = note.x - center_note
                    toward_other = center_other - other.x
                else:
                    toward_note = center_note - note.x
                    toward_other = other.x - center_other
                if (
                    toward_note <= spacing * .12
                    or toward_other <= spacing * .12
                ):
                    continue
                if (
                    abs(note.x - other.x)
                    > max(45.0, (note.width + other.width) * .6)
                ):
                    continue
                if y_other > y_note:
                    note = other
                remaining.remove(other)
                break
            merged.append(note)
        return merged

    @staticmethod
    def _compatible(track: _Track, note: ObservedNote, now: float) -> bool:
        latest = track.samples[-1]
        elapsed = max(0.0, now - track.last_seen)
        maximum_forward = 25 + elapsed * 2500
        dy = note.y - latest.y
        return -6 <= dy <= maximum_forward and abs(note.x - latest.x) <= 90

    def _assign(
        self, tracks: list[_Track], notes: list[ObservedNote], now: float
    ) -> list[tuple[_Track, ObservedNote]]:
        # Both sequences are ordered far-to-near.  Dynamic programming may
        # skip either side, but matched pairs can never cross.
        @lru_cache(maxsize=None)
        def solve(i: int, j: int) -> tuple[int, float, tuple[tuple[int, int], ...]]:
            if i == len(tracks) or j == len(notes):
                return 0, 0.0, ()
            options = [solve(i + 1, j), solve(i, j + 1)]
            if self._compatible(tracks[i], notes[j], now):
                matches, cost, pairs = solve(i + 1, j + 1)
                predicted = tracks[i].samples[-1].y + self._velocity(tracks[i].samples) * max(
                    0.0, now - tracks[i].last_seen
                )
                options.append((
                    matches + 1,
                    cost - abs(notes[j].y - predicted) - abs(notes[j].x - tracks[i].samples[-1].x) * .1,
                    ((i, j),) + pairs,
                ))
            return max(options, key=lambda value: (value[0], value[1]))

        return [(tracks[i], notes[j]) for i, j in solve(0, 0)[2]]

    def update(self, notes: list[ObservedNote], now: float) -> list[TrackedNote]:
        self._tracks = {
            key: track for key, track in self._tracks.items()
            if now - track.last_seen <= self.memory_seconds
        }
        current_ids: set[int] = set()
        grouped: dict[tuple[NoteKind, int], list[ObservedNote]] = {}
        for note in notes:
            if note.kind in self.ORDINARY_KINDS:
                grouped.setdefault((note.kind, note.lane), []).append(note)

        clustered: dict[tuple[NoteKind, int], list[ObservedNote]] = {}
        for (kind, lane), raw in grouped.items():
            clustered[(kind, lane)] = self._cluster(raw)
        if self.keep_downward_on_jitter:
            by_kind: dict[NoteKind, list[ObservedNote]] = {}
            for (kind, _lane), candidates in clustered.items():
                by_kind.setdefault(kind, []).extend(candidates)
            merged_by_kind = {
                kind: self._merge_cross_lane_fragments(candidates)
                for kind, candidates in by_kind.items()
            }
            clustered = {}
            for kind, candidates in merged_by_kind.items():
                for note in candidates:
                    clustered.setdefault((kind, note.lane), []).append(note)
            for key in clustered:
                clustered[key].sort(key=lambda item: item.y)

        for (kind, lane), candidates in clustered.items():
            tracks = sorted(
                (track for track in self._tracks.values()
                 if track.kind == kind and track.lane == lane),
                key=lambda track: track.samples[-1].y,
            )
            assignments = self._assign(tracks, candidates, now)
            assigned_notes = {id(note) for _, note in assignments}
            for track, note in assignments:
                latest = track.samples[-1]
                if abs(note.y - latest.y) >= .2 or abs(note.x - latest.x) >= .2:
                    track.samples.append(note)
                    track.samples = track.samples[-self.max_samples:]
                    track.motion_samples += 1
                    track.downward_motion_frames = (
                        track.downward_motion_frames + 1
                        if note.y > latest.y else 0
                    )
                track.minimum_y = min(track.minimum_y, note.y)
                track.last_seen = now
                current_ids.add(track.track_id)
            for note in candidates:
                if id(note) in assigned_notes:
                    continue
                track = _Track(
                    self._next_id,
                    kind,
                    lane,
                    [note],
                    last_seen=now,
                    first_y=note.y,
                    minimum_y=note.y,
                )
                self._tracks[track.track_id] = track
                current_ids.add(track.track_id)
                self._next_id += 1

        result = []
        for track_id in sorted(current_ids):
            track = self._tracks[track_id]
            result.append(TrackedNote(
                track.track_id,
                track.samples[-1],
                track.samples[-2].y if len(track.samples) >= 2 else None,
                track.samples[-2].x if len(track.samples) >= 2 else None,
                self._velocity(track.samples),
                len(track.samples),
                track.fired,
                track.first_y,
                track.minimum_y,
                track.motion_samples,
                track.downward_motion_frames,
                track.fired_at,
                track.last_seen,
            ))
        self._current_ids = current_ids
        return result

    def stale(self) -> list[TrackedNote]:
        """Return retained tracks that had no detection in the latest frame."""
        result = []
        for track_id in sorted(self._tracks.keys() - self._current_ids):
            track = self._tracks[track_id]
            result.append(TrackedNote(
                track.track_id,
                track.samples[-1],
                track.samples[-2].y if len(track.samples) >= 2 else None,
                track.samples[-2].x if len(track.samples) >= 2 else None,
                self._velocity(track.samples),
                len(track.samples),
                track.fired,
                track.first_y,
                track.minimum_y,
                track.motion_samples,
                track.downward_motion_frames,
                track.fired_at,
                track.last_seen,
            ))
        return result

    def mark_fired(self, track_id: int, now: float | None = None) -> None:
        if track_id in self._tracks:
            self._tracks[track_id].fired = True
            self._tracks[track_id].fired_at = now

    def discard(self, track_id: int) -> None:
        """Remove a proven visual artifact before it can steal a later match."""
        self._tracks.pop(track_id, None)

    def reset(self) -> None:
        self._tracks.clear()
        self._current_ids.clear()
        self._next_id = 1
