from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .note_detector import NoteKind, ObservedNote
from .note_tracker import MultiNoteTracker


class ActionKind(str, Enum):
    TAP = "tap"
    DOWN = "down"
    MOVE = "move"
    UP = "up"
    FLICK = "flick"


@dataclass(frozen=True)
class TouchAction:
    kind: ActionKind
    lane: int
    timestamp: float
    contact: int | None = None
    reason: str = ""
    track_id: int | None = None


class RealtimePlanner:
    """Convert tracked visual notes into deduplicated touch actions."""

    def __init__(
        self,
        judgement_y: float = 565,
        timing_offset_ms: int = 10,
        retrigger_seconds: float = 0.06,
        hold_grace_seconds: float = 0.35,
        track_memory_seconds: float = 0.15,
        enable_slide: bool = True,
        rescue_first_visible: bool = False,
        lane_sweep_interval: float | None = None,
        # Real rehearsal frames bracketed the valid tail window: 590 released
        # The tail ring is centred near y=572 when it overlaps the visual line;
        # the body mask's far edge reaches about y=590 at the same instant.
        hold_release_y: float = 570,
        paired_hold_rescue_margin: float = 35,
        hold_max_seconds: float = 6,
    ):
        self.judgement_y = float(judgement_y)
        self.timing_offset = timing_offset_ms / 1000
        self.retrigger_seconds = retrigger_seconds
        self.hold_grace_seconds = hold_grace_seconds
        self.track_memory_seconds = track_memory_seconds
        self.enable_slide = enable_slide
        self.rescue_first_visible = rescue_first_visible
        self.lane_sweep_interval = lane_sweep_interval
        self.hold_release_y = float(hold_release_y)
        self.paired_hold_rescue_margin = float(paired_hold_rescue_margin)
        self.hold_max_seconds = float(hold_max_seconds)
        self._last_lane_sweep = float("-inf")
        self._previous: dict[tuple[NoteKind, int], ObservedNote] = {}
        self._last_trigger: dict[int, float] = {}
        self._hold_seen: dict[int, float] = {}
        self._active_hold_tail: dict[int, float] = {}
        self._hold_release_at: dict[int, float] = {}
        self._hold_started: dict[int, float] = {}
        self._note_tracker = MultiNoteTracker(memory_seconds=track_memory_seconds)

    def _predict_hold_release(
        self, lane: int, previous_tail: float, previous_time: float,
        tail: float, now: float,
    ) -> None:
        elapsed = now - previous_time
        if elapsed <= 0 or tail >= self.hold_release_y:
            return
        velocity = (tail - previous_tail) / elapsed
        if 80 <= velocity <= 3000:
            release_at = now + (self.hold_release_y - tail) / velocity
            self._hold_release_at[lane] = max(now + .02, release_at)

    def _trigger_y(self, previous: ObservedNote, note: ObservedNote) -> float:
        elapsed = note.timestamp - previous.timestamp
        if elapsed <= 0:
            return self.judgement_y
        velocity = max(0.0, (note.y - previous.y) / elapsed)
        return self.judgement_y - velocity * self.timing_offset

    @staticmethod
    def _same_falling_note(previous: ObservedNote, note: ObservedNote) -> bool:
        dy = note.y - previous.y
        return 0.2 <= dy <= 80 and abs(note.x - previous.x) <= 90

    @staticmethod
    def _hold_head(note: ObservedNote) -> float:
        """Near end of a falling green bar: this is what must be pressed."""
        return note.y + note.height / 2

    @staticmethod
    def _hold_tail(note: ObservedNote) -> float:
        """Far end of a falling green bar: release only after it reaches line."""
        return note.y - note.height / 2

    def update(self, notes: list[ObservedNote], now: float) -> list[TouchAction]:
        actions: list[TouchAction] = []
        tracked_notes = self._note_tracker.update(notes, now)
        grouped: dict[tuple[NoteKind, int], list[ObservedNote]] = {}
        for note in notes:
            if note.kind != NoteKind.HOLD:
                continue
            grouped.setdefault((note.kind, note.lane), []).append(note)
        current: dict[tuple[NoteKind, int], ObservedNote] = {}
        for key, candidates in grouped.items():
            kind, lane = key
            if kind == NoteKind.HOLD and lane in self._active_hold_tail:
                # The translucent bar body is the only component that carries
                # the real tail. Bright head rings and judgement particles near
                # the line are short components and must never release a hold.
                previous_tail = self._active_hold_tail[lane]
                elapsed = max(0.0, now - self._hold_seen.get(lane, now))
                maximum_forward = 25 + elapsed * 900
                plausible = [
                    note for note in candidates
                    if previous_tail - 15 <= self._hold_tail(note)
                    <= previous_tail + maximum_forward
                ]
                if not plausible:
                    continue
                tail_rings = [
                    note for note in plausible
                    if note.height <= 30 and note.width >= 70 and note.y >= 570
                ]
                # Near the judgement line the mask often splits into a large
                # translucent body plus a thin bright tail ring. The ring is
                # the authoritative release marker even when the body is a
                # numerically closer continuation of the previous component.
                current[key] = (
                    max(tail_rings, key=lambda note: note.y)
                    if tail_rings
                    else min(
                        plausible,
                        key=lambda note: abs(self._hold_tail(note) - previous_tail),
                    )
                )
            else:
                current[key] = max(candidates, key=lambda note: note.y + note.height / 2)
        for key, note in sorted(current.items(), key=lambda item: item[0][1]):
            previous = self._previous.get(key)
            if note.kind == NoteKind.HOLD:
                previous_seen = self._hold_seen.get(note.lane)
                self._hold_seen[note.lane] = now
                head = self._hold_head(note)
                tail = self._hold_tail(note)
                should_rescue = (
                    self.rescue_first_visible
                    and note.height >= 80
                    and head >= self.judgement_y - 5
                    and (
                        previous is None
                        or self._hold_head(previous) < self.judgement_y - 5
                    )
                )
                # Perspective/alpha segmentation can make one side of a
                # simultaneous long-note pair look a frame shorter. If its
                # partner is already due, press both instead of losing the
                # second contact (most visible on lanes 2 and 6).
                paired_due = any(
                    other_kind == NoteKind.HOLD
                    and other_lane != note.lane
                    and other_note.height >= 80
                    and self._hold_head(other_note) >= self.judgement_y - 5
                    and abs(self._hold_head(other_note) - head)
                    <= self.paired_hold_rescue_margin
                    for (other_kind, other_lane), other_note in current.items()
                )
                should_pair_rescue = (
                    note.lane not in self._active_hold_tail
                    and note.height >= 80
                    and head >= self.judgement_y - self.paired_hold_rescue_margin
                    and paired_due
                )
                should_cross = (
                    previous is not None
                    and self._same_falling_note(previous, note)
                    and self._hold_head(previous) < self._trigger_y(previous, note) <= head
                )
                if note.lane not in self._active_hold_tail and (
                    should_rescue or should_cross or should_pair_rescue
                ):
                    self._active_hold_tail[note.lane] = tail
                    self._hold_started[note.lane] = now
                    if previous is not None:
                        self._predict_hold_release(
                            note.lane, self._hold_tail(previous),
                            previous.timestamp, tail, now,
                        )
                    actions.append(TouchAction(
                        ActionKind.DOWN, note.lane, now, note.lane,
                        (
                            "rescue" if should_rescue else
                            "paired-rescue" if should_pair_rescue else "crossing"
                        ),
                    ))
                elif note.lane in self._active_hold_tail:
                    previous_tail = self._active_hold_tail[note.lane]
                    tail_ring_at_line = (
                        note.height <= 30
                        and note.width >= 70
                        and note.y >= 555
                    )
                    if tail_ring_at_line:
                        actions.append(TouchAction(
                            ActionKind.UP, note.lane, now, note.lane, "tail-ring"
                        ))
                        self._active_hold_tail.pop(note.lane, None)
                        self._hold_seen.pop(note.lane, None)
                        self._hold_release_at.pop(note.lane, None)
                        self._hold_started.pop(note.lane, None)
                    elif previous_tail < self.hold_release_y <= tail:
                        actions.append(TouchAction(
                            ActionKind.UP, note.lane, now, note.lane, "tail-crossing"
                        ))
                        self._active_hold_tail.pop(note.lane, None)
                        self._hold_seen.pop(note.lane, None)
                        self._hold_release_at.pop(note.lane, None)
                        self._hold_started.pop(note.lane, None)
                    else:
                        if note.height >= 40:
                            self._predict_hold_release(
                                note.lane, previous_tail,
                                previous_seen or now, tail, now,
                            )
                        self._active_hold_tail[note.lane] = tail
                continue
        for tracked in sorted(tracked_notes, key=lambda item: (item.note.lane, item.note.y)):
            note = tracked.note
            if tracked.fired:
                continue
            crossed_rescue_line = (
                tracked.previous_y is not None
                and tracked.previous_y < self.judgement_y - 5 <= note.y
            )
            if (
                self.rescue_first_visible
                and (tracked.previous_y is None or crossed_rescue_line)
                and note.y >= self.judgement_y - 5
            ):
                kind = ActionKind.FLICK if note.kind == NoteKind.FLICK else ActionKind.TAP
                actions.append(TouchAction(
                    kind, note.lane, now, reason="rescue", track_id=tracked.track_id
                ))
                self._note_tracker.mark_fired(tracked.track_id)
                continue
            if tracked.previous_y is None or tracked.velocity_y <= 0:
                continue
            target = self.judgement_y - tracked.velocity_y * self.timing_offset
            if tracked.previous_y < target <= note.y:
                kind = ActionKind.FLICK if note.kind == NoteKind.FLICK else ActionKind.TAP
                actions.append(TouchAction(
                    kind, note.lane, now, reason="crossing", track_id=tracked.track_id
                ))
                self._note_tracker.mark_fired(tracked.track_id)
        # Losing the green mask is not evidence that the tail has ended. Skill
        # animations and judgement text can cover a long bar for many frames.
        # Release only on an observed tail crossing, or via reset/failsafe when
        # the realtime engine itself exits.
        for lane, release_at in list(self._hold_release_at.items()):
            if lane in self._active_hold_tail and now >= release_at:
                actions.append(TouchAction(
                    ActionKind.UP, lane, now, lane, "predicted-tail"
                ))
                self._active_hold_tail.pop(lane, None)
                self._hold_seen.pop(lane, None)
                self._hold_release_at.pop(lane, None)
                self._hold_started.pop(lane, None)
        for lane, started in list(self._hold_started.items()):
            if lane in self._active_hold_tail and now - started >= self.hold_max_seconds:
                actions.append(TouchAction(
                    ActionKind.UP, lane, now, lane, "hold-failsafe"
                ))
                self._active_hold_tail.pop(lane, None)
                self._hold_seen.pop(lane, None)
                self._hold_release_at.pop(lane, None)
                self._hold_started.pop(lane, None)
        # A white connector can pair a long-note tail with a normal/flick
        # head. The tail ring is often detected one frame before the partner's
        # motion crossing. Pull near-line partners into the same action batch;
        # MaaTouch commits partner DOWN and hold UP atomically.
        if any(action.kind == ActionKind.UP for action in actions):
            already_touched = {action.lane for action in actions}
            for tracked in tracked_notes:
                kind, lane, note = tracked.note.kind, tracked.note.lane, tracked.note
                if tracked.fired or lane in already_touched:
                    continue
                if note.y < self.judgement_y - 45:
                    continue
                action_kind = ActionKind.FLICK if kind == NoteKind.FLICK else ActionKind.TAP
                actions.append(TouchAction(
                    action_kind, lane, now, reason="linked-tail",
                    track_id=tracked.track_id,
                ))
                self._note_tracker.mark_fired(tracked.track_id)
                already_touched.add(lane)
        if (
            self.lane_sweep_interval is not None
            and now - self._last_lane_sweep >= self.lane_sweep_interval
        ):
            occupied = set(self._active_hold_tail)
            already_touched = {action.lane for action in actions}
            for lane in range(7):
                if lane not in occupied and lane not in already_touched:
                    actions.append(TouchAction(
                        ActionKind.FLICK, lane, now, reason="lane-sweep"
                    ))
            self._last_lane_sweep = now
        remembered = {
            key: previous for key, previous in self._previous.items()
            if now - previous.timestamp <= self.track_memory_seconds
        }
        for key, note in current.items():
            previous = remembered.get(key)
            # A duplicate LDOpenGL frame carries no new motion information.
            # Preserve the last genuinely different position so the next fresh
            # frame has a useful velocity baseline.
            if previous is None or abs(note.y - previous.y) >= .2:
                remembered[key] = note
        self._previous = remembered
        return actions

    def reset(self, now: float) -> list[TouchAction]:
        actions = []
        for lane in sorted(self._active_hold_tail):
            actions.append(TouchAction(ActionKind.UP, lane, now, lane, "engine-reset"))
        self._active_hold_tail.clear()
        self._hold_seen.clear()
        self._hold_release_at.clear()
        self._hold_started.clear()
        self._previous.clear()
        self._last_trigger.clear()
        self._note_tracker.reset()
        self._last_lane_sweep = float("-inf")
        return actions
