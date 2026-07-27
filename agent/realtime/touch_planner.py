from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .note_detector import NoteDetector, NoteKind, ObservedNote
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
        post_release_rescue_seconds: float = 0.4,
        hold_start_suppress_seconds: float = 0.35,
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
        self.hold_start_suppress_seconds = float(hold_start_suppress_seconds)
        self._last_lane_sweep = float("-inf")
        self._previous: dict[tuple[NoteKind, int], ObservedNote] = {}
        self._last_trigger: dict[int, float] = {}
        self._hold_seen: dict[int, float] = {}
        self._active_hold_tail: dict[int, float] = {}
        self._hold_release_at: dict[int, float] = {}
        self._hold_started: dict[int, float] = {}
        self._hold_released_at: dict[int, float] = {}
        self._hold_chord_partner: dict[int, int] = {}
        self._hold_confirmed: set[int] = set()
        self._hold_tail_flick: set[int] = set()
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
        # The suppression window is lane-keyed; for a slide hold the contact
        # id is the stale START lane. Poisoning it kills real notes crossing
        # there within 0.4 s of the release. Record only where the finger
        # actually lifted.
        self._hold_released_at[final_lane] = now
        self._active_hold_lane.pop(contact, None)
        self._active_hold_x.pop(contact, None)
        self._hold_last_moved_at.pop(contact, None)
        self._hold_tail_flick.discard(contact)

    def _release_hold(
        self,
        contact: int,
        lane: int,
        now: float,
        reason: str,
        actions: list[TouchAction],
    ) -> None:
        """Release a hold, releasing its chord partner in the same frame.

        Double long notes share one white connector: both contacts must lift
        together. When the partner's tail is also within the rescue margin of
        the release line, holding it any longer only drags one half of the
        chord past its judgement.
        """
        # A hold whose tail carried the pink chevron marker must be swiped,
        # not lifted: the dispatcher converts the held contact into a flick.
        if contact in self._hold_tail_flick:
            actions.append(TouchAction(ActionKind.FLICK, lane, now, contact, reason))
        else:
            actions.append(TouchAction(ActionKind.UP, lane, now, contact, reason))
        self._finish_hold(contact, now, reason)
        # The UP action reports the hold's current observed lane, which can
        # differ from the last recorded active lane after a slide. Record the
        # release under the action lane too, or the post-release suppression
        # window will miss residue appearing where the finger actually lifted.
        self._hold_released_at[lane] = now
        partner = self._hold_chord_partner.pop(contact, None)
        if partner is None or partner not in self._active_hold_tail:
            return
        partner_tail = self._active_hold_tail[partner]
        if partner_tail < self.hold_release_y - self.paired_hold_rescue_margin:
            return
        partner_lane = self._active_hold_lane.get(partner, partner)
        if partner in self._hold_tail_flick:
            actions.append(TouchAction(
                ActionKind.FLICK, partner_lane, now, partner, f"{reason}-paired"
            ))
        else:
            actions.append(TouchAction(
                ActionKind.UP, partner_lane, now, partner, f"{reason}-paired"
            ))
        self._finish_hold(partner, now, f"{reason}-paired")
        self._hold_chord_partner.pop(partner, None)

    @staticmethod
    def _touch_x(note: ObservedNote) -> int:
        return max(120, min(1160, round(note.x)))

    @staticmethod
    def _lane_center_x(lane: int, y: float) -> float:
        progress = min(1.08, max(0.0, (y - NoteDetector.VANISHING_Y) / (
            NoteDetector.JUDGEMENT_Y - NoteDetector.VANISHING_Y
        )))
        return 640 + (NoteDetector.DEFAULT_LANE_CENTERS[lane] - 640) * progress

    def _hugs_hold_edge(self, note: ObservedNote, hold_lane: int) -> bool:
        """True when the note sits on the lane edge facing the active hold.

        A hold body bleeds edge pixels into the neighbouring lane, and those
        fragments hug the edge toward the hold. A real note on the adjacent
        lane sits near its own centre, so only edge-hugging tracks count as
        artifacts.
        """
        center = self._lane_center_x(note.lane, note.y)
        spacing = max(
            24.0,
            abs(self._lane_center_x(1, note.y) - self._lane_center_x(0, note.y)),
        )
        toward = 1.0 if hold_lane > note.lane else -1.0
        return (note.x - center) * toward > spacing * .2

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
            if continuing and contact not in self._active_hold_tail:
                # Already released as a chord partner earlier in this frame.
                continue
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

            if note.hold_tail_flick:
                # Latch the marker: one clean sighting is enough, and the
                # release must stay a swipe even if later frames lose it.
                self._hold_tail_flick.add(contact)

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
                    self._release_hold(contact, note.lane, now, "tail-ring", actions)
                    continue
                if (
                    hold_age >= .30
                    and previous_tail < self.hold_release_y <= tail
                ):
                    self._release_hold(
                        contact, note.lane, now, "tail-crossing", actions
                    )
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
                if (
                    now - self._last_trigger.get(note.lane, float("-inf"))
                    < self.hold_start_suppress_seconds
                ):
                    # A perfect-hit flash is a tall green beam at the line
                    # appearing right after a tap on the same lane; it can
                    # linger and drift up for half a second, so this window
                    # does not depend on the previous sample. A real hold
                    # head was visible falling first and starts via
                    # should_cross, which stays untouched.
                    should_rescue = False
                    should_pair_rescue = False
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
                        and previous.hold_body_confidence >= .8
                        and note.hold_body_confidence >= .8
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
                    # Holds whose heads arrive together share one connector;
                    # link them so the release side can lift both contacts in
                    # the same frame later.
                    for other_contact, other_started in self._hold_started.items():
                        if (
                            other_contact != contact
                            and other_contact in self._active_hold_tail
                            and now - other_started <= 0.08
                        ):
                            self._hold_chord_partner[contact] = other_contact
                            self._hold_chord_partner[other_contact] = contact
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
            adjacent_hold_lane = next(
                (
                    lane for lane in self._active_hold_lane.values()
                    if abs(note.lane - lane) == 1
                ),
                None,
            )
            trusted_adjacent_track = (
                tracked.minimum_y <= self.judgement_y - 40
                and tracked.motion_samples >= 3
                and tracked.downward_motion_frames >= 2
                and tracked.velocity_y > 0
            )
            if (
                adjacent_hold_lane is not None
                and note.y >= self.judgement_y - 20
                and self._hugs_hold_edge(note, adjacent_hold_lane)
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
            # One physical pink flick often segments into a FLICK track plus
            # trailing TAP/SKILL fragments on the same lane. Never let a
            # plainer fragment judge the note first: the game's flick
            # judgement covers the press, the reverse downgrades it.
            flick_shadowed = (
                note.kind != NoteKind.FLICK
                and any(
                    other.note.kind == NoteKind.FLICK
                    and other.note.lane == note.lane
                    and not other.fired
                    and abs(other.note.y - note.y) <= 60
                    # A flick-hit afterimage rises or fades in place and can
                    # pass every shape gate. It must not shadow real notes:
                    # only a track with proven downward motion is a threat.
                    and other.downward_motion_frames >= 1
                    and other.velocity_y > 100
                    for other in tracked_notes
                )
            )
            if flick_shadowed:
                self._note_tracker.mark_fired(tracked.track_id, now)
                continue
            # Bright skill heads and briefly occluded notes can stay outside
            # the colour ranges until they reach the judgement line, so their
            # first tracked sample already sits at the line with no usable
            # velocity. Without a guarded first-visible rescue these notes
            # silently miss. The post-release window is what keeps the hold
            # tail-ring residue from retriggering: it stays suppressive.
            crossed_rescue_line = (
                tracked.previous_y is not None
                and tracked.previous_y < self.judgement_y - 5 <= note.y
            )
            if (
                self.rescue_first_visible
                and (tracked.previous_y is None or crossed_rescue_line)
                and note.y >= self.judgement_y - 5
            ):
                if (
                    tracked.previous_y is None
                    and note.y >= self.judgement_y + 8
                ):
                    # Tap-effect ripples and hold-tail residue park a flat
                    # fragment just below the line (around judgement + 9)
                    # with no prior motion. A real head is always first seen
                    # at the line itself, so a first sample this low is
                    # never a note: swallow it before it becomes a phantom
                    # tap on an idle lane.
                    self._note_tracker.mark_fired(tracked.track_id, now)
                    self._record_diagnostic(
                        "below_line_residue_suppressed",
                        now,
                        lane=note.lane,
                        y=round(note.y, 2),
                        width=note.width,
                        height=note.height,
                    )
                    continue
                release_age = now - self._hold_released_at.get(
                    note.lane, float("-inf")
                )
                if release_age < self.post_release_rescue_seconds:
                    target = self.judgement_y - tracked.velocity_y * self.timing_offset
                    is_real_crossing = (
                        tracked.previous_y is not None
                        and tracked.velocity_y > 0
                        # The immediate previous sample sits only ~12 px above
                        # the line at 60 fps, so previous_y can never prove a
                        # fall. The track's own minimum proves it instead.
                        and tracked.minimum_y <= self.judgement_y - 25
                        and tracked.previous_y < target <= note.y
                    )
                    self._note_tracker.mark_fired(tracked.track_id, now)
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
                self._note_tracker.mark_fired(tracked.track_id, now)
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
                self._release_hold(contact, lane, now, "predicted-tail", actions)
        for contact, started in list(self._hold_started.items()):
            if (
                contact in self._active_hold_tail
                and now - started >= self.hold_max_seconds
            ):
                lane = self._active_hold_lane.get(contact, contact)
                self._release_hold(contact, lane, now, "hold-failsafe", actions)
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
        """Fire validated chord partners together; merge only fragments.

        Notes joined by the white connector must be judged at the same
        instant, so same-frame neighbours each backed by a validated track
        (or a detector-validated rescue) all fire. A single physical note
        rebuilt as a second fragmented track in the adjacent lane has no
        such evidence and is still merged into one press.
        """
        structural = [
            action for action in actions
            if action.kind in (ActionKind.UP, ActionKind.DOWN, ActionKind.MOVE)
            # A hold release delivered as a FLICK (tail-flick conversion)
            # still owns a live contact. Treating it as a judgement
            # transient can drop it, and the dispatcher then keeps the
            # finger down forever: the next hold started on that contact
            # crashes with "touch contact N is already active".
            or action.contact is not None
        ]
        current_down_lanes = {
            action.lane for action in structural if action.kind == ActionKind.DOWN
        }

        transients = [
            action for action in actions
            if action.kind in (ActionKind.TAP, ActionKind.FLICK)
            and action.contact is None
            and action.reason != "lane-sweep"
        ]
        lane_sweeps = [action for action in actions if action.reason == "lane-sweep"]
        # An upward flick begins with a press and can satisfy an adjacent tap;
        # keep it before plain taps when both occupy one judgement window.
        transients.sort(key=lambda action: (
            0 if action.kind == ActionKind.FLICK else 1, action.lane
        ))

        def trusted(action: TouchAction) -> bool:
            if action.reason == "rescue":
                return True
            track = tracked_by_id.get(action.track_id)
            return (
                track is not None
                and track.minimum_y <= self.judgement_y - 40
                and track.motion_samples >= 3
                and track.downward_motion_frames >= 2
                and track.velocity_y > 0
            )

        # Union same-frame neighbours, then keep every validated member of
        # each group. Groups with no validated member keep their first action,
        # preserving the old single-press behaviour for ambiguous clusters.
        parent = list(range(len(transients)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        for first in range(len(transients)):
            for second in range(first + 1, len(transients)):
                if abs(transients[first].lane - transients[second].lane) <= 1:
                    first_root, second_root = find(first), find(second)
                    if first_root != second_root:
                        parent[second_root] = first_root

        grouped: dict[int, list[TouchAction]] = {}
        for index, action in enumerate(transients):
            grouped.setdefault(find(index), []).append(action)
        survivors: list[TouchAction] = []
        for members in grouped.values():
            validated = [action for action in members if trusted(action)]
            survivors.extend(validated if validated else members[:1])

        kept: list[TouchAction] = []
        for action in survivors:
            # Same-lane retrigger: only an unvalidated fragment is swallowed;
            # validated tracks (including a real flick shadowed by its own
            # tap fragment) always fire again. A rescue never follows a
            # recent same/adjacent-lane trigger: the note was already judged.
            # Same-frame chord partners sit on different lanes by definition,
            # so a same-lane rescue is a fragment even at an identical
            # timestamp.
            recently_covered = any(
                (
                    action.lane == lane
                    and now - timestamp < self.retrigger_seconds
                    and not trusted(action)
                )
                or (
                    action.reason == "rescue"
                    and abs(action.lane - lane) <= 1
                    and now - timestamp < self.retrigger_seconds
                    and (
                        action.lane == lane
                        or 1e-9 < now - timestamp
                    )
                )
                for lane, timestamp in self._last_trigger.items()
            )
            covered_by_hold_start = (
                action.kind == ActionKind.TAP
                and not trusted(action)
                and any(abs(action.lane - lane) <= 1 for lane in current_down_lanes)
            )
            if recently_covered or covered_by_hold_start:
                continue
            kept.append(action)
            self._last_trigger[action.lane] = now
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
        self._hold_chord_partner.clear()
        self._hold_confirmed.clear()
        self._last_hold_rejection.clear()
        self._active_hold_lane.clear()
        self._active_hold_x.clear()
        self._hold_last_moved_at.clear()
        self._hold_tail_flick.clear()
        self._previous.clear()
        self._last_trigger.clear()
        self._note_tracker.reset()
        self._last_lane_sweep = float("-inf")
        return actions
