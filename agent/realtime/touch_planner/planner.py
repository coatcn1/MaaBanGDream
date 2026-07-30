from __future__ import annotations

from .actions import ActionKind, TouchAction
from .state import PlannerConfig, PlannerState
from .geometry import lane_center_x, touch_x, trusted_crossing_track
from .holds import HoldPipeline
from ..note_detector import NoteKind, ObservedNote
from ..note_tracker import MultiNoteTracker, TrackedNote


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
        # TAP EFFECT 1 can leave a first-visible line fragment for roughly
        # half a second after a hold/slide releases.  The 0.4 s window used
        # to expire just before that fragment appeared in SAVIOR OF SONG
        # Hard (about 0.47-0.50 s), so it was rescued as a TAP and the real
        # following slide head was then swallowed by same-lane deduplication.
        # Tracked physical crossings remain eligible inside this window; only
        # untracked residue is suppressed.
        post_release_rescue_seconds: float = 0.65,
        hold_start_suppress_seconds: float = 0.35,
    ):
        self._config = PlannerConfig(
            judgement_y=float(judgement_y),
            retrigger_seconds=retrigger_seconds,
            hold_grace_seconds=hold_grace_seconds,
            track_memory_seconds=track_memory_seconds,
            enable_slide=enable_slide,
            rescue_first_visible=rescue_first_visible,
            lane_sweep_interval=lane_sweep_interval,
            hold_release_y=float(hold_release_y),
            paired_hold_rescue_margin=float(paired_hold_rescue_margin),
            hold_max_seconds=float(hold_max_seconds),
            hold_restart_cooldown_seconds=hold_restart_cooldown_seconds,
            post_release_rescue_seconds=post_release_rescue_seconds,
            hold_start_suppress_seconds=hold_start_suppress_seconds,
        )
        self._state = PlannerState(timing_offset_ms=timing_offset_ms)
        self._note_tracker = MultiNoteTracker(
            memory_seconds=track_memory_seconds
        )
        self._holds = HoldPipeline(self._config, self._state)

    @property
    def has_active_holds(self) -> bool:
        return bool(self._state._active_hold_tail)

    @property
    def timing_offset_ms(self) -> int:
        return round(self._state.timing_offset * 1000)

    def set_timing_offset_ms(self, value: int) -> None:
        if not -250 <= int(value) <= 250:
            raise ValueError("timing offset must be between -250 and 250 ms")
        self._state.timing_offset = int(value) / 1000

    def _record_diagnostic(self, event: str, now: float, **fields: object) -> None:
        self._state._diagnostics.append({"event": event, "timestamp": now, **fields})

    def drain_diagnostics(self) -> list[dict[str, object]]:
        result = self._state._diagnostics
        self._state._diagnostics = []
        return result

    def _hugs_hold_edge(self, note: ObservedNote, hold_lane: int) -> bool:
        """True when the note sits on the lane edge facing the active hold.

        A hold body bleeds edge pixels into the neighbouring lane, and those
        fragments hug the edge toward the hold. A real note on the adjacent
        lane sits near its own centre, so only edge-hugging tracks count as
        artifacts.
        """
        center = lane_center_x(note.lane, note.y)
        spacing = max(
            24.0,
            abs(lane_center_x(1, note.y) - lane_center_x(0, note.y)),
        )
        toward = 1.0 if hold_lane > note.lane else -1.0
        return (note.x - center) * toward > spacing * .2

    def _matching_flick_arrow(
        self,
        fragment: TrackedNote,
        tracked_notes: list[TrackedNote],
    ) -> TrackedNote | None:
        """Match a late ring fragment to its proven falling flick arrow.

        The skin draws a flick as magenta chevrons above a wide playable ring.
        Near the judgement line colour segmentation often reports those as a
        FLICK track plus a separate TAP track. Their separation grows with
        perspective, so a fixed pixel gate both misses real pairs and absorbs
        genuinely dense same-lane notes.

        A wide arrow-to-ring gap is distinctive even when the ring inherited a
        stale TAP track id. For close 8-20 px pairs, only fragments first seen
        in the feedback band are eligible; a real dense following note has a
        long falling history of its own. The upper bound scales with lane
        spacing instead of using one fixed pixel width.
        """
        note = fragment.note
        if note.kind == NoteKind.FLICK:
            return None
        lane_spacing = max(
            24.0,
            abs(
                lane_center_x(1, note.y)
                - lane_center_x(0, note.y)
            ),
        )
        maximum_gap = min(90.0, lane_spacing * .56)
        matches = []
        for candidate in tracked_notes:
            arrow = candidate.note
            if (
                arrow.kind != NoteKind.FLICK
                or candidate.fired
                or arrow.lane != note.lane
                or not 0 <= note.timestamp - arrow.timestamp <= .05
                or candidate.downward_motion_frames < 1
                or candidate.velocity_y <= 100
            ):
                continue
            gap = note.y - arrow.y
            late_fragment = fragment.minimum_y >= self._config.judgement_y - 40
            maximum_x_delta = max(
                55.0,
                (note.width + arrow.width) * .55,
            )
            if (
                0 <= gap <= maximum_gap
                and (gap >= 24 or late_fragment)
                and abs(note.x - arrow.x) <= maximum_x_delta
            ):
                matches.append((gap, abs(note.x - arrow.x), candidate))
        if not matches:
            return None
        return min(matches, key=lambda item: (item[0], item[1]))[2]

    def _ordinary_trigger_y(self, velocity_y: float) -> float:
        """Keep enough lead time for capture-to-touch dispatch latency.

        The stored profile offset predates tracked-note triggering and can be
        negative. Applying it literally made ordinary notes fire around y=573
        in the 2026-07-29 trace; 8/9 readable judgements were SLOW. Forcing
        y=560 produced 12 FAST / 5 SLOW on the stable baseline that completed
        with 6 MISS. Detector-clean experiments place the FAST/SLOW transition
        within a few pixels. A full y=562 result still reported 17 FAST /
        7 SLOW, so y=563 is the next one-pixel correction.

        Positive calibrated offsets may still request an earlier trigger, but
        a negative offset must never push the action below this bounded line.
        """
        velocity = max(0.0, velocity_y)
        calibrated = self._config.judgement_y - velocity * self._state.timing_offset
        # Capture cadence varies sharply on the emulator (16 ms in the
        # successful run, 31 ms in the life-depleted run). Predict most of one
        # frame ahead so a head cannot jump from above the trigger to below
        # the judgement line before the next screenshot is dispatched.
        predictive_lead = min(
            .025,
            max(.006, self._state._frame_interval_seconds * .65),
        )
        predicted = self._config.judgement_y - velocity * predictive_lead
        return min(self._config.judgement_y - 3.0, calibrated, predicted)

    def _crossed_ordinary_trigger(
        self,
        tracked: TrackedNote,
        target: float,
    ) -> bool:
        """Accept the predictive crossing or the immutable physical line.

        The predictive target moves upward when capture cadence suddenly
        slows. Without the physical-line fallback, a head can sit below the
        new target on the previous frame and then cross y=565 while never
        satisfying ``previous < current_target <= current``.
        """
        previous_y = tracked.previous_y
        if previous_y is None:
            return False
        return (
            previous_y < target <= tracked.note.y
            or previous_y < self._config.judgement_y <= tracked.note.y
        )

    def _reclassified_crossing(
        self,
        note: ObservedNote,
        *,
        now: float,
    ) -> ObservedNote | None:
        """Recover a head whose colour class changes at the line.

        TAP/SKILL heads can lose their cyan/yellow centre on the last frame
        and leave only a magenta FLICK-shaped arc.  The kind-keyed tracker then
        creates a new track below the line, while the old track never crosses.
        Require a consecutive-frame, spatially continuous crossing so parked
        judgement/hold residue cannot use this fallback.
        """
        target = self._ordinary_trigger_y(0.0)
        candidates = []
        for previous in self._state._recent_ordinary.get(note.lane, []):
            elapsed = now - previous.timestamp
            if (
                previous.kind != note.kind
                and 0 < elapsed <= .06
                and target - 45 <= previous.y < target <= note.y
                and note.y <= self._config.judgement_y + 25
                and abs(note.x - previous.x)
                <= max(70.0, (note.width + previous.width) * .65)
            ):
                velocity = (note.y - previous.y) / elapsed
                if 80 <= velocity <= 3000:
                    candidates.append(previous)
        if not candidates:
            return None
        return min(candidates, key=lambda item: abs(note.x - item.x))

    def _has_upstream_same_lane_head(
        self,
        fragment: TrackedNote,
        tracked_notes: list[TrackedNote],
    ) -> TrackedNote | None:
        """Detect a line glow that appeared ahead of the real falling head.

        Hit effects can briefly expose a flat cyan fragment at y~=573 while
        the next real note on that lane is still 30-120 px above it.  Firing
        the first-visible rescue at that fragment both creates an extra press
        and swallows the real head in the retrigger window.  A genuinely dense
        same-lane pair remains eligible because its heads are much closer and
        independently tracked.
        """
        note = fragment.note
        late_near_line_fragment = (
            fragment.minimum_y >= self._config.judgement_y - 50
        )
        tracker_swap_at_line = (
            fragment.previous_y is not None
            # Two genuine dense heads can both move about 50 px per frame.
            # The recorded false crossings jumped 91-94 px in one frame as
            # the tracker was reassigned from the head to the line effect.
            and note.y - fragment.previous_y >= 70
        )
        if (
            fragment.previous_y is not None
            and not late_near_line_fragment
            and not tracker_swap_at_line
        ):
            return None
        candidates = [
            other for other in tracked_notes
            if (
                other.track_id != fragment.track_id
                and not other.fired
                and other.note.kind in MultiNoteTracker.ORDINARY_KINDS
                and other.note.lane == note.lane
                and 30 <= note.y - other.note.y <= 120
                and (
                    (
                        late_near_line_fragment
                        and other.velocity_y >= 350
                        and other.motion_samples >= 3
                        and other.downward_motion_frames >= 2
                        and other.velocity_y
                        >= max(350.0, fragment.velocity_y * 1.5)
                    )
                    or (
                        tracker_swap_at_line
                        and fragment.previous_y is not None
                        and abs(other.note.y - fragment.previous_y) <= 25
                        and other.velocity_y >= 250
                        and other.motion_samples >= 2
                        and other.downward_motion_frames >= 1
                    )
                )
            )
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.note.y)

    def update(self, notes: list[ObservedNote], now: float) -> list[TouchAction]:
        actions: list[TouchAction] = []
        if self._state._last_update_at is not None:
            interval = now - self._state._last_update_at
            if .005 <= interval <= .100:
                self._state._frame_interval_seconds = (
                    self._state._frame_interval_seconds * .75
                    + interval * .25
                )
        self._state._last_update_at = now
        tracked_notes = self._note_tracker.update(notes, now)
        selected_by_lane = self._holds.process_frame(notes, now, actions)
        promoted_flick_tracks: set[int] = set()
        for tracked in sorted(tracked_notes, key=lambda item: (item.note.lane, item.note.y)):
            note = tracked.note
            if tracked.fired:
                continue
            flick_arrow = self._matching_flick_arrow(tracked, tracked_notes)
            adjacent_hold_lane = next(
                (
                    lane for lane in self._state._active_hold_lane.values()
                    if abs(note.lane - lane) == 1
                ),
                None,
            )
            trusted_adjacent_track = (
                tracked.minimum_y <= self._config.judgement_y - 40
                and tracked.motion_samples >= 3
                and tracked.downward_motion_frames >= 2
                and tracked.velocity_y > 0
            )
            if (
                adjacent_hold_lane is not None
                and note.y >= self._config.judgement_y - 20
                and self._hugs_hold_edge(note, adjacent_hold_lane)
                and not trusted_adjacent_track
                and flick_arrow is None
            ):
                self._note_tracker.mark_fired(tracked.track_id, now)
                self._state.filtered_adjacent_artifacts += 1
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
            if flick_arrow is not None:
                if flick_arrow.track_id in promoted_flick_tracks:
                    self._note_tracker.mark_fired(tracked.track_id, now)
                    continue
                rescue_due = (
                    self._config.rescue_first_visible
                    and tracked.previous_y is None
                    and self._config.judgement_y - 5 <= note.y < self._config.judgement_y + 8
                )
                target = self._ordinary_trigger_y(tracked.velocity_y)
                crossing_due = (
                    tracked.previous_y is not None
                    and tracked.velocity_y > 0
                    and self._crossed_ordinary_trigger(tracked, target)
                )
                if rescue_due or crossing_due:
                    reason = "rescue" if rescue_due else "crossing"
                    actions.append(TouchAction(
                        ActionKind.FLICK,
                        note.lane,
                        now,
                        reason=reason,
                        track_id=flick_arrow.track_id,
                    ))
                    self._note_tracker.mark_fired(flick_arrow.track_id, now)
                    self._note_tracker.mark_fired(tracked.track_id, now)
                    promoted_flick_tracks.add(flick_arrow.track_id)
                    self._record_diagnostic(
                        "flick_ring_promoted",
                        now,
                        lane=note.lane,
                        arrow_track_id=flick_arrow.track_id,
                        ring_track_id=tracked.track_id,
                        vertical_gap=round(note.y - flick_arrow.note.y, 2),
                        maximum_gap=round(
                            abs(
                                lane_center_x(1, note.y)
                                - lane_center_x(0, note.y)
                            ) * .56,
                            2,
                        ),
                    )
                continue
            # Bright skill heads and briefly occluded notes can stay outside
            # the colour ranges until they reach the judgement line, so their
            # first tracked sample already sits at the line with no usable
            # velocity. Without a guarded first-visible rescue these notes
            # silently miss. The post-release window is what keeps the hold
            # tail-ring residue from retriggering: it stays suppressive.
            release_age = now - self._state._hold_released_at.get(
                note.lane, float("-inf")
            )
            target = self._ordinary_trigger_y(tracked.velocity_y)
            fragment_due = (
                (
                    tracked.previous_y is None
                    and note.y >= self._config.judgement_y - 5
                )
                or (
                    tracked.previous_y is not None
                    and tracked.velocity_y > 0
                    and self._crossed_ordinary_trigger(tracked, target)
                )
            )
            upstream = (
                self._has_upstream_same_lane_head(tracked, tracked_notes)
                if fragment_due else None
            )
            if upstream is not None:
                self._note_tracker.discard(tracked.track_id)
                remaining = (
                    self._ordinary_trigger_y(upstream.velocity_y)
                    - upstream.note.y
                )
                delay = max(
                    .02,
                    min(
                        .18,
                        remaining / max(100.0, upstream.velocity_y),
                    ),
                )
                self._state._pending_ordinary_rescue[note.lane] = (
                    now + delay,
                    upstream.note.kind,
                    upstream.track_id,
                )
                self._record_diagnostic(
                    "upstream_head_protected",
                    now,
                    lane=note.lane,
                    track_id=tracked.track_id,
                    upstream_track_id=upstream.track_id,
                    predicted_delay_ms=round(delay * 1000),
                    upstream_y=round(upstream.note.y, 2),
                    upstream_velocity=round(upstream.velocity_y, 2),
                    upstream_motion_samples=upstream.motion_samples,
                    upstream_downward_frames=(
                        upstream.downward_motion_frames
                    ),
                    y=round(note.y, 2),
                )
                continue
            reclassified_previous = (
                self._reclassified_crossing(note, now=now)
                if tracked.previous_y is None else None
            )
            if (
                reclassified_previous is not None
                and release_age >= self._config.post_release_rescue_seconds
            ):
                kind = (
                    ActionKind.FLICK
                    if note.kind == NoteKind.FLICK
                    else ActionKind.TAP
                )
                actions.append(TouchAction(
                    kind,
                    note.lane,
                    now,
                    reason="reclassified-crossing",
                    track_id=tracked.track_id,
                ))
                self._note_tracker.mark_fired(tracked.track_id, now)
                self._record_diagnostic(
                    "reclassified_crossing_rescued",
                    now,
                    lane=note.lane,
                    previous_kind=reclassified_previous.kind.value,
                    current_kind=note.kind.value,
                    previous_y=round(reclassified_previous.y, 2),
                    y=round(note.y, 2),
                )
                continue
            if (
                release_age < self._config.post_release_rescue_seconds
                and note.y >= self._config.judgement_y - 5
            ):
                target = self._ordinary_trigger_y(tracked.velocity_y)
                is_real_crossing = (
                    trusted_crossing_track(tracked, self._config.judgement_y)
                    and (
                        tracked.downward_motion_frames >= 2
                        or tracked.previous_y >= self._config.judgement_y - 50
                    )
                    # The immediate previous sample sits only ~12 px above
                    # the line at 60 fps, so the track minimum proves the
                    # longer fall rather than the one latest sample.
                    and self._crossed_ordinary_trigger(tracked, target)
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
            if (
                self._config.rescue_first_visible
                and tracked.previous_y is None
                and note.y >= self._config.judgement_y - 5
            ):
                if (
                    tracked.previous_y is None
                    and note.y >= self._config.judgement_y + 8
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
                kind = ActionKind.FLICK if note.kind == NoteKind.FLICK else ActionKind.TAP
                actions.append(TouchAction(
                    kind, note.lane, now, reason="rescue", track_id=tracked.track_id
                ))
                self._note_tracker.mark_fired(tracked.track_id, now)
                continue
            if tracked.previous_y is None or tracked.velocity_y <= 0:
                continue
            target = self._ordinary_trigger_y(tracked.velocity_y)
            if self._crossed_ordinary_trigger(tracked, target):
                trusted_crossing = trusted_crossing_track(tracked, self._config.judgement_y)
                if not trusted_crossing:
                    # A PERFECT glyph or lane-light fragment can disappear,
                    # then be assigned to a different fragment at the line.
                    # Two far-apart samples create a fake high velocity.  A
                    # real crossing either has a sample near the line or at
                    # least three consistently descending observations.
                    self._note_tracker.mark_fired(tracked.track_id, now)
                    self._record_diagnostic(
                        "untrusted_crossing_suppressed",
                        now,
                        lane=note.lane,
                        track_id=tracked.track_id,
                        previous_y=round(tracked.previous_y, 2),
                        y=round(note.y, 2),
                        motion_samples=tracked.motion_samples,
                        downward_motion_frames=tracked.downward_motion_frames,
                    )
                    continue
                kind = ActionKind.FLICK if note.kind == NoteKind.FLICK else ActionKind.TAP
                actions.append(TouchAction(
                    kind, note.lane, now, reason="crossing", track_id=tracked.track_id
                ))
                self._note_tracker.mark_fired(tracked.track_id, now)
        stale_tracked_notes = self._note_tracker.stale()
        dropout_by_lane: dict[int, list[tuple[float, TrackedNote]]] = {}
        for tracked in stale_tracked_notes:
            note = tracked.note
            if (
                tracked.fired
                or note.kind not in MultiNoteTracker.ORDINARY_KINDS
                or tracked.minimum_y > self._config.judgement_y - 60
                or tracked.motion_samples < 4
                or tracked.downward_motion_frames < 3
                or not 200 <= tracked.velocity_y <= 2500
                or not self._config.judgement_y - 60 <= note.y < self._config.judgement_y
                or not 0 < now - tracked.last_seen <= .12
                or any(
                    not current.fired
                    and current.note.lane == note.lane
                    and abs(current.note.y - note.y) <= 120
                    and abs(current.note.x - note.x) <= 120
                    for current in tracked_notes
                )
            ):
                continue
            target = self._ordinary_trigger_y(tracked.velocity_y)
            remaining = max(0.0, target - note.y)
            predicted_at = (
                tracked.last_seen + remaining / tracked.velocity_y
            )
            if predicted_at <= now <= predicted_at + .075:
                dropout_by_lane.setdefault(note.lane, []).append(
                    (predicted_at, tracked)
                )
        for lane, candidates in dropout_by_lane.items():
            predicted_at, tracked = max(
                candidates,
                key=lambda item: (
                    item[1].note.y,
                    item[1].motion_samples,
                    -item[1].minimum_y,
                ),
            )
            kind = (
                ActionKind.FLICK
                if tracked.note.kind == NoteKind.FLICK
                else ActionKind.TAP
            )
            actions.append(TouchAction(
                kind,
                lane,
                now,
                reason="predicted-dropout-rescue",
                track_id=tracked.track_id,
            ))
            self._note_tracker.mark_fired(tracked.track_id, now)
            self._record_diagnostic(
                "predicted_dropout_rescued",
                now,
                lane=lane,
                track_id=tracked.track_id,
                kind=tracked.note.kind.value,
                last_y=round(tracked.note.y, 2),
                last_seen_age_ms=round((now - tracked.last_seen) * 1000),
                predicted_lateness_ms=round((now - predicted_at) * 1000),
                velocity=round(tracked.velocity_y, 2),
                motion_samples=tracked.motion_samples,
                downward_frames=tracked.downward_motion_frames,
            )
        transient_lanes = {
            action.lane for action in actions
            if action.kind in {ActionKind.TAP, ActionKind.FLICK}
        }
        for lane in transient_lanes:
            self._state._pending_ordinary_rescue.pop(lane, None)
        occupied_hold_lanes = set(self._state._active_hold_lane.values())
        for lane, (due_at, kind, track_id) in list(
            self._state._pending_ordinary_rescue.items()
        ):
            if now < due_at:
                continue
            self._state._pending_ordinary_rescue.pop(lane, None)
            if lane in occupied_hold_lanes:
                continue
            action_kind = (
                ActionKind.FLICK
                if kind == NoteKind.FLICK else ActionKind.TAP
            )
            actions.append(TouchAction(
                action_kind,
                lane,
                now,
                reason="predicted-crossing-rescue",
                track_id=track_id,
            ))
            self._note_tracker.mark_fired(track_id, now)
            self._record_diagnostic(
                "predicted_crossing_rescued",
                now,
                lane=lane,
                track_id=track_id,
            )
        self._holds.finish_frame(selected_by_lane, now, actions)
        if (
            self._config.lane_sweep_interval is not None
            and now - self._state._last_lane_sweep >= self._config.lane_sweep_interval
        ):
            occupied = set(self._state._active_hold_lane.values())
            already_touched = {action.lane for action in actions}
            for lane in range(7):
                if lane not in occupied and lane not in already_touched:
                    actions.append(TouchAction(
                        ActionKind.FLICK, lane, now, reason="lane-sweep"
                    ))
            self._state._last_lane_sweep = now
        recent_ordinary: dict[int, list[ObservedNote]] = {}
        for note in notes:
            if note.kind in MultiNoteTracker.ORDINARY_KINDS:
                recent_ordinary.setdefault(note.lane, []).append(note)
        self._state._recent_ordinary = recent_ordinary
        return self._suppress_redundant_judgements(
            actions,
            now,
            {
                tracked.track_id: tracked
                for tracked in tracked_notes + stale_tracked_notes
            },
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
            if action.reason == "predicted-dropout-rescue":
                return False
            track = tracked_by_id.get(action.track_id)
            return (
                track is not None
                and track.minimum_y <= self._config.judgement_y - 40
                and track.motion_samples >= 3
                and track.downward_motion_frames >= 2
                and track.velocity_y > 0
            )

        def chord_trusted(action: TouchAction) -> bool:
            track = tracked_by_id.get(action.track_id)
            return (
                trusted(action)
                or (
                    action.reason == "crossing"
                    and track is not None
                    and trusted_crossing_track(track, self._config.judgement_y)
                )
            )

        def same_physical_fragment(
            action: TouchAction,
            lane: int,
            timestamp: float,
        ) -> bool:
            if (
                action.lane != lane
                or now - timestamp >= self._config.retrigger_seconds
            ):
                return False
            previous = self._state._last_trigger_note.get(lane)
            previous_track = self._state._last_trigger_track.get(lane)
            current_track = tracked_by_id.get(action.track_id)
            if previous is None or current_track is None:
                return False
            current = current_track.note
            horizontal_delta = abs(current.x - previous.x)
            horizontal_limit = max(
                current.width, previous.width
            ) / 2 + 60
            live_previous = (
                tracked_by_id.get(previous_track.track_id)
                if previous_track is not None else None
            )
            if (
                live_previous is not None
                and live_previous.last_seen < now - 1e-6
            ):
                live_previous = None
            exact_reidentified_head = (
                live_previous is None
                and abs(current.x - previous.x) < 1
                and abs(current.y - previous.y) < 1
                and abs(current.width - previous.width) <= 2
                and abs(current.height - previous.height) <= 2
            )
            if exact_reidentified_head:
                return True
            if (
                live_previous is None
                and abs(current.y - previous.y) <= 24
                and current.y >= self._config.judgement_y - 20
            ):
                return True
            if (
                self._state._last_trigger_reason.get(lane)
                == "predicted-dropout-rescue"
                and horizontal_delta <= horizontal_limit
                and abs(current.y - previous.y) <= 40
            ):
                return True
            previous_action_kind = self._state._last_trigger_action_kind.get(lane)
            if previous_action_kind == ActionKind.FLICK:
                # A flick's upper arrow and lower ring are often maintained
                # as two long-lived tracks. After one component dispatches
                # the gesture, the other can still be visibly upstream and
                # cross on the next frame; it is not a dense second note.
                return True
            if (
                live_previous is not None
                and live_previous.track_id != current_track.track_id
                and live_previous.note.y - current.y >= 25
            ):
                # The already-fired head is still visible farther down while
                # this independent track reaches the line: these are dense
                # successive notes, not two fragments of one head.
                return False
            fragment_offset = max(
                8.0, min(current.width, previous.width) * .12
            )
            vertical_limit = max(
                24.0, (current.height + previous.height) * .7
            )
            concentric_split = (
                previous_track is not None
                and horizontal_delta < fragment_offset
                and abs(current.y - previous.y) <= 30
                and (
                    previous_track.minimum_y >= self._config.judgement_y - 100
                    or current_track.minimum_y >= self._config.judgement_y - 100
                )
            )
            return (
                concentric_split
                or (
                    fragment_offset <= horizontal_delta <= horizontal_limit
                    and abs(current.y - previous.y) <= vertical_limit
                )
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
            validated = [action for action in members if chord_trusted(action)]
            survivors.extend(validated if validated else members[:1])

        kept: list[TouchAction] = []
        for action in survivors:
            # Same-lane retrigger: validated tracks may be genuine dense notes,
            # but horizontally offset fragments of the same head must still be
            # swallowed. A rescue never follows a recent same/adjacent-lane
            # trigger: the note was already judged.
            # Same-frame chord partners sit on different lanes by definition,
            # so a same-lane rescue is a fragment even at an identical
            # timestamp.
            recently_covered = any(
                (
                    same_physical_fragment(action, lane, timestamp)
                )
                or (
                    action.lane == lane
                    and now - timestamp < self._config.retrigger_seconds
                    and not trusted(action)
                )
                or (
                    action.reason == "rescue"
                    and abs(action.lane - lane) <= 1
                    and now - timestamp < self._config.retrigger_seconds
                    and (
                        action.lane == lane
                        or 1e-9 < now - timestamp
                    )
                )
                for lane, timestamp in self._state._last_trigger.items()
            )
            covered_by_hold_start = (
                action.kind == ActionKind.TAP
                and not trusted(action)
                and any(abs(action.lane - lane) <= 1 for lane in current_down_lanes)
            )
            if recently_covered or covered_by_hold_start:
                continue
            kept.append(action)
            self._state._last_trigger[action.lane] = now
            track = tracked_by_id.get(action.track_id)
            if track is not None:
                self._state._last_trigger_note[action.lane] = track.note
                self._state._last_trigger_track[action.lane] = track
            self._state._last_trigger_action_kind[action.lane] = action.kind
            self._state._last_trigger_reason[action.lane] = action.reason
        return structural + kept + lane_sweeps


    def reset(self, now: float) -> list[TouchAction]:
        actions = []
        for contact in sorted(self._state._active_hold_tail):
            lane = self._state._active_hold_lane.get(contact, contact)
            actions.append(TouchAction(
                ActionKind.UP, lane, now, contact, "engine-reset"
            ))
        self._state.reset()
        self._note_tracker.reset()
        return actions

    # Backward-compatible accessors for tests and the engine.
    @property
    def hold_release_y(self) -> float:
        return self._config.hold_release_y

    @property
    def _active_hold_tail(self) -> dict[int, float]:
        return self._state._active_hold_tail

    @property
    def _active_hold_lane(self) -> dict[int, int]:
        return self._state._active_hold_lane

    @property
    def _hold_chord_partner(self) -> dict[int, int]:
        return self._state._hold_chord_partner

    @property
    def _frame_interval_seconds(self) -> float:
        return self._state._frame_interval_seconds

    @_frame_interval_seconds.setter
    def _frame_interval_seconds(self, value: float) -> None:
        self._state._frame_interval_seconds = value

    @property
    def filtered_adjacent_artifacts(self) -> int:
        return self._state.filtered_adjacent_artifacts

    @property
    def rejected_hold_candidates(self) -> int:
        return self._state.rejected_hold_candidates
