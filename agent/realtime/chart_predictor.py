"""Chart-backed prediction for occluded notes and hold-tail releases.

The detector loses Hard slide-chart notes that are occluded by green trails
just before the judgement line, and hold releases can be late because the
grace window waits for the body to vanish.  With a bundled official chart
timeline the planner can:

1. calibrate the engine-to-song offset from the first trusted crossings;
2. press an imminent tap/hold-head when no trusted track is near the line
   (the detector missed it);
3. release active holds exactly at the chart tail time.

Prediction is only active for Hard+ runs with a matching chart.  Calibration
fails closed: if the first actions do not line up with the chart, prediction
stays disabled for the whole run (a different song is being played).
"""

from __future__ import annotations

from .chart_timeline import ChartJudgement, ChartTimeline
from .note_detector import NoteKind, ObservedNote
from .note_tracker import TrackedNote
from .touch_planner.actions import ActionKind, TouchAction
from .touch_planner.holds import HoldPipeline
from .touch_planner.state import PlannerState


TRUSTED_CALIBRATION_REASONS = {
    "crossing",
    "rescue",
    "reclassified-crossing",
    "predicted-crossing-rescue",
    "predicted-dropout-rescue",
}


class ChartPredictor:
    """Optional chart-timeline safety net for Hard+ realtime play."""

    def __init__(
        self,
        chart: ChartTimeline,
        *,
        judgement_y: float = 565,
        min_calibration_samples: int = 16,
        predict_presses: bool = False,
        press_bias_ms: int = 0,
    ) -> None:
        self.chart = chart
        self.judgement_y = float(judgement_y)
        self.min_calibration_samples = int(min_calibration_samples)
        self.predict_presses = bool(predict_presses)
        self.press_bias_s = int(press_bias_ms) / 1000.0
        self.calibrated = False
        self.song_offset_s = 0.0
        self.calibration_samples: list[tuple[float, int, str, float]] = []
        self.expected_hold_tail: dict[int, tuple[float, int]] = {}
        self.predicted_presses = 0
        self.predicted_releases = 0
        self.calibration_failed = False
        self._last_predicted: dict[int, float] = {}
        self._calibration_diagnosed = False
        self._calibration_failed_diagnosed = False
        self._anchor_time: float | None = None
        self._mistimed_lanes: dict[int, tuple[float, float]] = {}

    def reset(self) -> None:
        self.calibrated = False
        self.song_offset_s = 0.0
        self.calibration_samples = []
        self.expected_hold_tail = {}
        self.predicted_presses = 0
        self.predicted_releases = 0
        self.calibration_failed = False
        self._last_predicted = {}
        self._calibration_diagnosed = False
        self._calibration_failed_diagnosed = False
        self._anchor_time = None
        self._mistimed_lanes = {}

    def _relative(self, engine_time: float) -> float:
        """Convert an absolute monotonic engine time to song-relative time."""
        if self._anchor_time is None:
            self._anchor_time = engine_time
        return engine_time - self._anchor_time

    def validate_crossing(self, lane: int, engine_time: float) -> bool:
        """Return True when a crossing on ``lane`` matches the chart timing.

        Junk fragments near the line fire crossings on the right lane but
        far from the chart time.  Such crossings are suppressed and replaced
        by a chart-timed press, so the game receives one correctly-timed
        input instead of an early/late miss.
        """
        if not self.calibrated or not self.predict_presses:
            return True
        last_pred = self._last_predicted.get(lane, float("-inf"))
        if engine_time - last_pred <= 0.15:
            # The chart already pressed this lane in the last ~150 ms; a
            # detector crossing right after it is the same note's delayed
            # fragment and must not dispatch a duplicate input.
            return False
        song_now = self._relative(engine_time) + self.song_offset_s
        judgement = self.chart.judgement_near(
            lane,
            song_now,
            window_s=0.12,
        )
        if (
            judgement is not None
            and judgement.kind in ("tap", "hold-head")
            and abs(judgement.time_s - song_now) <= 0.12
        ):
            return True
        next_judgement = self.chart.next_judgement(lane, song_now - 0.05)
        if next_judgement is not None and next_judgement.kind == "tap":
            self._mistimed_lanes[lane] = (engine_time, next_judgement.time_s)
        return False

    def _feed_calibration(
        self,
        actions: list[TouchAction],
    ) -> None:
        if self.calibrated or self.calibration_failed:
            return
        for action in actions:
            if (
                action.kind not in (ActionKind.TAP, ActionKind.FLICK, ActionKind.DOWN)
                or action.reason not in TRUSTED_CALIBRATION_REASONS
            ):
                continue
            expected_kind = (
                "hold-head" if action.kind == ActionKind.DOWN else "tap"
            )
            self.calibration_samples.append((
                self._relative(action.timestamp),
                action.lane,
                expected_kind,
                0.0,
            ))
        if len(self.calibration_samples) < self.min_calibration_samples:
            return

        def count_matches(offset_s: float) -> tuple[int, float]:
            total = 0
            delta_sum = 0.0
            for engine_time, lane, kind, _ in self.calibration_samples:
                judgement = self.chart.judgement_near(
                    lane,
                    engine_time + offset_s,
                    window_s=0.12,
                )
                if judgement is not None and judgement.kind == kind:
                    total += 1
                    delta_sum += abs(
                        (engine_time + offset_s) - judgement.time_s
                    )
            return total, delta_sum

        best_offset = 0.0
        best_count = -1
        best_delta_sum = float("inf")
        offset = -8.0
        while offset <= 1.0:
            count, delta_sum = count_matches(offset)
            if count > best_count or (
                count == best_count and delta_sum < best_delta_sum
            ):
                best_count = count
                best_offset = offset
                best_delta_sum = delta_sum
            offset += 0.02
        # Refine around the coarse winner with a fine step.
        refined_offset = best_offset
        offset = best_offset - 0.1
        while offset <= best_offset + 0.1:
            count, delta_sum = count_matches(offset)
            if count > best_count or (
                count == best_count and delta_sum < best_delta_sum
            ):
                best_count = count
                refined_offset = offset
                best_delta_sum = delta_sum
            offset += 0.005
        best_offset = refined_offset
        required_matches = max(6, int(self.min_calibration_samples * 0.6))
        if best_count < required_matches:
            self.calibration_failed = True
            return
        self.song_offset_s = best_offset
        self.calibrated = True

    def _track_will_judge(
        self,
        tracked_notes: list[TrackedNote],
        lane: int,
        *,
        chart_time_s: float,
        now: float,
    ) -> bool:
        """True when a falling track on ``lane`` will judge this chart note.

        The normal pipeline presses at the trigger line, roughly 0.15-0.2 s
        before the chart time (perspective lead + capture latency).  A track
        whose predicted crossing is near the chart note means the detector
        already owns the note; predicting a press would only duplicate it.
        """
        target_engine = chart_time_s - self.song_offset_s
        for tracked in tracked_notes:
            if (
                tracked.note.kind not in (
                    NoteKind.TAP, NoteKind.FLICK, NoteKind.SKILL
                )
                or tracked.note.lane != lane
            ):
                continue
            if tracked.fired:
                # The note was already pressed.  A fired track sitting at the
                # line (or just past it) is the same note; a fired track far
                # above belongs to an earlier judgement and is irrelevant.
                if tracked.note.y >= self.judgement_y - 15:
                    return True
                continue
            if tracked.velocity_y <= 100:
                # A near-line track with no usable velocity only blocks the
                # chart press if it is a TRUSTED falling track (the pipeline
                # will fire crossing/rescue).  Junk fragments with 1-2 samples
                # never fire, so the chart must still rescue the note.
                trusted_without_velocity = (
                    tracked.note.y >= self.judgement_y - 120
                    and tracked.motion_samples >= 3
                    and tracked.downward_motion_frames >= 2
                    and tracked.minimum_y <= self.judgement_y - 40
                )
                if trusted_without_velocity:
                    return True
                continue
            remaining = self.judgement_y - tracked.note.y
            if remaining <= 0:
                continue
            crossing_at = self._relative(now) + remaining / tracked.velocity_y
            if abs(crossing_at - target_engine) <= 0.15:
                return True
        return False

    def _predict_presses(
        self,
        notes: list[ObservedNote],
        tracked_notes: list[TrackedNote],
        now: float,
        actions: list[TouchAction],
        state: PlannerState,
        holds: HoldPipeline,
    ) -> None:
        """Rescue taps whose chart judgement time has already passed.

        The detector is the primary trigger for ordinary taps.  A note can
        still be stuck on a residue, swallowed by a drifted hold body, or
        first visible only after the line, leaving the lane with no action.
        The chart then presses the tap shortly AFTER the judgement time so
        the input matches the game's judgement instead of preempting the
        detector with an early press.
        """
        song_now = self._relative(now) + self.song_offset_s
        occupied_lanes = set(state._active_hold_lane.values())
        for lane in range(self.chart.LANE_COUNT):
            next_judgement = self.chart.next_judgement(
                lane, song_now - 0.08
            )
            if next_judgement is None:
                continue
            target = next_judgement.time_s + self.press_bias_s
            lead = target - song_now
            if not -0.12 <= lead <= -0.03:
                continue
            # Only skip when a recent press on this lane already covered the
            # chart note.  Junk presses far from the chart time (or from a
            # different note) must not silence the prediction.
            expected_kind = (
                ActionKind.DOWN
                if next_judgement.kind == "hold-head"
                else ActionKind.TAP
            )
            last_kind = state._last_trigger_action_kind.get(lane)
            covered = any(
                timestamp is not None
                and kind == expected_kind
                and abs(
                    (self._relative(timestamp) + self.song_offset_s)
                    - target
                ) <= 0.12
                for timestamp, kind in (
                    (state._last_trigger.get(lane), last_kind),
                )
            )
            if covered:
                continue
            if next_judgement.kind == "tap":
                if lane in occupied_lanes:
                    continue
                mistimed_at, _ = self._mistimed_lanes.get(
                    lane, (float("-inf"), 0.0)
                )
                if self._track_will_judge(
                    tracked_notes,
                    lane,
                    chart_time_s=next_judgement.time_s,
                    now=now,
                ) and now - mistimed_at > 0.6:
                    continue
                actions.append(TouchAction(
                    ActionKind.TAP,
                    lane,
                    now,
                    reason="chart-predicted",
                ))
                self.predicted_presses += 1
                self._last_predicted[lane] = now
                state._last_trigger[lane] = now
                state._last_trigger_action_kind[lane] = ActionKind.TAP
                state.record_diagnostic(
                    "chart_predicted_press",
                    now,
                    lane=lane,
                    chart_time_s=round(next_judgement.time_s, 3),
                    lead_ms=round(lead * 1000, 1),
                    rescue=True,
                )
                self._mistimed_lanes.pop(lane, None)
            elif next_judgement.kind == "hold-head":
                self._predict_hold_head(
                    notes,
                    lane,
                    next_judgement,
                    now,
                    actions,
                    state,
                    holds,
                    song_now,
                )

    def _predict_hold_head(
        self,
        notes: list[ObservedNote],
        lane: int,
        judgement: ChartJudgement,
        now: float,
        actions: list[TouchAction],
        state: PlannerState,
        holds: HoldPipeline,
        song_now: float,
    ) -> None:
        """Press a hold head at the chart time when the detector cannot.

        The detector can lose hold heads in dense slide sections (green-body
        occlusion or a drifted hold occupying the lane).  A visible high
        confidence body on the lane is strong evidence, so the chart presses
        the head and registers the hold in the planner state to prevent a
        second start.  A lane occupied by a hold whose chart tail has already
        passed is force-released first.
        """
        if lane in set(state._active_hold_lane.values()):
            for contact, active_lane in list(state._active_hold_lane.items()):
                if active_lane != lane:
                    continue
                expected = self.expected_hold_tail.get(contact)
                if (
                    expected is not None
                    and song_now >= expected[0] - 0.05
                ):
                    holds._release_hold(
                        contact,
                        lane,
                        now,
                        "chart-lane-free",
                        actions,
                    )
                    state._previous.pop((NoteKind.HOLD, lane), None)
                    state._hold_released_at.pop(lane, None)
                    self.expected_hold_tail.pop(contact, None)
                    break
                if expected is None:
                    # A hold without any chart pair is a phantom (drifted
                    # body).  Real holds in this chart last at most ~1.6 s;
                    # an occupant older than 2 s must be stuck and is
                    # blocking the real head that is due now.
                    started = state._hold_started.get(contact, now)
                    if now - started >= 2.0:
                        holds._release_hold(
                            contact,
                            lane,
                            now,
                            "chart-lane-free",
                            actions,
                        )
                        state._previous.pop((NoteKind.HOLD, lane), None)
                        state._hold_released_at.pop(lane, None)
                        break
                return
            if lane in set(state._active_hold_lane.values()):
                return
        body = next(
            (
                note for note in notes
                if (
                    note.kind == NoteKind.HOLD
                    and note.lane == lane
                    and note.y + note.height / 2 >= self.judgement_y - 80
                )
            ),
            None,
        )
        if body is None:
            return
        contact = lane
        state._active_hold_tail[contact] = body.y - body.height / 2
        state._hold_started[contact] = now
        state._hold_confirmed.add(contact)
        state._active_hold_lane[contact] = lane
        state._active_hold_x[contact] = float(body.x)
        actions.append(TouchAction(
            ActionKind.DOWN,
            lane,
            now,
            contact,
            "chart-predicted",
            target_x=max(120, min(1160, round(body.x))),
        ))
        self.predicted_presses += 1
        self._last_predicted[lane] = now
        state._last_trigger[lane] = now
        state._last_trigger_action_kind[lane] = ActionKind.DOWN
        state.record_diagnostic(
            "chart_predicted_hold_head",
            now,
            lane=lane,
            contact=contact,
            chart_time_s=round(judgement.time_s, 3),
            body_y=round(body.y, 2),
        )

    def _free_lane_for_hold_head(
        self,
        lane: int,
        judgement: ChartJudgement,
        now: float,
        actions: list[TouchAction],
        state: PlannerState,
        holds: HoldPipeline,
        song_now: float,
    ) -> None:
        """Release a lane-occupying hold before a real head is due.

        Phantom/drifted holds (started on a residue body, then moved onto a
        lane with a real upcoming head) block the normal hold pipeline: the
        real head's body is visible but the lane stays occupied.  When the
        chart says a hold head is due on that lane, release the occupant if
        its own chart tail has passed or it has no chart identity and is old
        enough that it cannot be a real hold in this song.
        """
        for contact, active_lane in list(state._active_hold_lane.items()):
            if active_lane != lane:
                continue
            expected = self.expected_hold_tail.get(contact)
            if expected is not None:
                if song_now < expected[0] - 0.05:
                    continue
            else:
                started = state._hold_started.get(contact, now)
                if now - started < 2.0:
                    continue
            holds._release_hold(
                contact,
                lane,
                now,
                "chart-lane-free",
                actions,
            )
            state._previous.pop((NoteKind.HOLD, lane), None)
            state._hold_released_at.pop(lane, None)
            self.expected_hold_tail.pop(contact, None)
            state.record_diagnostic(
                "chart_lane_freed",
                now,
                lane=lane,
                contact=contact,
                chart_head_s=round(judgement.time_s, 3),
            )
            return

    def _schedule_hold_tails(
        self,
        actions: list[TouchAction],
    ) -> None:
        """Remember chart tail times for holds whose head just started."""
        for action in actions:
            if action.kind != ActionKind.DOWN:
                continue
            if action.contact in self.expected_hold_tail:
                continue
            head = self.chart.judgement_near(
                action.lane,
                self._relative(action.timestamp) + self.song_offset_s,
                window_s=0.35,
            )
            if head is None or head.kind != "hold-head":
                continue
            tail = self.chart.hold_tail_for_head(head)
            if tail is not None:
                self.expected_hold_tail[action.contact] = (
                    tail.time_s,
                    tail.lane,
                )

    def _release_due_holds(
        self,
        now: float,
        actions: list[TouchAction],
        state: PlannerState,
        holds: HoldPipeline,
    ) -> None:
        song_now = self._relative(now) + self.song_offset_s
        for contact, (tail_time, tail_lane) in list(
            self.expected_hold_tail.items()
        ):
            if contact not in state._active_hold_tail:
                self.expected_hold_tail.pop(contact, None)
                continue
            if song_now < tail_time - 0.015:
                continue
            lane = state._active_hold_lane.get(contact, contact)
            if lane != tail_lane:
                # The finger has not yet followed the slide to the tail
                # lane; releasing now would drop the tail judgement.  Let the
                # hold pipeline release when the tail ring reaches the line.
                continue
            holds._release_hold(
                contact,
                lane,
                now,
                "chart-tail",
                actions,
            )
            self.predicted_releases += 1
            self.expected_hold_tail.pop(contact, None)
            state.record_diagnostic(
                "chart_predicted_release",
                now,
                contact=contact,
                lane=lane,
                chart_time_s=round(tail_time, 3),
            )

    def update(
        self,
        notes: list[ObservedNote],
        tracked_notes: list[TrackedNote],
        now: float,
        actions: list[TouchAction],
        state: PlannerState,
        holds: HoldPipeline,
    ) -> list[TouchAction]:
        """Return ``actions`` plus any chart-predicted inputs."""
        self._relative(now)
        self._feed_calibration(actions)
        if not self.calibrated:
            if self.calibration_failed and not self._calibration_failed_diagnosed:
                state.record_diagnostic(
                    "chart_calibration_failed",
                    now,
                    samples=len(self.calibration_samples),
                )
                self._calibration_failed_diagnosed = True
            return actions
        if not self._calibration_diagnosed:
            state.record_diagnostic(
                "chart_calibrated",
                now,
                offset_ms=round(self.song_offset_s * 1000, 1),
                samples=len(self.calibration_samples),
            )
            self._calibration_diagnosed = True
        # Free lanes for due hold heads regardless of press prediction: the
        # normal hold pipeline can then start the real head next frame.
        song_now = self._relative(now) + self.song_offset_s
        for lane in range(self.chart.LANE_COUNT):
            next_judgement = self.chart.next_judgement(
                lane, song_now - 0.05
            )
            if (
                next_judgement is None
                or next_judgement.kind != "hold-head"
                or next_judgement.time_s - song_now > 0.06
            ):
                continue
            if lane in set(state._active_hold_lane.values()):
                self._free_lane_for_hold_head(
                    lane,
                    next_judgement,
                    now,
                    actions,
                    state,
                    holds,
                    song_now,
                )
        if self.predict_presses:
            self._predict_presses(
                notes, tracked_notes, now, actions, state, holds
            )
        self._schedule_hold_tails(actions)
        self._release_due_holds(now, actions, state, holds)
        return actions
