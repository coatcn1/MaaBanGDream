from __future__ import annotations

from ..note_detector import NoteKind, ObservedNote
from .actions import ActionKind, TouchAction
from .geometry import touch_x
from .state import PlannerConfig, PlannerState


class HoldPipeline:
    """Hold/slide lifecycle: matching, start, release, move, bookkeeping."""

    def __init__(self, config: PlannerConfig, state: PlannerState) -> None:
        self._config = config
        self._state = state

    def _record_diagnostic(
        self, event: str, now: float, **fields: object
    ) -> None:
        self._state.record_diagnostic(event, now, **fields)

    def _predict_hold_release(
        self, lane: int, previous_tail: float, previous_time: float,
        tail: float, now: float,
    ) -> None:
        elapsed = now - previous_time
        if elapsed <= 0 or tail >= self._config.hold_release_y:
            return
        velocity = (tail - previous_tail) / elapsed
        if 80 <= velocity <= 3000:
            release_at = now + (self._config.hold_release_y - tail) / velocity
            started = self._state._hold_started.get(lane, now)
            self._state._hold_release_at[lane] = max(
                now + .02,
                started + .30,
                release_at,
            )

    def _finish_hold(self, contact: int, now: float, reason: str) -> None:
        started = self._state._hold_started.get(contact, now)
        final_lane = self._state._active_hold_lane.get(contact, contact)
        self._record_diagnostic(
            "hold_release",
            now,
            lane=final_lane,
            release_method=reason,
            duration_ms=round(max(0.0, now - started) * 1000),
            body_confirmed=contact in self._state._hold_confirmed,
            contact=contact,
            final_lane=final_lane,
        )
        self._state._active_hold_tail.pop(contact, None)
        self._state._hold_seen.pop(contact, None)
        self._state._hold_release_at.pop(contact, None)
        self._state._hold_started.pop(contact, None)
        self._state._hold_confirmed.discard(contact)
        # The suppression window is lane-keyed; for a slide hold the contact
        # id is the stale START lane. Poisoning it kills real notes crossing
        # there within 0.4 s of the release. Record only where the finger
        # actually lifted.
        self._state._hold_released_at[final_lane] = now
        self._state._active_hold_lane.pop(contact, None)
        self._state._active_hold_x.pop(contact, None)
        self._state._hold_last_moved_at.pop(contact, None)
        self._state._hold_tail_flick.discard(contact)

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
        if contact in self._state._hold_tail_flick:
            actions.append(TouchAction(ActionKind.FLICK, lane, now, contact, reason))
        else:
            actions.append(TouchAction(ActionKind.UP, lane, now, contact, reason))
        self._finish_hold(contact, now, reason)
        # The UP action reports the hold's current observed lane, which can
        # differ from the last recorded active lane after a slide. Record the
        # release under the action lane too, or the post-release suppression
        # window will miss residue appearing where the finger actually lifted.
        self._state._hold_released_at[lane] = now
        partner = self._state._hold_chord_partner.pop(contact, None)
        if partner is None or partner not in self._state._active_hold_tail:
            return
        partner_tail = self._state._active_hold_tail[partner]
        if partner_tail < self._config.hold_release_y - self._config.paired_hold_rescue_margin:
            return
        partner_lane = self._state._active_hold_lane.get(partner, partner)
        if partner in self._state._hold_tail_flick:
            actions.append(TouchAction(
                ActionKind.FLICK, partner_lane, now, partner, f"{reason}-paired"
            ))
        else:
            actions.append(TouchAction(
                ActionKind.UP, partner_lane, now, partner, f"{reason}-paired"
            ))
        self._finish_hold(partner, now, f"{reason}-paired")
        self._state._hold_chord_partner.pop(partner, None)

    def _trigger_y(self, previous: ObservedNote, note: ObservedNote) -> float:
        elapsed = note.timestamp - previous.timestamp
        if elapsed <= 0:
            return self._config.judgement_y
        velocity = max(0.0, (note.y - previous.y) / elapsed)
        calibrated = self._config.judgement_y - velocity * self._state.timing_offset
        # Starting a hold slightly early is safe because the contact remains
        # pressed, while waiting for the ordinary-note line loses bodies that
        # are occluded by skill and judgement effects on their last frame.
        return min(self._config.judgement_y - 10.0, calibrated)

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

    def _linked_hold_body(
        self,
        head: ObservedNote,
        hold_notes: list[ObservedNote],
        *,
        now: float,
    ) -> ObservedNote | None:
        """Prove a detached head from the continuous body immediately above.

        Diagonal slides are segmented into one huge body whose centroid moves
        ahead across lanes plus a thin playable head ring at the judgement
        line.  The ring alone is deliberately insufficient hold evidence; the
        spatially connected high-confidence body makes the pair trustworthy.

        A body that is still tracked as an active hold, or that belongs to a
        hold released moments ago, must not validate a NEW head: its tail ring
        and the previous body can overlap the next lane for a few frames and
        would otherwise restart a phantom hold that sticks around for the
        hold_max_seconds failsafe (observed with TAP EFFECT 4 in SAVIOR OF
        SONG Hard: a 16 px tail fragment on lane 1 linked to the lane-0 body
        that was released in the same frame, then blocked lane 0 for 17 s).
        """
        suppress_window = self._config.hold_start_suppress_seconds
        recent_release_lanes = {
            lane
            for lane, released_at in self._state._hold_released_at.items()
            if now - released_at < suppress_window
        }
        head_top = head.y - head.height / 2
        candidates = []
        for body in hold_notes:
            if (
                body is head
                or body.height < 80
                or body.hold_body_confidence < .8
                or body.lane in recent_release_lanes
            ):
                continue
            body_bottom = body.y + body.height / 2
            horizontal_margin = max(20.0, head.width * .25)
            if (
                body.x - body.width / 2 - horizontal_margin
                <= head.x
                <= body.x + body.width / 2 + horizontal_margin
                and -20 <= head_top - body_bottom <= 60
            ):
                candidates.append(body)
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda body: abs(
                head_top - (body.y + body.height / 2)
            ),
        )

    @staticmethod
    def _same_wide_hold_shape(
        selected: ObservedNote,
        candidate: ObservedNote,
    ) -> bool:
        """Recognise extra components cut from one wide diagonal slide."""
        if (
            max(selected.width, candidate.width) < 180
            and max(selected.height, candidate.height) < 80
        ):
            return False
        selected_left = selected.x - selected.width / 2
        selected_right = selected.x + selected.width / 2
        candidate_left = candidate.x - candidate.width / 2
        candidate_right = candidate.x + candidate.width / 2
        horizontal_overlap = (
            min(selected_right, candidate_right)
            - max(selected_left, candidate_left)
        )
        selected_top = selected.y - selected.height / 2
        selected_bottom = selected.y + selected.height / 2
        candidate_top = candidate.y - candidate.height / 2
        candidate_bottom = candidate.y + candidate.height / 2
        vertical_gap = max(
            selected_top - candidate_bottom,
            candidate_top - selected_bottom,
            0,
        )
        return horizontal_overlap >= -30 and vertical_gap <= 35

    def process_frame(
        self,
        notes: list[ObservedNote],
        now: float,
        actions: list[TouchAction],
    ) -> dict[tuple[NoteKind, int], ObservedNote]:
        hold_notes = [note for note in notes if note.kind == NoteKind.HOLD]

        # A slanted hold crosses lane boundaries while the finger contact must
        # remain the same. Match every visible component to an existing contact
        # by continuous tail/x motion before considering new lane-local holds.
        current_holds: dict[int, tuple[ObservedNote, bool]] = {}
        used_notes: set[int] = set()
        for contact in sorted(self._state._active_hold_tail):
            if contact in self._state._blind_hold_contacts:
                # A chart-pressed straight hold has no visible body to follow.
                # Keep the finger on the head lane and let the chart release
                # the contact at the tail time instead of latching it to an
                # unrelated green fragment.
                continue
            previous_tail = self._state._active_hold_tail[contact]
            previous_seen = self._state._hold_seen.get(contact, now)
            elapsed = max(0.0, now - previous_seen)
            current_lane = self._state._active_hold_lane.get(contact, contact)
            hold_age = now - self._state._hold_started.get(contact, now)
            # A short slide can remain latched to unrelated green fragments
            # after its real tail disappears. If a new, fully confirmed body
            # reaches the line on the contact's original lane while the stale
            # contact has moved elsewhere, end the old gesture and let the
            # normal new-hold path press this head in the same frame.
            restarting_head = next(
                (
                    note for note in hold_notes
                    if (
                        current_lane != contact
                        and note.lane == contact
                        and hold_age >= .55
                        and note.width >= 180
                        and note.height >= 80
                        and note.hold_body_confidence >= .8
                        and self._hold_head(note) >= self._config.judgement_y - 10
                    )
                ),
                None,
            )
            if restarting_head is not None:
                self._release_hold(
                    contact,
                    current_lane,
                    now,
                    "new-hold-head",
                    actions,
                )
                continue
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
            previous_x = self._state._active_hold_x.get(
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
                        self._config.enable_slide
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

        # A wide diagonal slide is often segmented into a playable ring plus
        # two or three overlapping body components assigned to neighbouring
        # lanes. Once one component continues an active contact, the others
        # are parts of that contact—not new hold heads.
        continuing_components = [
            note for note, continuing in current_holds.values()
            if continuing
        ]
        for index, candidate in enumerate(hold_notes):
            if index in used_notes:
                continue
            if any(
                self._same_wide_hold_shape(selected, candidate)
                for selected in continuing_components
            ):
                used_notes.add(index)

        grouped_new: dict[int, list[tuple[int, ObservedNote]]] = {}
        for index, note in enumerate(hold_notes):
            if index not in used_notes:
                grouped_new.setdefault(note.lane, []).append((index, note))
        occupied_lanes = set(self._state._active_hold_lane.values())
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
            if continuing and contact not in self._state._active_hold_tail:
                # Already released as a chord partner earlier in this frame.
                continue
            key = (NoteKind.HOLD, note.lane)
            previous = self._state._previous.get(key)
            if (
                previous is not None
                and now - previous.timestamp > self._config.track_memory_seconds
            ):
                previous = None
            previous_seen = self._state._hold_seen.get(contact)
            self._state._hold_seen[contact] = now
            head = self._hold_head(note)
            tail = self._hold_tail(note)

            if note.hold_tail_flick:
                # Latch the marker: one clean sighting is enough, and the
                # release must stay a swipe even if later frames lose it.
                self._state._hold_tail_flick.add(contact)

            if continuing:
                previous_tail = self._state._active_hold_tail[contact]
                hold_age = now - self._state._hold_started.get(contact, now)
                chart_tail_lane = self._state._chart_tail_lane.get(contact)
                wrong_release_lane = (
                    chart_tail_lane is not None
                    and chart_tail_lane != note.lane
                )
                tail_ring_at_line = (
                    note.height <= 30
                    and note.width >= 70
                    and note.y >= 555
                    and contact in self._state._hold_confirmed
                    and hold_age >= .30
                    and not wrong_release_lane
                )
                if tail_ring_at_line:
                    self._release_hold(contact, note.lane, now, "tail-ring", actions)
                    continue
                if (
                    hold_age >= .30
                    and previous_tail < self._config.hold_release_y <= tail
                    and not wrong_release_lane
                ):
                    self._release_hold(
                        contact, note.lane, now, "tail-crossing", actions
                    )
                    continue
                if note.height >= 40 and not wrong_release_lane:
                    self._predict_hold_release(
                        contact, previous_tail, previous_seen or now, tail, now
                    )
                self._state._active_hold_tail[contact] = tail
                target_x = touch_x(note)
                previous_lane = self._state._active_hold_lane.get(contact, note.lane)
                previous_x = self._state._active_hold_x.get(contact, float(target_x))
                last_moved = self._state._hold_last_moved_at.get(
                    contact, float("-inf")
                )
                near_judgement_line = head >= self._config.judgement_y - 45
                should_move = (
                    self._config.enable_slide
                    and near_judgement_line
                    and (
                        note.lane != previous_lane
                        or abs(target_x - previous_x) >= 18
                    )
                    and now - last_moved >= .03
                )
                if near_judgement_line:
                    self._state._active_hold_lane[contact] = note.lane
                    self._state._active_hold_x[contact] = float(target_x)
                if should_move:
                    self._state._hold_last_moved_at[contact] = now
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
                linked_body = self._linked_hold_body(
                    note, hold_notes, now=now
                )
                should_rescue = (
                    self._config.rescue_first_visible
                    and (note.height >= 80 or linked_body)
                    and head >= self._config.judgement_y - 5
                    and (
                        previous is None
                        or self._hold_head(previous) < self._config.judgement_y - 5
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
                    and self._hold_head(other_note) >= self._config.judgement_y - 5
                    and abs(self._hold_head(other_note) - head)
                    <= self._config.paired_hold_rescue_margin
                    for (other_kind, other_lane), other_note
                    in selected_by_lane.items()
                )
                should_pair_rescue = (
                    contact not in self._state._active_hold_tail
                    and note.height >= 80
                    and head >= self._config.judgement_y - self._config.paired_hold_rescue_margin
                    and paired_due
                )
                if (
                    now - self._state._last_trigger.get(note.lane, float("-inf"))
                    < self._config.hold_start_suppress_seconds
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
                    and self._hold_head(previous)
                    <= self._trigger_y(previous, note) <= head
                )
                body_confirmed = (
                    note.height >= 80
                    or linked_body
                    or (
                        should_cross
                        and previous is not None
                        and previous.height >= 30
                        and note.height >= 30
                        and previous.hold_body_confidence >= .8
                        and note.hold_body_confidence >= .8
                    )
                )
                release_age = now - self._state._hold_released_at.get(note.lane, float("-inf"))
                strong_new_crossing = (
                    should_cross
                    and previous is not None
                    # At a 31 ms capture interval a real confirmed body can
                    # move from y=547/553 to beyond the trigger in one frame.
                    # Requiring the previous head to be above y=540 rejected
                    # those new holds merely because this lane had released
                    # an unrelated hold seconds earlier.  The normal hold
                    # trigger is y<=555, and should_cross already requires
                    # continuous falling geometry, so use that same bound.
                    and self._hold_head(previous) <= self._config.judgement_y - 10
                )
                restart_allowed = (
                    note.lane not in self._state._hold_released_at
                    or (
                        release_age >= self._config.hold_restart_cooldown_seconds
                        and (
                            strong_new_crossing
                            or should_rescue
                            or should_pair_rescue
                        )
                    )
                )
                if (
                    contact not in self._state._active_hold_tail
                    and restart_allowed
                    and body_confirmed
                    and (
                    should_rescue
                    or should_cross
                    or should_pair_rescue
                    )
                ):
                    if getattr(self._state, "_debug_holds", False) and note.lane == 2:
                        print(
                            "HOLDSTART", round(now, 3),
                            "restart_allowed", restart_allowed,
                            "body_confirmed", body_confirmed,
                            "should_rescue", should_rescue,
                            "should_cross", should_cross,
                            "should_pair_rescue", should_pair_rescue,
                            "head", round(head, 1),
                            "prev_head", None if previous is None else round(self._hold_head(previous), 1),
                            "release_age", round(release_age, 3),
                            "suppress", round(
                                now - self._state._last_trigger.get(note.lane, float("-inf")), 3
                            ),
                            flush=True,
                        )
                    tracking_tail = (
                        self._hold_tail(linked_body)
                        if linked_body is not None else tail
                    )
                    self._state._active_hold_tail[contact] = tracking_tail
                    self._state._hold_started[contact] = now
                    self._state._hold_confirmed.add(contact)
                    # Holds whose heads arrive together share one connector;
                    # link them so the release side can lift both contacts in
                    # the same frame later.
                    for other_contact, other_started in self._state._hold_started.items():
                        if (
                            other_contact != contact
                            and other_contact in self._state._active_hold_tail
                            and now - other_started <= 0.08
                        ):
                            self._state._hold_chord_partner[contact] = other_contact
                            self._state._hold_chord_partner[other_contact] = contact
                    target_x = touch_x(note)
                    self._state._active_hold_lane[contact] = note.lane
                    self._state._active_hold_x[contact] = float(target_x)
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
                    contact not in self._state._active_hold_tail
                    and head >= self._config.judgement_y - 5
                    and not body_confirmed
                ):
                    last_rejected = self._state._last_hold_rejection.get(
                        note.lane, float("-inf")
                    )
                    if now - last_rejected > self._config.track_memory_seconds:
                        self._state.rejected_hold_candidates += 1
                        self._state._last_hold_rejection[note.lane] = now
                        self._record_diagnostic(
                            "hold_candidate_rejected",
                            now,
                            lane=note.lane,
                            height=round(note.height, 2),
                            reason="unconfirmed-short-fragment",
                        )
                continue
        return selected_by_lane

    def finish_frame(
        self,
        selected_by_lane: dict[tuple[NoteKind, int], ObservedNote],
        now: float,
        actions: list[TouchAction],
    ) -> None:
        # Losing the green mask is not immediate evidence that the tail ended.
        # Skill animations can cover a long bar for many frames, so a predicted
        # release is valid only after the body has remained absent for a full
        # grace window. Visible bodies always override stale predictions.
        for contact, release_at in list(self._state._hold_release_at.items()):
            unseen_for = now - self._state._hold_seen.get(contact, now)
            held_for = now - self._state._hold_started.get(contact, now)
            if (
                contact in self._state._active_hold_tail
                and now >= release_at
                and unseen_for >= self._config.hold_grace_seconds
                and held_for >= .30
            ):
                lane = self._state._active_hold_lane.get(contact, contact)
                chart_tail_lane = self._state._chart_tail_lane.get(contact)
                if chart_tail_lane is not None and chart_tail_lane != lane:
                    # Predicted release belongs to a different lane than the
                    # chart tail; the chart predictor handles this contact.
                    continue
                self._release_hold(contact, lane, now, "predicted-tail", actions)
        for contact, started in list(self._state._hold_started.items()):
            if (
                contact in self._state._active_hold_tail
                and now - started >= self._config.hold_max_seconds
            ):
                lane = self._state._active_hold_lane.get(contact, contact)
                self._release_hold(contact, lane, now, "hold-failsafe", actions)
        remembered = {
            key: previous for key, previous in self._state._previous.items()
            if now - previous.timestamp <= self._config.track_memory_seconds
        }
        for key, note in selected_by_lane.items():
            previous = remembered.get(key)
            # A duplicate LDOpenGL frame carries no new motion information.
            # Preserve the last genuinely different position so the next fresh
            # frame has a useful velocity baseline.
            if previous is None or abs(note.y - previous.y) >= .2:
                remembered[key] = note
        self._state._previous = remembered
