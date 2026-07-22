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
        retrigger_seconds: float = 0.12,
        hold_grace_seconds: float = 0.35,
        track_memory_seconds: float = 0.15,
        enable_slide: bool = True,
        rescue_first_visible: bool = False,
        lane_sweep_interval: float | None = None,
        # Real rehearsal frames bracketed the valid tail window: 590 released
        # The tail ring is centred near y=572 when it overlaps the visual line;
        # the body mask's far edge reaches about y=590 at the same instant.
        hold_release_y: float = 555,
        paired_hold_rescue_margin: float = 35,
        hold_max_seconds: float = 20,
        hold_restart_cooldown_seconds: float = 0.25,
        post_release_rescue_seconds: float = 0.4,
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
        self.hold_restart_cooldown_seconds = float(hold_restart_cooldown_seconds)
        self.post_release_rescue_seconds = float(post_release_rescue_seconds)
        self._last_lane_sweep = float("-inf")
        self._previous: dict[tuple[NoteKind, int], ObservedNote] = {}
        self._last_trigger: dict[int, float] = {}
        self._hold_seen: dict[int, float] = {}
        self._active_hold_tail: dict[int, float] = {}
        self._hold_release_at: dict[int, float] = {}
        self._hold_started: dict[int, float] = {}
        self._hold_released_at: dict[int, float] = {}
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
                release_age = now - self._hold_released_at.get(note.lane, float("-inf"))
                restart_allowed = (
                    release_age >= self.hold_restart_cooldown_seconds
                    and (
                        should_cross
                        or release_age >= self.post_release_rescue_seconds
                    )
                )
                if (
                    note.lane not in self._active_hold_tail
                    and restart_allowed
                    and (
                    should_rescue or should_cross or should_pair_rescue
                    )
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
                        self._hold_released_at[note.lane] = now
                    elif previous_tail < self.hold_release_y <= tail:
                        actions.append(TouchAction(
                            ActionKind.UP, note.lane, now, note.lane, "tail-crossing"
                        ))
                        self._active_hold_tail.pop(note.lane, None)
                        self._hold_seen.pop(note.lane, None)
                        self._hold_release_at.pop(note.lane, None)
                        self._hold_started.pop(note.lane, None)
                        self._hold_released_at[note.lane] = now
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
                release_age = now - self._hold_released_at.get(
                    note.lane, float("-inf")
                )
                if release_age < self.post_release_rescue_seconds:
                    target = self.judgement_y - tracked.velocity_y * self.timing_offset
                    is_real_crossing = (
                        tracked.previous_y is not None
                        and tracked.velocity_y > 0
                        and tracked.previous_y <= self.judgement_y - 25
                        and tracked.previous_y < target <= note.y
                    )
                    self._note_tracker.mark_fired(tracked.track_id)
                    if is_real_crossing:
                        kind = (
                            ActionKind.FLICK
                            if note.kind == NoteKind.FLICK
                            else ActionKind.TAP
                        )
                        actions.append(TouchAction(
                            kind, note.lane, now,
                            reason="crossing", track_id=tracked.track_id,
                        ))
                    continue
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
                self._hold_released_at[lane] = now
        for lane, started in list(self._hold_started.items()):
            if lane in self._active_hold_tail and now - started >= self.hold_max_seconds:
                actions.append(TouchAction(
                    ActionKind.UP, lane, now, lane, "hold-failsafe"
                ))
                self._active_hold_tail.pop(lane, None)
                self._hold_seen.pop(lane, None)
                self._hold_release_at.pop(lane, None)
                self._hold_started.pop(lane, None)
                self._hold_released_at[lane] = now
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
        return self._suppress_redundant_judgements(actions, now)

    def _suppress_redundant_judgements(
        self, actions: list[TouchAction], now: float
    ) -> list[TouchAction]:
        """Use the game's adjacent-lane judgement instead of repeated input."""
        structural = [
            action for action in actions
            if action.kind in (ActionKind.UP, ActionKind.DOWN, ActionKind.MOVE)
        ]
        current_down_lanes = {
            action.lane for action in structural if action.kind == ActionKind.DOWN
        }

        transients = [
            action for action in actions
            if action.kind in (ActionKind.TAP, ActionKind.FLICK)
            and action.reason != "lane-sweep"
        ]
        lane_sweeps = [action for action in actions if action.reason == "lane-sweep"]
        # An upward flick begins with a press and can satisfy an adjacent tap;
        # keep it before plain taps when both occupy one judgement window.
        transients.sort(key=lambda action: (
            0 if action.kind == ActionKind.FLICK else 1, action.lane
        ))
        kept: list[TouchAction] = []
        for action in transients:
            recently_covered = any(
                abs(action.lane - lane) <= 1
                and (
                    abs(now - timestamp) <= 1e-9
                    or (
                        action.reason == "rescue"
                        and now - timestamp < self.retrigger_seconds
                    )
                )
                for lane, timestamp in self._last_trigger.items()
            )
            covered_by_hold_start = (
                action.kind == ActionKind.TAP
                and any(abs(action.lane - lane) <= 1 for lane in current_down_lanes)
            )
            if recently_covered or covered_by_hold_start:
                continue
            kept.append(action)
            self._last_trigger[action.lane] = now
        return structural + kept + lane_sweeps

    def reset(self, now: float) -> list[TouchAction]:
        actions = []
        for lane in sorted(self._active_hold_tail):
            actions.append(TouchAction(ActionKind.UP, lane, now, lane, "engine-reset"))
        self._active_hold_tail.clear()
        self._hold_seen.clear()
        self._hold_release_at.clear()
        self._hold_started.clear()
        self._hold_released_at.clear()
        self._previous.clear()
        self._last_trigger.clear()
        self._note_tracker.reset()
        self._last_lane_sweep = float("-inf")
        return actions
