from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .note_detector import NoteKind, ObservedNote
from .note_tracker import MultiNoteTracker, TrackedNote


class ActionKind(str, Enum):
    TAP = "tap"
    DOWN = "down"
    MOVE = "move"
    UP = "up"
    FLICK = "flick"


def sliding_holds_enabled(difficulty: str) -> bool:
    return str(difficulty).strip().lower() in {"hard", "expert", "special"}


@dataclass(frozen=True)
class TouchAction:
    kind: ActionKind
    lane: int
    timestamp: float
    contact: int | None = None
    reason: str = ""
    track_id: int | None = None
    target_x: int | None = None


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
        self._last_lane_sweep = float("-inf")
        self._previous: dict[tuple[NoteKind, int], ObservedNote] = {}
        self._last_trigger: dict[int, tuple[float, NoteKind | None]] = {}
        self._hold_seen: dict[int, float] = {}
        self._active_hold_tail: dict[int, float] = {}
        self._hold_release_at: dict[int, float] = {}
        self._hold_started: dict[int, float] = {}
        self._hold_released_at: dict[int, float] = {}
        self._hold_confirmed: set[int] = set()
        self._last_hold_rejection: dict[int, float] = {}
        self._active_hold_lane: dict[int, int] = {}
        self._active_hold_x: dict[int, float] = {}
        self._hold_last_moved_at: dict[int, float] = {}
        self._diagnostics: list[dict[str, object]] = []
        self.filtered_adjacent_artifacts = 0
        self.rejected_hold_candidates = 0
        self._note_tracker = MultiNoteTracker(memory_seconds=track_memory_seconds)

    @property
    def has_active_holds(self) -> bool:
        return bool(self._active_hold_tail)

    @property
    def timing_offset_ms(self) -> int:
        return round(self.timing_offset * 1000)

    def set_timing_offset_ms(self, value: int) -> None:
        if not -250 <= int(value) <= 250:
            raise ValueError("timing offset must be between -250 and 250 ms")
        self.timing_offset = int(value) / 1000

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
            started = self._hold_started.get(lane, now)
            self._hold_release_at[lane] = max(
                now + .02,
                started + .30,
                release_at,
            )

    def _record_diagnostic(self, event: str, now: float, **fields: object) -> None:
        self._diagnostics.append({"event": event, "timestamp": now, **fields})

    def drain_diagnostics(self) -> list[dict[str, object]]:
        result = self._diagnostics
        self._diagnostics = []
        return result

    def _finish_hold(self, contact: int, now: float, reason: str) -> None:
        started = self._hold_started.get(contact, now)
        final_lane = self._active_hold_lane.get(contact, contact)
        self._record_diagnostic(
            "hold_release",
            now,
            lane=final_lane,
            release_method=reason,
            duration_ms=round(max(0.0, now - started) * 1000),
            body_confirmed=contact in self._hold_confirmed,
            contact=contact,
            final_lane=final_lane,
        )
        self._active_hold_tail.pop(contact, None)
        self._hold_seen.pop(contact, None)
        self._hold_release_at.pop(contact, None)
        self._hold_started.pop(contact, None)
        self._hold_confirmed.discard(contact)
        self._hold_released_at[contact] = now
        self._hold_released_at[final_lane] = now
        self._active_hold_lane.pop(contact, None)
        self._active_hold_x.pop(contact, None)
        self._hold_last_moved_at.pop(contact, None)

    @staticmethod
    def _touch_x(note: ObservedNote) -> int:
        return max(120, min(1160, round(note.x)))

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
        hold_notes = [note for note in notes if note.kind == NoteKind.HOLD]

        # A slanted hold crosses lane boundaries while the finger contact must
        # remain the same. Match every visible component to an existing contact
        # by continuous tail/x motion before considering new lane-local holds.
        current_holds: dict[int, tuple[ObservedNote, bool]] = {}
        used_notes: set[int] = set()
        for contact in sorted(self._active_hold_tail):
            previous_tail = self._active_hold_tail[contact]
            previous_seen = self._hold_seen.get(contact, now)
            elapsed = max(0.0, now - previous_seen)
            current_lane = self._active_hold_lane.get(contact, contact)
            hold_age = now - self._hold_started.get(contact, now)
            # The tail ring is often disconnected from the translucent body.
            # Do not require it to match the stale body-tail coordinate: doing
            # so made the planner fall through to an early predicted release.
            direct_tail_rings = [
                (index, note)
                for index, note in enumerate(hold_notes)
                if index not in used_notes
                and note.lane == current_lane
                and note.height <= 30
                and note.width >= 70
                and note.y >= 555
                and hold_age >= .30
            ]
            if direct_tail_rings:
                selected_index, selected = max(
                    direct_tail_rings, key=lambda item: item[1].y
                )
                current_holds[contact] = (selected, True)
                used_notes.add(selected_index)
                continue
            previous_x = self._active_hold_x.get(
                contact,
                190 + 150 * current_lane,
            )
            maximum_forward = 25 + elapsed * 1200
            maximum_x_delta = 120 + elapsed * 1800
            plausible = [
                (index, note)
                for index, note in enumerate(hold_notes)
                if index not in used_notes
                and previous_tail - 20 <= self._hold_tail(note)
                <= previous_tail + maximum_forward
                and (
                    note.lane == current_lane
                    or (
                        self.enable_slide
                        and abs(note.x - previous_x) <= maximum_x_delta
                    )
                )
            ]
            if not plausible:
                continue
            tail_rings = [
                (index, note) for index, note in plausible
                if note.height <= 30 and note.width >= 70 and note.y >= 570
            ]
            if tail_rings:
                selected_index, selected = max(
                    tail_rings, key=lambda item: item[1].y
                )
            else:
                selected_index, selected = min(
                    plausible,
                    key=lambda item: (
                        abs(self._hold_tail(item[1]) - previous_tail) * 2
                        + abs(item[1].x - previous_x) * .35
                    ),
                )
            current_holds[contact] = (selected, True)
            used_notes.add(selected_index)

        grouped_new: dict[int, list[tuple[int, ObservedNote]]] = {}
        for index, note in enumerate(hold_notes):
            if index not in used_notes:
                grouped_new.setdefault(note.lane, []).append((index, note))
        occupied_lanes = set(self._active_hold_lane.values())
        for lane, candidates in grouped_new.items():
            if lane in current_holds or lane in occupied_lanes:
                continue
            _, selected = max(
                candidates, key=lambda item: self._hold_head(item[1])
            )
            current_holds[lane] = (selected, False)

        selected_by_lane = {
            (NoteKind.HOLD, note.lane): note
            for note, _ in current_holds.values()
        }
        for contact, (note, continuing) in sorted(
            current_holds.items(), key=lambda item: item[1][0].lane
        ):
            key = (NoteKind.HOLD, note.lane)
            previous = self._previous.get(key)
            if (
                previous is not None
                and now - previous.timestamp > self.track_memory_seconds
            ):
                previous = None
            previous_seen = self._hold_seen.get(contact)
            self._hold_seen[contact] = now
            head = self._hold_head(note)
            tail = self._hold_tail(note)

            if continuing:
                previous_tail = self._active_hold_tail[contact]
                hold_age = now - self._hold_started.get(contact, now)
                tail_ring_at_line = (
                    note.height <= 30
                    and note.width >= 70
                    and note.y >= 555
                    and contact in self._hold_confirmed
                    and hold_age >= .30
                )
                if tail_ring_at_line:
                    actions.append(TouchAction(
                        ActionKind.UP, note.lane, now, contact, "tail-ring"
                    ))
                    self._finish_hold(contact, now, "tail-ring")
                    continue
                if (
                    hold_age >= .30
                    and previous_tail < self.hold_release_y <= tail
                ):
                    actions.append(TouchAction(
                        ActionKind.UP, note.lane, now, contact, "tail-crossing"
                    ))
                    self._finish_hold(contact, now, "tail-crossing")
                    continue
                if note.height >= 40:
                    self._predict_hold_release(
                        contact, previous_tail, previous_seen or now, tail, now
                    )
                self._active_hold_tail[contact] = tail
                target_x = self._touch_x(note)
                previous_lane = self._active_hold_lane.get(contact, note.lane)
                previous_x = self._active_hold_x.get(contact, float(target_x))
                last_moved = self._hold_last_moved_at.get(
                    contact, float("-inf")
                )
                near_judgement_line = head >= self.judgement_y - 45
                should_move = (
                    self.enable_slide
                    and near_judgement_line
                    and (
                        note.lane != previous_lane
                        or abs(target_x - previous_x) >= 18
                    )
                    and now - last_moved >= .03
                )
                if near_judgement_line:
                    self._active_hold_lane[contact] = note.lane
                    self._active_hold_x[contact] = float(target_x)
                if should_move:
                    self._hold_last_moved_at[contact] = now
                    actions.append(TouchAction(
                        ActionKind.MOVE,
                        note.lane,
                        now,
                        contact,
                        "hold-follow",
                        target_x=target_x,
                    ))
                    self._record_diagnostic(
                        "hold_move",
                        now,
                        contact=contact,
                        lane=note.lane,
                        previous_lane=previous_lane,
                        target_x=target_x,
                    )
                continue

            if note.kind == NoteKind.HOLD:
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
                    for (other_kind, other_lane), other_note
                    in selected_by_lane.items()
                )
                should_pair_rescue = (
                    contact not in self._active_hold_tail
                    and note.height >= 80
                    and head >= self.judgement_y - self.paired_hold_rescue_margin
                    and paired_due
                )
                should_cross = (
                    previous is not None
                    and self._same_falling_note(previous, note)
                    and self._hold_head(previous) < self._trigger_y(previous, note) <= head
                )
                body_confirmed = (
                    note.height >= 80
                    or (
                        should_cross
                        and previous is not None
                        and previous.height >= 30
                        and note.height >= 30
                    )
                )
                release_age = now - self._hold_released_at.get(note.lane, float("-inf"))
                strong_new_crossing = (
                    should_cross
                    and previous is not None
                    and self._hold_head(previous) <= self.judgement_y - 25
                )
                restart_allowed = (
                    note.lane not in self._hold_released_at
                    or (
                        release_age >= self.hold_restart_cooldown_seconds
                        and (
                            strong_new_crossing
                            or should_rescue
                            or should_pair_rescue
                        )
                    )
                )
                if (
                    contact not in self._active_hold_tail
                    and restart_allowed
                    and body_confirmed
                    and (
                    should_rescue
                    or should_cross
                    or should_pair_rescue
                    )
                ):
                    self._active_hold_tail[contact] = tail
                    self._hold_started[contact] = now
                    self._hold_confirmed.add(contact)
                    target_x = self._touch_x(note)
                    self._active_hold_lane[contact] = note.lane
                    self._active_hold_x[contact] = float(target_x)
                    start_reason = (
                        "rescue" if should_rescue else
                        "paired-rescue" if should_pair_rescue else
                        "crossing"
                    )
                    self._record_diagnostic(
                        "hold_start",
                        now,
                        lane=note.lane,
                        start_reason=start_reason,
                        body_confirmed=True,
                        height=round(note.height, 2),
                        contact=contact,
                        target_x=target_x,
                    )
                    self._record_diagnostic(
                        "hold_body_confirmed",
                        now,
                        lane=note.lane,
                        height=round(note.height, 2),
                    )
                    if previous is not None:
                        self._predict_hold_release(
                            contact, self._hold_tail(previous),
                            previous.timestamp, tail, now,
                        )
                    actions.append(TouchAction(
                        ActionKind.DOWN,
                        note.lane,
                        now,
                        contact,
                        start_reason,
                        target_x=target_x,
                    ))
                elif (
                    contact not in self._active_hold_tail
                    and head >= self.judgement_y - 5
                    and not body_confirmed
                ):
                    last_rejected = self._last_hold_rejection.get(
                        note.lane, float("-inf")
                    )
                    if now - last_rejected > self.track_memory_seconds:
                        self.rejected_hold_candidates += 1
                        self._last_hold_rejection[note.lane] = now
                        self._record_diagnostic(
                            "hold_candidate_rejected",
                            now,
                            lane=note.lane,
                            height=round(note.height, 2),
                            reason="unconfirmed-short-fragment",
                        )
                continue
        for tracked in sorted(tracked_notes, key=lambda item: (item.note.lane, item.note.y)):
            note = tracked.note
            if tracked.fired:
                continue
            adjacent_to_hold = any(
                abs(note.lane - lane) == 1
                for lane in self._active_hold_lane.values()
            )
            trusted_adjacent_track = (
                tracked.minimum_y <= self.judgement_y - 40
                and tracked.motion_samples >= 3
                and tracked.downward_motion_frames >= 2
                and tracked.velocity_y > 0
            )
            if (
                adjacent_to_hold
                and note.y >= self.judgement_y - 20
                and not trusted_adjacent_track
            ):
                self._note_tracker.mark_fired(tracked.track_id, now)
                self.filtered_adjacent_artifacts += 1
                self._record_diagnostic(
                    "adjacent_artifact_filtered",
                    now,
                    lane=note.lane,
                    track_id=tracked.track_id,
                    first_y=round(tracked.first_y, 2),
                    minimum_y=round(tracked.minimum_y, 2),
                    motion_samples=tracked.motion_samples,
                    downward_motion_frames=tracked.downward_motion_frames,
                )
                continue
            if tracked.previous_y is None or tracked.velocity_y <= 0:
                continue
            target = self.judgement_y - tracked.velocity_y * self.timing_offset
            if tracked.previous_y < target <= note.y:
                kind = ActionKind.FLICK if note.kind == NoteKind.FLICK else ActionKind.TAP
                actions.append(TouchAction(
                    kind, note.lane, now, reason="crossing", track_id=tracked.track_id
                ))
                self._note_tracker.mark_fired(tracked.track_id, now)
        # Losing the green mask is not immediate evidence that the tail ended.
        # Skill animations can cover a long bar for many frames, so a predicted
        # release is valid only after the body has remained absent for a full
        # grace window. Visible bodies always override stale predictions.
        for contact, release_at in list(self._hold_release_at.items()):
            unseen_for = now - self._hold_seen.get(contact, now)
            held_for = now - self._hold_started.get(contact, now)
            if (
                contact in self._active_hold_tail
                and now >= release_at
                and unseen_for >= self.hold_grace_seconds
                and held_for >= .30
            ):
                lane = self._active_hold_lane.get(contact, contact)
                actions.append(TouchAction(
                    ActionKind.UP, lane, now, contact, "predicted-tail"
                ))
                self._finish_hold(contact, now, "predicted-tail")
        for contact, started in list(self._hold_started.items()):
            if (
                contact in self._active_hold_tail
                and now - started >= self.hold_max_seconds
            ):
                lane = self._active_hold_lane.get(contact, contact)
                actions.append(TouchAction(
                    ActionKind.UP, lane, now, contact, "hold-failsafe"
                ))
                self._finish_hold(contact, now, "hold-failsafe")
        if (
            self.lane_sweep_interval is not None
            and now - self._last_lane_sweep >= self.lane_sweep_interval
        ):
            occupied = set(self._active_hold_lane.values())
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
        for key, note in selected_by_lane.items():
            previous = remembered.get(key)
            # A duplicate LDOpenGL frame carries no new motion information.
            # Preserve the last genuinely different position so the next fresh
            # frame has a useful velocity baseline.
            if previous is None or abs(note.y - previous.y) >= .2:
                remembered[key] = note
        self._previous = remembered
        return self._suppress_redundant_judgements(
            actions,
            now,
            {tracked.track_id: tracked for tracked in tracked_notes},
        )

    def _suppress_redundant_judgements(
        self,
        actions: list[TouchAction],
        now: float,
        tracked_by_id: dict[int, TrackedNote],
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
            current_track = tracked_by_id.get(action.track_id)
            current_kind = (
                current_track.note.kind if current_track is not None else None
            )
            late_born = (
                current_track is not None
                and current_track.minimum_y >= self.judgement_y - 20
            )
            recently_covered = any(
                (
                    action.lane == lane
                    and now - timestamp < self.retrigger_seconds
                    and (
                        late_born
                        or (
                            current_kind is not None
                            and previous_kind is not None
                            and current_kind != previous_kind
                        )
                    )
                )
                or (
                    abs(action.lane - lane) <= 1
                    and abs(now - timestamp) <= 1e-9
                )
                for lane, (timestamp, previous_kind) in self._last_trigger.items()
            )
            covered_by_hold_start = (
                action.kind == ActionKind.TAP
                and any(abs(action.lane - lane) <= 1 for lane in current_down_lanes)
            )
            if recently_covered or covered_by_hold_start:
                continue
            kept.append(action)
            self._last_trigger[action.lane] = (now, current_kind)
        return structural + kept + lane_sweeps

    def reset(self, now: float) -> list[TouchAction]:
        actions = []
        for contact in sorted(self._active_hold_tail):
            lane = self._active_hold_lane.get(contact, contact)
            actions.append(TouchAction(
                ActionKind.UP, lane, now, contact, "engine-reset"
            ))
        self._active_hold_tail.clear()
        self._hold_seen.clear()
        self._hold_release_at.clear()
        self._hold_started.clear()
        self._hold_released_at.clear()
        self._hold_confirmed.clear()
        self._last_hold_rejection.clear()
        self._active_hold_lane.clear()
        self._active_hold_x.clear()
        self._hold_last_moved_at.clear()
        self._previous.clear()
        self._last_trigger.clear()
        self._note_tracker.reset()
        self._last_lane_sweep = float("-inf")
        return actions
