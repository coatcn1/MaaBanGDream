"""Chart-backed prediction for occluded notes and hold-tail releases.

The detector loses Hard slide-chart notes that are occluded by green trails
just before the judgement line, and hold releases can be late because the
grace window waits for the body to vanish.  With a bundled official chart
timeline the planner can:

1. calibrate the engine-to-song offset from the first trusted crossings;
2. press an imminent tap/hold-head when no trusted track is near the line
   (the detector missed it);
3. release active holds exactly at the chart tail time.

Prediction is only active for an exact song+difficulty match.  Calibration
fails closed: if the first actions do not line up with the chart, prediction
stays disabled for the whole run (a different song is being played).
"""

from __future__ import annotations

import statistics
from dataclasses import replace

from .chart_timeline import ChartJudgement, ChartTimeline
from .note_detector import NoteKind, ObservedNote
from .note_tracker import TrackedNote
from .touch_planner.actions import ActionKind, TouchAction
from .touch_planner.geometry import lane_center_x
from .touch_planner.holds import HoldPipeline
from .touch_planner.state import PlannerState


TRUSTED_CALIBRATION_REASONS = {
    "crossing",
    "rescue",
    "reclassified-crossing",
    "predicted-crossing-rescue",
    "predicted-dropout-rescue",
}

# Dense slide sections can emit a short burst of visual hold-body and tail
# artifacts even while the already-calibrated official chart remains aligned.
# Requiring a full calibration-window-sized disagreement preserves fail-closed
# behaviour for a genuinely wrong chart without abandoning a correct chart on
# the nine-action burst observed in the Hard representative trace.
MAX_CONSECUTIVE_VISUAL_CHART_MISMATCHES = 8
MAX_EARLY_PHASE_RELOCKS = 1
EARLY_PHASE_RELOCK_WINDOW_S = 12.0
PHASE_RELOCK_MAD_LIMIT_S = 0.020
PHASE_RELOCK_MAX_SHIFT_S = 0.200
PHASE_RELOCK_MIN_LANES = 2
PHASE_REFINEMENT_MAX_TOTAL_S = 0.006
PHASE_REFINEMENT_WINDOW_S = 30.0
HOLD_HEAD_CLAIM_WINDOW_S = 0.25
MAX_EARLY_VISUAL_HOLD_HEAD_S = 0.08
POST_CHART_INPUT_GRACE_S = 0.75
MIN_PROVISIONAL_RESCUE_TRACKS = 6
MIN_PROVISIONAL_RESCUE_JUDGEMENTS = 4
PROVISIONAL_OFFSET_WINDOW_S = 0.12
PROVISIONAL_MAD_LIMIT_S = 0.08
PROVISIONAL_CROSSING_WINDOW_S = 0.06
PRELOCK_SEMANTIC_OFFSET_WINDOW_S = 0.06
MIN_ADJACENT_ZIGZAG_TRANSITIONS = 3


def _adjacent_zigzag_anchor(
    path: tuple[tuple[float, float], ...],
) -> tuple[int, int] | None:
    """Return the two judgement lanes for a one-lane sawtooth slide.

    Garupa accepts a slide connection from the target lane and either
    neighbouring lane.  Repeatedly moving an already-held contact for a
    1-2-1-2 sawtooth adds input traffic but no judgement value, so these
    paths can use the midpoint shared by both judgement windows.
    """
    if len(path) < MIN_ADJACENT_ZIGZAG_TRANSITIONS + 1:
        return None
    lanes: list[int] = []
    for _time_s, lane_value in path:
        lane = round(lane_value)
        if abs(lane_value - lane) > 0.01:
            return None
        lanes.append(lane)
    lower = min(lanes)
    upper = max(lanes)
    if upper - lower != 1:
        return None
    transitions = sum(
        previous != current
        for previous, current in zip(lanes, lanes[1:])
    )
    if transitions < MIN_ADJACENT_ZIGZAG_TRANSITIONS:
        return None
    return lower, upper


class ChartPredictor:
    """Optional chart-timeline safety net for Hard+ realtime play."""

    def __init__(
        self,
        chart: ChartTimeline,
        *,
        judgement_y: float = 565,
        min_calibration_samples: int = 6,
        predict_presses: bool = False,
        press_bias_ms: int = 0,
    ) -> None:
        self.chart = chart
        self.judgement_y = float(judgement_y)
        self.min_calibration_samples = int(min_calibration_samples)
        self.predict_presses = bool(predict_presses)
        self.press_bias_s = 0.0
        self.set_press_bias_ms(press_bias_ms)
        self.calibrated = False
        self.song_offset_s = 0.0
        self.calibration_samples: list[tuple[float, int, str, float]] = []
        self.expected_hold_tail: dict[int, tuple[float, int]] = {}
        self.predicted_presses = 0
        self.predicted_releases = 0
        self.calibration_failed = False
        self._last_predicted: dict[int, float] = {}
        self._last_predicted_judgement: dict[
            int, tuple[int, str, float]
        ] = {}
        self._calibration_diagnosed = False
        self._calibration_failed_diagnosed = False
        self._anchor_time: float | None = None
        self._mistimed_lanes: dict[int, tuple[float, float]] = {}
        self.disabled_for_run = False
        self.disable_reason: str | None = None
        self._phase_residuals: list[float] = []
        self._phase_refinement_total = 0.0
        self._mismatch_streak = 0
        self._mismatch_direction: int | None = None
        self._mismatch_residuals: list[tuple[float, int]] = []
        self._calibrated_at_relative_s: float | None = None
        self._phase_relock_count = 0
        self._pending_phase_relock: dict | None = None
        self._disabled_diagnosed = False
        self._claimed_hold_note_indices: set[int] = set()
        self._track_phase_candidates: dict[
            int, list[tuple[float, int, int, float, float]]
        ] = {}
        self._sampled_track_ids: set[int] = set()
        self._consumed_judgements: set[tuple[int, str]] = set()
        self._phase_validation_track_ids: set[int] = set()
        self._phase_validation_judgement_indices: set[int] = set()
        self._prelock_semantic_offset_s: float | None = None
        self._prelock_semantic_judgement_indices: set[int] = set()
        self._pending_opening_semantic_lock: dict | None = None

    def reset(self) -> None:
        self.calibrated = False
        self.song_offset_s = 0.0
        self.calibration_samples = []
        self.expected_hold_tail = {}
        self.predicted_presses = 0
        self.predicted_releases = 0
        self.calibration_failed = False
        self._last_predicted = {}
        self._last_predicted_judgement = {}
        self._calibration_diagnosed = False
        self._calibration_failed_diagnosed = False
        self._anchor_time = None
        self._mistimed_lanes = {}
        self.disabled_for_run = False
        self.disable_reason = None
        self._phase_residuals = []
        self._phase_refinement_total = 0.0
        self._mismatch_streak = 0
        self._mismatch_direction = None
        self._mismatch_residuals = []
        self._calibrated_at_relative_s = None
        self._phase_relock_count = 0
        self._pending_phase_relock = None
        self._disabled_diagnosed = False
        self._claimed_hold_note_indices = set()
        self._track_phase_candidates = {}
        self._sampled_track_ids = set()
        self._consumed_judgements = set()
        self._phase_validation_track_ids = set()
        self._phase_validation_judgement_indices = set()
        self._prelock_semantic_offset_s = None
        self._prelock_semantic_judgement_indices = set()
        self._pending_opening_semantic_lock = None

    def recover_touch_state(self) -> None:
        """Forget chart contacts released outside the normal planner flow."""
        self.expected_hold_tail.clear()

    def _relative(self, engine_time: float) -> float:
        """Convert an absolute monotonic engine time to song-relative time."""
        if self._anchor_time is None:
            self._anchor_time = engine_time
        return engine_time - self._anchor_time

    def input_window_finished(self, engine_time: float) -> bool:
        """Return whether an exact, locked chart can no longer need input."""
        if (
            self.disabled_for_run
            or not self.calibrated
            or self._anchor_time is None
        ):
            return False
        song_now = self._relative(engine_time) + self.song_offset_s
        return song_now > self.chart.end_time_s + POST_CHART_INPUT_GRACE_S

    def song_time(self, engine_time: float) -> float:
        return self._relative(engine_time) + self.song_offset_s

    def input_song_time(self, engine_time: float) -> float:
        """Song clock advanced by the calibrated device-input latency."""
        return self.song_time(engine_time) - self.press_bias_s

    def set_press_bias_ms(self, value: int) -> None:
        """Apply the planner offset using its positive-means-earlier sign."""
        self.press_bias_s = -int(value) / 1000.0

    def validate_crossing(self, lane: int, engine_time: float) -> bool:
        """Return True when a crossing on ``lane`` matches the chart timing.

        Junk fragments near the line fire crossings on the right lane but
        far from the chart time.  Such crossings are suppressed and replaced
        by a chart-timed press, so the game receives one correctly-timed
        input instead of an early/late miss.
        """
        if (
            self.disabled_for_run
            or not self.calibrated
            or not self.predict_presses
        ):
            return True
        last_pred = self._last_predicted.get(lane, float("-inf"))
        if engine_time - last_pred <= 0.15:
            # The chart already pressed this lane in the last ~150 ms; a
            # detector crossing right after it is the same note's delayed
            # fragment and must not dispatch a duplicate input.
            return False
        song_now = self._relative(engine_time) + self.song_offset_s
        # Once locked, the chart is the tap/flick clock.  A visual crossing is
        # phase evidence only; dispatching it here would race the chart and
        # reintroduce capture jitter.
        next_judgement = self.chart.next_judgement(lane, song_now - 0.05)
        if next_judgement is not None and next_judgement.kind == "tap":
            self._mistimed_lanes[lane] = (engine_time, next_judgement.time_s)
        return False

    def observe_visual_actions(self, actions: list[TouchAction]) -> None:
        """Use trusted visual actions for initial lock and ongoing phase lock.

        After calibration, repeated visual/chart disagreement disables chart
        input for the rest of the song.  Small matched residuals adjust the
        offset gradually, keeping device/capture drift in a closed loop.
        """
        if self.disabled_for_run:
            return
        # Action timestamps are deliberately ignored for both initial and
        # ongoing phase.  The detector triggers above the judgement line, so
        # treating TAP/DOWN as an over-line timestamp creates a stable
        # 80-150 ms bias.  Only projected crossings in ``observe_tracks`` may
        # establish or adjust song phase.

    def _try_early_phase_relock(self, relative_time_s: float | None) -> bool:
        """Apply one bounded low-MAD correction shortly after initial lock.

        Eight unique chart judgements that agree in direction and magnitude
        across multiple lanes are stronger evidence of an early off-by-one
        phase choice than of a wrong local chart.  Only the first such burst,
        within a bounded window, may move the song clock.  Any later repeated
        disagreement still fails closed to visual input.
        """
        if (
            self._phase_relock_count >= MAX_EARLY_PHASE_RELOCKS
            or self._calibrated_at_relative_s is None
            or relative_time_s is None
            or relative_time_s < self._calibrated_at_relative_s
            or relative_time_s - self._calibrated_at_relative_s
            > EARLY_PHASE_RELOCK_WINDOW_S
        ):
            return False
        evidence = self._mismatch_residuals[
            -MAX_CONSECUTIVE_VISUAL_CHART_MISMATCHES:
        ]
        if (
            len(evidence) < MAX_CONSECUTIVE_VISUAL_CHART_MISMATCHES
            or len({lane for _residual, lane in evidence})
            < PHASE_RELOCK_MIN_LANES
        ):
            return False
        residuals = [residual for residual, _lane in evidence]
        median = float(statistics.median(residuals))
        mad = float(statistics.median(
            abs(residual - median) for residual in residuals
        ))
        if (
            mad > PHASE_RELOCK_MAD_LIMIT_S
            or abs(median) > PHASE_RELOCK_MAX_SHIFT_S
        ):
            return False
        previous_offset = self.song_offset_s
        self.song_offset_s += median
        self._phase_relock_count += 1
        self._pending_phase_relock = {
            "previous_offset_ms": round(previous_offset * 1000, 1),
            "offset_ms": round(self.song_offset_s * 1000, 1),
            "correction_ms": round(median * 1000, 1),
            "mad_ms": round(mad * 1000, 1),
            "samples": len(evidence),
            "lanes": len({lane for _residual, lane in evidence}),
        }
        self._mismatch_streak = 0
        self._mismatch_direction = None
        self._mismatch_residuals = []
        self._phase_residuals = []
        return True

    def _record_phase_residual(
        self,
        residual: float,
        lane: int,
        *,
        relative_time_s: float | None = None,
    ) -> None:
        if abs(residual) > 0.08:
            direction = 1 if residual > 0 else -1
            if self._mismatch_direction != direction:
                # Song-clock drift is directional.  Dense note projection can
                # jump to the neighbouring same-lane judgement and produce a
                # burst of large residuals on both sides of zero; treating
                # those as one streak disabled the correct chart in the fatal
                # Hibana Expert trace.  A sign reversal starts new evidence.
                self._mismatch_streak = 0
                self._mismatch_direction = direction
                self._mismatch_residuals = []
            self._mismatch_streak += 1
            self._mismatch_residuals.append((residual, lane))
            if self._mismatch_streak >= MAX_CONSECUTIVE_VISUAL_CHART_MISMATCHES:
                if self._try_early_phase_relock(relative_time_s):
                    return
                self.disabled_for_run = True
                self.disable_reason = (
                    f"{MAX_CONSECUTIVE_VISUAL_CHART_MISMATCHES} same-direction "
                    f"credible phase residuals exceeded 80ms (lane={lane}, "
                    f"residual_ms={residual * 1000:.1f})"
                )
            return
        self._mismatch_streak = 0
        self._mismatch_direction = None
        self._mismatch_residuals = []
        self._phase_residuals.append(residual)
        self._phase_residuals = self._phase_residuals[-9:]
        # 锁定后的连续相位精修必须严格有界：视觉投影在密集段落带有
        # 系统性偏差，无上限的小步累积曾在整局内漂移 40ms+，把中段按压
        # 整体推到判定窗外。总修正量封顶 ±6ms，且只在锁定后 30 秒内进行；
        # 更大的会话级误差交由游戏 FAST/SLOW 反馈回路修正按压偏移。
        if (
            len(self._phase_residuals) >= 4
            and abs(self._phase_refinement_total)
            < PHASE_REFINEMENT_MAX_TOTAL_S
            and (
                relative_time_s is None
                or self._calibrated_at_relative_s is None
                or relative_time_s - self._calibrated_at_relative_s
                <= PHASE_REFINEMENT_WINDOW_S
            )
        ):
            median = statistics.median(self._phase_residuals)
            adjustment = max(-0.002, min(0.002, median * 0.25))
            remaining = (
                PHASE_REFINEMENT_MAX_TOTAL_S
                - abs(self._phase_refinement_total)
            )
            adjustment = max(-remaining, min(remaining, adjustment))
            self.song_offset_s += adjustment
            self._phase_refinement_total += adjustment

    def observe_tracks(
        self,
        tracked_notes: list[TrackedNote],
        now: float,
    ) -> None:
        """Acquire initial phase from predicted visual line-crossing times.

        Each track contributes candidates against same-lane chart taps.  A
        lock needs six unique tracks on at least two lanes and a <=20 ms MAD.
        No touch action is needed, so the first chart-owned input can occur as
        soon as the falling trajectories provide enough evidence.
        """
        if self.disabled_for_run:
            return
        relative_now = self._relative(now)
        if self.calibrated:
            for tracked in tracked_notes:
                if tracked.track_id in self._phase_validation_track_ids:
                    continue
                if (
                    tracked.note.kind not in {
                        NoteKind.TAP, NoteKind.FLICK, NoteKind.SKILL,
                    }
                    or tracked.velocity_y < 100
                    or tracked.sample_count < 4
                    or tracked.motion_samples < 4
                    or tracked.downward_motion_frames < 3
                ):
                    continue
                remaining = self.judgement_y - tracked.note.y
                if not 0 < remaining <= 140:
                    continue
                self._phase_validation_track_ids.add(tracked.track_id)
                crossing_song_time = (
                    relative_now + remaining / tracked.velocity_y
                    + self.song_offset_s
                )
                judgement = self.chart.judgement_near(
                    tracked.note.lane,
                    crossing_song_time,
                    window_s=0.35,
                )
                if judgement is None or judgement.kind != "tap":
                    continue
                if (
                    judgement.note_index
                    in self._phase_validation_judgement_indices
                ):
                    # Dense sections frequently split one physical head into
                    # several independently tracked visual fragments.  Phase
                    # disagreement is defined over chart judgements, not
                    # detector track IDs; otherwise one note can consume the
                    # entire eight-sample fail-closed budget in a few frames.
                    continue
                self._phase_validation_judgement_indices.add(
                    judgement.note_index
                )
                self._record_phase_residual(
                    judgement.time_s - crossing_song_time,
                    tracked.note.lane,
                    relative_time_s=relative_now,
                )
                if self.disabled_for_run:
                    return
            return
        if self.calibration_failed:
            return
        for tracked in tracked_notes:
            if tracked.track_id in self._sampled_track_ids:
                continue
            if (
                tracked.note.kind not in {NoteKind.TAP, NoteKind.FLICK, NoteKind.SKILL}
                or tracked.velocity_y < 100
                or tracked.sample_count < 4
                or tracked.motion_samples < 4
                or tracked.downward_motion_frames < 3
            ):
                continue
            remaining = self.judgement_y - tracked.note.y
            if not 0 < remaining <= 140:
                continue
            crossing_relative = relative_now + remaining / tracked.velocity_y
            candidates = [
                (
                    judgement.time_s - crossing_relative,
                    tracked.note.lane,
                    judgement.note_index,
                    judgement.time_s,
                    crossing_relative,
                )
                for judgement in self.chart.judgements
                if judgement.lane == tracked.note.lane
                and judgement.kind == "tap"
                and -12.0 <= judgement.time_s - crossing_relative <= 3.0
            ]
            if candidates:
                self._sampled_track_ids.add(tracked.track_id)
                self._track_phase_candidates[tracked.track_id] = candidates
        if len(self._track_phase_candidates) < self.min_calibration_samples:
            return

        best: list[tuple[float, int, int, float, float]] = []
        best_error = float("inf")
        solutions: list[
            tuple[list[tuple[float, int, int, float, float]], float, float]
        ] = []
        centers = [
            offset
            for candidates in self._track_phase_candidates.values()
            for offset, _lane, _note_index, _chart_time, _crossing in candidates
        ]
        for center in centers:
            cluster: list[tuple[float, int, int, float, float]] = []
            used_notes: set[int] = set()
            previous_chart_time = float("-inf")
            ordered_candidates = sorted(
                self._track_phase_candidates.values(),
                key=lambda items: items[0][4],
            )
            for candidates in ordered_candidates:
                eligible = [
                    item for item in candidates
                    if abs(item[0] - center) <= 0.04
                    and item[2] not in used_notes
                    and item[3] >= previous_chart_time - 0.05
                ]
                if not eligible:
                    continue
                nearest = min(eligible, key=lambda item: abs(item[0] - center))
                cluster.append(nearest)
                used_notes.add(nearest[2])
                previous_chart_time = nearest[3]
            error = sum(abs(item[0] - center) for item in cluster)
            if cluster:
                solutions.append((
                    cluster,
                    error,
                    float(statistics.median(item[0] for item in cluster)),
                ))
            if len(cluster) > len(best) or (
                len(cluster) == len(best) and error < best_error
            ):
                best = cluster
                best_error = error
        if len(best) < self.min_calibration_samples:
            return
        ranked: list[
            tuple[list[tuple[float, int, int, float, float]], float, float]
        ] = []
        for solution in sorted(
            solutions, key=lambda item: (-len(item[0]), item[1]),
        ):
            if any(abs(solution[2] - known[2]) < 0.08 for known in ranked):
                continue
            ranked.append(solution)
        if len(ranked) > 1 and len(ranked[1][0]) >= len(ranked[0][0]) - 1:
            # Periodic lane patterns can yield several low-MAD offsets.  Keep
            # collecting trajectories until one one-to-one ordered alignment
            # is clearly stronger; never take control on an ambiguous phase.
            return
        best, best_error, _best_center = ranked[0]
        offsets = [item[0] for item in best]
        median = statistics.median(offsets)
        mad = statistics.median(abs(item - median) for item in offsets)
        if len({item[1] for item in best}) < 2 or mad > 0.020:
            return
        self.song_offset_s = float(median)
        self.calibrated = True
        self._calibrated_at_relative_s = relative_now
        self.calibration_samples = [
            (crossing, lane, "track-crossing", offset)
            for offset, lane, _note_index, _chart_time, crossing in best
        ]

    def provisional_residue_judgement(
        self,
        lane: int,
        engine_time: float,
    ) -> ChartJudgement | None:
        """Confirm one visible late fragment before strict chart lock.

        This does not enable chart scheduling or mutate the phase.  It only
        answers when an exact local chart, six projected visual trajectories,
        and four ordered distinct judgements agree that a fragment already
        visible on ``lane`` is crossing now.  The wider early-velocity limit
        is safe here because no blind input is created: visual evidence is
        still mandatory.
        """
        if (
            self.disabled_for_run
            or self.calibrated
            or self.calibration_failed
            or not self.predict_presses
            or self._anchor_time is None
            or len(self._track_phase_candidates)
            < max(MIN_PROVISIONAL_RESCUE_TRACKS, self.min_calibration_samples)
        ):
            return None
        relative_now = engine_time - self._anchor_time
        candidates = [
            item
            for items in self._track_phase_candidates.values()
            for item in items
            if item[1] == lane
            and abs(item[4] - relative_now) <= PROVISIONAL_CROSSING_WINDOW_S
        ]
        ranked: list[tuple[int, int, float, float, ChartJudgement]] = []
        ordered_tracks = sorted(
            self._track_phase_candidates.values(),
            key=lambda items: items[0][4],
        )
        for candidate in candidates:
            candidate_offset = candidate[0]
            support: list[tuple[float, int, int, float, float]] = []
            used_notes: set[int] = set()
            previous_chart_time = float("-inf")
            for track_candidates in ordered_tracks:
                eligible = [
                    item
                    for item in track_candidates
                    if abs(item[0] - candidate_offset)
                    <= PROVISIONAL_OFFSET_WINDOW_S
                    and item[2] not in used_notes
                    and item[3] >= previous_chart_time - 0.05
                ]
                if not eligible:
                    continue
                nearest = min(
                    eligible,
                    key=lambda item: abs(item[0] - candidate_offset),
                )
                support.append(nearest)
                used_notes.add(nearest[2])
                previous_chart_time = nearest[3]
            if len(support) < MIN_PROVISIONAL_RESCUE_JUDGEMENTS:
                continue
            lanes = {item[1] for item in support}
            if len(lanes) < 2:
                continue
            offsets = [item[0] for item in support]
            median = statistics.median(offsets)
            mad = statistics.median(abs(item - median) for item in offsets)
            if (
                mad > PROVISIONAL_MAD_LIMIT_S
                or abs(candidate_offset - median) > PROVISIONAL_MAD_LIMIT_S
            ):
                continue
            judgement = next((
                item
                for item in self.chart.judgements
                if item.note_index == candidate[2] and item.kind == "tap"
            ), None)
            if judgement is None:
                continue
            ranked.append((
                len(support),
                len(lanes),
                -mad,
                -abs(candidate[4] - relative_now),
                judgement,
            ))
        if not ranked:
            return None
        return max(ranked, key=lambda item: item[:4])[4]

    def apply_chart_flick_semantics(
        self,
        actions: list[TouchAction],
        state: PlannerState,
    ) -> list[TouchAction]:
        """Apply explicit chart flick kind/direction to matched visual taps."""
        if self.disabled_for_run:
            return actions
        if not self.calibrated:
            return self._apply_prelock_flick_semantics(actions, state)
        enriched: list[TouchAction] = []
        for action in actions:
            if (
                action.kind not in {ActionKind.TAP, ActionKind.FLICK}
                or action.contact is not None
            ):
                enriched.append(action)
                continue
            song_time = self._relative(action.timestamp) + self.song_offset_s
            judgement = self.chart.judgement_near(
                action.lane,
                song_time,
                window_s=0.18,
            )
            if judgement is None or judgement.kind != "tap":
                enriched.append(action)
                continue
            expected_kind = (
                ActionKind.FLICK if judgement.flick else ActionKind.TAP
            )
            expected_direction = (
                judgement.direction if judgement.flick else None
            )
            if (
                action.kind == expected_kind
                and action.flick_direction == expected_direction
            ):
                enriched.append(action)
                continue
            updated = replace(
                action,
                kind=expected_kind,
                flick_direction=expected_direction,
            )
            enriched.append(updated)
            state._last_trigger_action_kind[action.lane] = expected_kind
            state.record_diagnostic(
                "chart_flick_semantics",
                action.timestamp,
                lane=action.lane,
                visual_kind=action.kind.value,
                chart_kind=expected_kind.value,
                direction=expected_direction,
            )
        return enriched

    def _apply_prelock_flick_semantics(
        self,
        actions: list[TouchAction],
        state: PlannerState,
    ) -> list[TouchAction]:
        """Upgrade visible FLICK kinds using a semantic-only provisional phase.

        Periodic lane patterns can keep strict phase calibration ambiguous for
        the first few seconds.  An exact, fully visible first all-FLICK chord
        establishes a provisional offset without granting scheduling control.
        Later visible actions may reuse that offset only when their own track
        candidate identifies the exact chart note.  This changes input kind;
        it never creates an input or changes the authoritative song clock.
        """
        transient = [
            action
            for action in actions
            if action.kind in {ActionKind.TAP, ActionKind.FLICK}
            and action.contact is None
        ]
        if not transient:
            return actions
        if (
            not self.predict_presses
            or self._anchor_time is None
            or len(self._track_phase_candidates)
            < max(MIN_PROVISIONAL_RESCUE_TRACKS, self.min_calibration_samples)
        ):
            return actions
        if self._prelock_semantic_offset_s is not None:
            return self._apply_followup_prelock_flicks(
                actions,
                transient,
                state,
            )
        first_time = min(
            judgement.time_s for judgement in self.chart.judgements
        )
        opening = [
            judgement
            for judgement in self.chart.judgements
            if abs(judgement.time_s - first_time) <= 0.03
        ]
        if (
            len(opening) < 2
            or any(
                judgement.kind != "tap" or not judgement.flick
                for judgement in opening
            )
        ):
            return actions
        opening_by_lane = {judgement.lane: judgement for judgement in opening}
        if (
            len(opening_by_lane) != len(opening)
            or {action.lane for action in transient} != set(opening_by_lane)
            or len(transient) != len(opening)
        ):
            return actions
        relative_by_lane = {
            action.lane: action.timestamp - self._anchor_time
            for action in transient
        }
        semantic_offsets: list[float] = []
        for lane, relative_now in relative_by_lane.items():
            opening_note_index = opening_by_lane[lane].note_index
            matches = [
                (offset, crossing_relative)
                for candidates in self._track_phase_candidates.values()
                for (
                    offset,
                    candidate_lane,
                    note_index,
                    _chart_time,
                    crossing_relative,
                ) in candidates
                if (
                    candidate_lane == lane
                    and note_index == opening_note_index
                    and abs(crossing_relative - relative_now)
                    <= PROVISIONAL_OFFSET_WINDOW_S
                )
            ]
            if not matches:
                return actions
            semantic_offsets.append(min(
                matches,
                key=lambda item: abs(item[1] - relative_now),
            )[0])
        self._prelock_semantic_offset_s = float(statistics.median(
            semantic_offsets
        ))
        self._prelock_semantic_judgement_indices.update(
            judgement.note_index for judgement in opening
        )
        enriched: list[TouchAction] = []
        for action in actions:
            judgement = opening_by_lane.get(action.lane)
            if action not in transient or judgement is None:
                enriched.append(action)
                continue
            updated = replace(
                action,
                kind=ActionKind.FLICK,
                flick_direction=judgement.direction,
            )
            enriched.append(updated)
            state._last_trigger_action_kind[action.lane] = ActionKind.FLICK
            state.record_diagnostic(
                "chart_provisional_opening_flick",
                action.timestamp,
                lane=action.lane,
                visual_kind=action.kind.value,
                direction=judgement.direction,
                offset_ms=round(self._prelock_semantic_offset_s * 1000, 1),
            )
        return enriched

    def _apply_followup_prelock_flicks(
        self,
        actions: list[TouchAction],
        transient: list[TouchAction],
        state: PlannerState,
    ) -> list[TouchAction]:
        semantic_offset = self._prelock_semantic_offset_s
        if semantic_offset is None or self._anchor_time is None:
            return actions
        tap_judgements = {
            judgement.note_index: judgement
            for judgement in self.chart.judgements
            if judgement.kind == "tap" and judgement.flick
        }
        enriched: list[TouchAction] = []
        for action in actions:
            if action not in transient:
                enriched.append(action)
                continue
            relative_now = action.timestamp - self._anchor_time
            candidates = [
                (
                    abs(offset - semantic_offset)
                    + abs(crossing_relative - relative_now),
                    judgement,
                )
                for track_candidates in self._track_phase_candidates.values()
                for (
                    offset,
                    candidate_lane,
                    note_index,
                    _chart_time,
                    crossing_relative,
                ) in track_candidates
                for judgement in [tap_judgements.get(note_index)]
                if (
                    judgement is not None
                    and candidate_lane == action.lane
                    and note_index
                    not in self._prelock_semantic_judgement_indices
                    and abs(offset - semantic_offset)
                    <= PRELOCK_SEMANTIC_OFFSET_WINDOW_S
                    and abs(crossing_relative - relative_now)
                    <= PROVISIONAL_OFFSET_WINDOW_S
                )
            ]
            if not candidates:
                enriched.append(action)
                continue
            _score, judgement = min(candidates, key=lambda item: item[0])
            updated = replace(
                action,
                kind=ActionKind.FLICK,
                flick_direction=judgement.direction,
            )
            enriched.append(updated)
            self._prelock_semantic_judgement_indices.add(
                judgement.note_index
            )
            state._last_trigger_action_kind[action.lane] = ActionKind.FLICK
            state.record_diagnostic(
                "chart_provisional_flick_semantics",
                action.timestamp,
                lane=action.lane,
                note_index=judgement.note_index,
                visual_kind=action.kind.value,
                direction=judgement.direction,
            )
        self._try_promote_opening_semantic_lock(
            max(action.timestamp for action in transient),
        )
        return enriched

    def _try_promote_opening_semantic_lock(self, engine_time: float) -> bool:
        """Promote two fully-confirmed opening FLICK chords to chart control.

        This deliberately covers only the exact Hyadain-style opening seen in
        the real trace: two consecutive, same-lane, all-FLICK chords.  Each of
        their four judgements must have been matched by its own projected track,
        while at least six trajectories still agree within the normal 20 ms
        phase MAD.  Ordinary openings and partially seen second chords remain
        visual-only until strict calibration succeeds.
        """
        if (
            self.calibrated
            or self._anchor_time is None
            or self._prelock_semantic_offset_s is None
        ):
            return False
        times = sorted({judgement.time_s for judgement in self.chart.judgements})
        if len(times) < 2:
            return False
        groups = [
            [
                judgement
                for judgement in self.chart.judgements
                if abs(judgement.time_s - time_s) <= 0.001
            ]
            for time_s in times[:2]
        ]
        if any(
            len(group) < 2
            or any(
                judgement.kind != "tap" or not judgement.flick
                for judgement in group
            )
            for group in groups
        ):
            return False
        lane_sets = [{judgement.lane for judgement in group} for group in groups]
        if lane_sets[0] != lane_sets[1] or len(lane_sets[0]) < 2:
            return False
        required_indices = {
            judgement.note_index for group in groups for judgement in group
        }
        if not required_indices.issubset(
            self._prelock_semantic_judgement_indices
        ):
            return False

        support: list[tuple[float, int, int, float]] = []
        semantic_offset = self._prelock_semantic_offset_s
        for candidates in self._track_phase_candidates.values():
            eligible = [
                item for item in candidates
                if abs(item[0] - semantic_offset)
                <= PRELOCK_SEMANTIC_OFFSET_WINDOW_S
            ]
            if not eligible:
                continue
            offset, lane, _note_index, _chart_time, crossing = min(
                eligible,
                key=lambda item: abs(item[0] - semantic_offset),
            )
            support.append((offset, lane, _note_index, crossing))
        if (
            len(support) < self.min_calibration_samples
            or len({item[1] for item in support}) < 2
        ):
            return False
        offsets = [item[0] for item in support]
        median = float(statistics.median(offsets))
        mad = float(statistics.median(abs(offset - median) for offset in offsets))
        if mad > 0.020:
            return False

        self.song_offset_s = median
        self.calibrated = True
        self._calibrated_at_relative_s = self._relative(engine_time)
        self.calibration_samples = [
            (crossing, lane, "opening-flick-track", offset)
            for offset, lane, _note_index, crossing in support
        ]
        self._consumed_judgements.update(
            (note_index, "tap") for note_index in required_indices
        )
        self._pending_opening_semantic_lock = {
            "offset_ms": round(median * 1000, 1),
            "mad_ms": round(mad * 1000, 1),
            "samples": len(support),
            "judgements": len(required_indices),
            "lanes": len(lane_sets[0]),
        }
        return True

    def filter_chart_owned_holds(
        self,
        actions: list[TouchAction],
        now: float,
        state: PlannerState,
        holds: HoldPipeline,
    ) -> list[TouchAction]:
        """Discard visual hold starts that do not match an exact chart head.

        Once press prediction owns the clock, a green tail ring or judgement
        effect must not create a persistent contact merely because it survived
        the ordinary hold detector.  Matching visual heads remain useful for
        geometry; unmatched heads are rolled back before dispatch.
        """
        if self.disabled_for_run or not self.calibrated or not self.predict_presses:
            return actions
        suppressed_contacts: set[int] = set()
        for action in actions:
            if action.kind != ActionKind.DOWN:
                continue
            song_time = self.song_time(action.timestamp)
            nearest = self.chart.judgement_near(
                action.lane,
                song_time,
                window_s=HOLD_HEAD_CLAIM_WINDOW_S,
            )
            early_by = (
                nearest.time_s - song_time
                if nearest is not None and nearest.kind == "hold-head"
                else None
            )
            if (
                nearest is not None
                and nearest.kind == "hold-head"
                and nearest.note_index not in self._claimed_hold_note_indices
                and early_by is not None
                and early_by <= MAX_EARLY_VISUAL_HOLD_HEAD_S
            ):
                continue
            contact = action.lane if action.contact is None else action.contact
            suppressed_contacts.add(contact)
            partner = state._hold_chord_partner.pop(contact, None)
            if partner is not None:
                state._hold_chord_partner.pop(partner, None)
            if contact in state._active_hold_tail:
                holds._discard_undispatched_hold(contact)
            diagnostic_event = (
                "chart_early_hold_suppressed"
                if early_by is not None
                and early_by > MAX_EARLY_VISUAL_HOLD_HEAD_S
                else "chart_unmatched_hold_suppressed"
            )
            state.record_diagnostic(
                diagnostic_event,
                now,
                lane=action.lane,
                contact=contact,
                reason=action.reason,
                song_time_s=round(song_time, 3),
                chart_head_s=(
                    round(nearest.time_s, 3)
                    if nearest is not None and nearest.kind == "hold-head"
                    else None
                ),
                early_ms=(
                    round(early_by * 1000)
                    if early_by is not None else None
                ),
            )
        if not suppressed_contacts:
            return actions
        return [
            action
            for action in actions
            if not (
                (
                    action.lane if action.contact is None else action.contact
                ) in suppressed_contacts
                and (
                    action.kind in {
                        ActionKind.DOWN,
                        ActionKind.MOVE,
                        ActionKind.UP,
                    }
                    or (
                        action.kind == ActionKind.FLICK
                        and action.contact is not None
                    )
                )
            )
        ]

    def _fall_back_to_visual(
        self,
        now: float,
        actions: list[TouchAction],
        state: PlannerState,
        holds: HoldPipeline,
    ) -> None:
        """Remove chart-only state when the closed loop loses confidence."""
        for contact in list(state._blind_hold_contacts):
            if contact not in state._active_hold_tail:
                continue
            lane = state._active_hold_lane.get(contact, contact)
            partner = state._hold_chord_partner.pop(contact, None)
            if partner is not None:
                state._hold_chord_partner.pop(partner, None)
            state._hold_tail_flick.discard(contact)
            state._hold_tail_flick_direction.pop(contact, None)
            holds._release_hold(
                contact,
                lane,
                now,
                "chart-disabled",
                actions,
            )
        self.expected_hold_tail.clear()
        state._blind_hold_contacts.clear()
        state._chart_tail_lane.clear()
        state._chart_hold_lanes.clear()
        state._chart_slide_path.clear()
        state._chart_slide_next_index.clear()
        state._chart_hold_release_at.clear()

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
        matched = []
        for engine_time, lane, kind, _ in self.calibration_samples:
            judgement = self.chart.judgement_near(
                lane, engine_time + best_offset, window_s=0.12,
            )
            if judgement is not None and judgement.kind == kind:
                matched.append((judgement.time_s - engine_time, lane))
        residual_mad = (
            statistics.median(
                abs(offset - statistics.median(item[0] for item in matched))
                for offset, _lane in matched
            )
            if matched else float("inf")
        )
        if (
            best_count < required_matches
            or len({lane for _offset, lane in matched}) < 2
            or residual_mad > 0.020
        ):
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
        """Dispatch every due chart judgement once, in lane/chord batches."""
        song_now = self._relative(now) + self.song_offset_s
        occupied_lanes = set(state._active_hold_lane.values())
        for lane in range(self.chart.LANE_COUNT):
            next_judgement = next((
                judgement
                for judgement in self.chart._by_lane[lane]
                if judgement.time_s >= song_now - 0.12
                and judgement.kind in {"tap", "hold-head"}
                and (judgement.note_index, judgement.kind)
                not in self._consumed_judgements
            ), None)
            if next_judgement is None:
                continue
            target = next_judgement.time_s + self.press_bias_s
            lead = target - song_now
            if not -0.12 <= lead <= 0.040:
                continue
            # Only skip when a recent press on this lane already covered the
            # chart note.  Junk presses far from the chart time (or from a
            # different note) must not silence the prediction.
            expected_kind = (
                ActionKind.DOWN
                if next_judgement.kind == "hold-head"
                else (
                    ActionKind.FLICK
                    if next_judgement.flick else ActionKind.TAP
                )
            )
            judgement_key = (
                next_judgement.note_index,
                next_judgement.kind,
            )
            last_timestamp = state._last_trigger.get(lane)
            last_kind = state._last_trigger_action_kind.get(lane)
            last_chart_judgement = self._last_predicted_judgement.get(lane)
            last_trigger_is_chart_owned = (
                last_timestamp is not None
                and last_chart_judgement is not None
                and last_timestamp == last_chart_judgement[2]
            )
            # A chart-owned press has already consumed its exact judgement.
            # Never let its broad visual-coverage window consume the next
            # same-lane judgement (Happy Synthesizer has 118 ms repeats).
            covered_by_last_trigger = (
                last_timestamp is not None
                and last_kind == expected_kind
                and (
                    not last_trigger_is_chart_owned
                    or last_chart_judgement[:2] == judgement_key
                )
                and abs(
                    (self._relative(last_timestamp) + self.song_offset_s)
                    - target
                ) <= 0.12
            )
            covered_by_current_action = any(
                action.kind == expected_kind
                and action.lane == lane
                and abs(
                    (self._relative(action.timestamp) + self.song_offset_s)
                    - target
                ) <= 0.12
                for action in actions
            )
            covered = covered_by_last_trigger or covered_by_current_action
            if covered:
                self._consumed_judgements.add(judgement_key)
                state.record_diagnostic(
                    "chart_visual_press_covered",
                    now,
                    lane=lane,
                    chart_time_s=round(next_judgement.time_s, 3),
                    source=(
                        "current-action"
                        if covered_by_current_action else "last-trigger"
                    ),
                )
                continue
            if next_judgement.kind == "tap":
                if lane in occupied_lanes:
                    continue
                action_kind = (
                    ActionKind.FLICK
                    if next_judgement.flick else ActionKind.TAP
                )
                actions.append(TouchAction(
                    action_kind,
                    lane,
                    now + lead if lead > 0 else now,
                    reason="chart-predicted",
                    flick_direction=next_judgement.direction,
                ))
                self.predicted_presses += 1
                self._last_predicted[lane] = now
                self._last_predicted_judgement[lane] = (
                    next_judgement.note_index,
                    next_judgement.kind,
                    now,
                )
                state._last_trigger[lane] = now
                state._last_trigger_action_kind[lane] = action_kind
                state.record_diagnostic(
                    "chart_predicted_press",
                    now,
                    lane=lane,
                    chart_time_s=round(next_judgement.time_s, 3),
                    lead_ms=round(lead * 1000, 1),
                    flick=next_judgement.flick,
                    rescue=True,
                )
                self._mistimed_lanes.pop(lane, None)
                self._consumed_judgements.add((
                    next_judgement.note_index, next_judgement.kind,
                ))
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
                    press_at=now + lead if lead > 0 else now,
                )
                if next_judgement.note_index in self._claimed_hold_note_indices:
                    self._consumed_judgements.add((
                        next_judgement.note_index, next_judgement.kind,
                    ))

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
        press_at: float | None = None,
    ) -> None:
        """Press a hold head at the chart time when the detector cannot.

        The detector can lose hold heads in dense slide sections (green-body
        occlusion or a drifted hold occupying the lane).  A visible high
        confidence body on the lane is strong evidence, so the chart presses
        the head and registers the hold in the planner state to prevent a
        second start.  A lane occupied by a hold whose chart tail has already
        passed is force-released first.  For straight holds whose green body
        is fully occluded, the fixed chart is authoritative: the head is
        pressed without a sighting and released on the same lane at the tail
        time.  A slide without a visible body follows its exact bundled path.
        """
        if judgement.note_index in self._claimed_hold_note_indices:
            return
        # A due chart head may overlap another slide's original contact id or
        # even a connection lane that its finger is crossing.  Multi-touch
        # permits both contacts at that coordinate; only contact-id reuse is
        # forbidden, and the allocator below handles that case.
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
            tail = self.chart.hold_tail_for_head(judgement)
            if tail is None:
                return
            last_tap = state._last_trigger.get(lane, float("-inf"))
            if now - last_tap < 0.06:
                # A same-instant transient already covered this judgement;
                # do not turn it into an overlapping persistent contact.
                return
            head_lane = judgement.lane
            body = ObservedNote(
                NoteKind.HOLD,
                head_lane,
                lane_center_x(head_lane, self.judgement_y),
                self.judgement_y - 60,
                1,
                1,
                now,
            )
        blind = body.width == 1 and body.height == 1
        tail = self.chart.hold_tail_for_head(judgement)
        path = self.chart.hold_path_for_head(judgement)
        contact = lane
        if contact in state._active_hold_tail:
            # High-density charts can start a second slide on the same lane
            # after the earlier slide's finger has moved away.  Its original
            # planned contact id remains lane-keyed, so allocate another free
            # persistent id instead of dropping the new head.
            contact = next(
                (
                    candidate
                    for candidate in (*range(7, 10), *range(7))
                    if candidate not in state._active_hold_tail
                ),
                -1,
            )
            if contact < 0:
                return
        state._active_hold_tail[contact] = body.y - body.height / 2
        state._hold_started[contact] = now
        state._hold_confirmed.add(contact)
        state._active_hold_lane[contact] = lane
        state._active_hold_x[contact] = float(body.x)
        chart_hold_lanes = {lane}
        if tail is not None:
            chart_hold_lanes.add(tail.lane)
        if path is not None:
            chart_hold_lanes.update(point.lane for point in path.points)
        state._chart_hold_lanes[contact] = frozenset(chart_hold_lanes)
        if blind:
            state._blind_hold_contacts.add(contact)
            if path is not None and any(
                point.lane != lane for point in path.points
            ):
                state._chart_slide_path[contact] = tuple(
                    (point.time_s, point.lane) for point in path.points
                )
                state._chart_slide_next_index[contact] = 1
        if tail is not None and tail.tail_flick:
            state._hold_tail_flick.add(contact)
            if tail.direction is not None:
                state._hold_tail_flick_direction[contact] = tail.direction
        actions.append(TouchAction(
            ActionKind.DOWN,
            lane,
            now if press_at is None else press_at,
            contact,
            "chart-predicted",
            target_x=max(120, min(1160, round(body.x))),
        ))
        self.predicted_presses += 1
        self._last_predicted[lane] = now
        self._last_predicted_judgement[lane] = (
            judgement.note_index,
            judgement.kind,
            now,
        )
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
                current_tail = self.chart.hold_tail_for_head(judgement)
                if (
                    current_tail is not None
                    and abs(expected[0] - current_tail.time_s) <= 0.000001
                    and expected[1] == current_tail.lane
                ):
                    # ``next_judgement`` still returns the just-consumed head
                    # during its rescue window.  This contact owns that exact
                    # head, so it is not an obstruction to free.  This matters
                    # for 75 ms Expert holds, whose tail is already inside the
                    # generic lane-free lead on the following analysis frame.
                    continue
                if expected[1] != active_lane:
                    # This is not a phantom lane occupant: a real cross-lane
                    # slide still has to move from this head lane to a tail on
                    # another lane.  A simultaneous new head can use another
                    # contact; releasing the old finger here drops its tail
                    # judgement (and its flick, when present).
                    continue
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
        state: PlannerState,
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
                window_s=HOLD_HEAD_CLAIM_WINDOW_S,
            )
            if head is None or head.kind != "hold-head":
                continue
            self._claimed_hold_note_indices.add(head.note_index)
            head_song_time = (
                self._relative(action.timestamp) + self.song_offset_s
            )
            state.record_diagnostic(
                "chart_hold_claimed",
                action.timestamp,
                contact=action.contact,
                lane=action.lane,
                note_index=head.note_index,
                residual_ms=round(
                    (head_song_time - head.time_s) * 1000.0,
                ),
            )
            tail = self.chart.hold_tail_for_head(head)
            if tail is not None:
                path = self.chart.hold_path_for_head(head)
                chart_hold_lanes = {head.lane, tail.lane}
                if path is not None:
                    chart_hold_lanes.update(point.lane for point in path.points)
                state._chart_hold_lanes[action.contact] = frozenset(
                    chart_hold_lanes
                )
                self.expected_hold_tail[action.contact] = (
                    tail.time_s,
                    tail.lane,
                )
                state._chart_tail_lane[action.contact] = tail.lane
                state._chart_hold_release_at[action.contact] = (
                    action.timestamp + tail.time_s - head_song_time
                )
                if path is not None and len(path.points) > 2:
                    state._chart_slide_path[action.contact] = tuple(
                        (point.time_s, point.lane) for point in path.points
                    )
                    state._chart_slide_next_index[action.contact] = 1
                if tail.tail_flick:
                    # Slide tails must end with a swipe.  The fixed chart is
                    # authoritative, so do not depend on the detector
                    # spotting the pink arrow on the tail ring.
                    state._hold_tail_flick.add(action.contact)
                    if tail.direction is not None:
                        state._hold_tail_flick_direction[action.contact] = tail.direction

    def _retire_visually_released_holds(
        self,
        actions: list[TouchAction],
        state: PlannerState,
    ) -> None:
        """Synchronise chart tail ownership after the visual hold pipeline.

        Visual processing runs before chart prediction.  It can release an
        old contact and the chart can legitimately reuse that contact for a
        new head later in the same frame.  Retire the old expected tail first;
        otherwise ``_schedule_hold_tails`` mistakes the new DOWN for the old
        hold and ``_release_due_holds`` immediately lifts the new contact.
        """
        for action in actions:
            if (
                action.kind not in {ActionKind.UP, ActionKind.FLICK}
                or action.contact is None
            ):
                continue
            expected = self.expected_hold_tail.pop(action.contact, None)
            if expected is None:
                continue
            state.record_diagnostic(
                "chart_visual_tail_retired",
                action.timestamp,
                contact=action.contact,
                release_reason=action.reason,
                chart_time_s=round(expected[0], 3),
                tail_lane=expected[1],
            )

    def _release_due_holds(
        self,
        now: float,
        actions: list[TouchAction],
        state: PlannerState,
        holds: HoldPipeline,
    ) -> None:
        song_now = self.input_song_time(now)
        for contact, (tail_time, tail_lane) in list(
            self.expected_hold_tail.items()
        ):
            if contact not in state._active_hold_tail:
                self.expected_hold_tail.pop(contact, None)
                state._blind_hold_contacts.discard(contact)
                state._chart_tail_lane.pop(contact, None)
                state._chart_hold_lanes.pop(contact, None)
                state._chart_slide_path.pop(contact, None)
                state._chart_slide_next_index.pop(contact, None)
                state._chart_hold_release_at.pop(contact, None)
                continue
            if song_now < tail_time - 0.015:
                continue
            lane = state._active_hold_lane.get(contact, contact)
            if lane != tail_lane:
                if contact in state._blind_hold_contacts:
                    # A chart-pressed straight hold: the finger stayed on the
                    # head lane and the release position does not matter.
                    pass
                elif (
                    (anchor := _adjacent_zigzag_anchor(
                        state._chart_slide_path.get(contact, ()),
                    ))
                    is not None
                    and tail_lane in anchor
                ):
                    # The fixed midpoint lies inside both adjacent-lane
                    # judgement windows, including the tail judgement.
                    pass
                elif song_now >= tail_time - 0.25:
                    # The slide body vanished before the finger reached the
                    # tail lane.  The fixed chart knows the final lane, so
                    # move the finger there and release in the same frame.
                    previous_lane = lane
                    target_x = lane_center_x(tail_lane, self.judgement_y)
                    actions.append(TouchAction(
                        ActionKind.MOVE,
                        tail_lane,
                        now,
                        contact,
                        "chart-tail-move",
                        target_x=round(target_x),
                    ))
                    state._active_hold_lane[contact] = tail_lane
                    state._active_hold_x[contact] = float(target_x)
                    lane = tail_lane
                    state.record_diagnostic(
                        "chart_tail_move",
                        now,
                        contact=contact,
                        previous_lane=previous_lane,
                        tail_lane=tail_lane,
                        chart_time_s=round(tail_time, 3),
                    )
                else:
                    continue
            partner = state._hold_chord_partner.get(contact)
            if partner is not None:
                partner_expected = self.expected_hold_tail.get(partner)
                if partner_expected is not None:
                    # Each contact has its own chart tail (possibly on a
                    # different lane, e.g. a simultaneous slide pair).  The
                    # generic paired release lifts the partner on the lane it
                    # happens to be on, dropping that tail judgement.  Unlink
                    # so this frame's own loop releases each contact at its
                    # chart tail and final lane.
                    state._hold_chord_partner.pop(contact, None)
                    state._hold_chord_partner.pop(partner, None)
            holds._release_hold(
                contact,
                lane,
                now,
                "chart-tail",
                actions,
            )
            self.predicted_releases += 1
            self.expected_hold_tail.pop(contact, None)
            state._blind_hold_contacts.discard(contact)
            state._chart_tail_lane.pop(contact, None)
            state._chart_slide_path.pop(contact, None)
            state._chart_slide_next_index.pop(contact, None)
            state._chart_hold_release_at.pop(contact, None)
            state.record_diagnostic(
                "chart_predicted_release",
                now,
                contact=contact,
                lane=lane,
                chart_time_s=round(tail_time, 3),
            )

    def _drive_chart_slides(
        self,
        now: float,
        actions: list[TouchAction],
        state: PlannerState,
    ) -> None:
        """Keep matched holds inside each chart connection judgement window.

        Visual green-body segmentation remains useful between nodes, but it
        is not authoritative at the connection itself.  One-lane sawtooth
        paths share a stable midpoint because adjacent-lane judgement covers
        both node lanes; wider paths still restore the exact chart lane.  This
        also drives fully blind slides.
        """
        if not state._chart_slide_path:
            return
        song_now = self.input_song_time(now)
        for contact, path in list(state._chart_slide_path.items()):
            if contact not in state._active_hold_tail:
                state._chart_slide_path.pop(contact, None)
                state._chart_slide_next_index.pop(contact, None)
                continue
            if len(path) < 2 or path[-1][0] <= path[0][0]:
                continue
            adjacent_anchor = _adjacent_zigzag_anchor(path)
            if adjacent_anchor is not None:
                lower_lane, upper_lane = adjacent_anchor
                target_x = (
                    lane_center_x(lower_lane, self.judgement_y)
                    + lane_center_x(upper_lane, self.judgement_y)
                ) / 2.0
                previous_lane = state._active_hold_lane.get(contact, contact)
                previous_x = state._active_hold_x.get(
                    contact,
                    lane_center_x(previous_lane, self.judgement_y),
                )
                if abs(previous_x - target_x) < 18:
                    continue
                target_lane = (
                    previous_lane
                    if previous_lane in adjacent_anchor
                    else lower_lane
                )
                actions.append(TouchAction(
                    ActionKind.MOVE,
                    target_lane,
                    now,
                    contact,
                    "chart-slide-move",
                    target_x=round(target_x),
                ))
                state._active_hold_lane[contact] = target_lane
                state._active_hold_x[contact] = float(target_x)
                state.record_diagnostic(
                    "chart_slide_move",
                    now,
                    contact=contact,
                    previous_lane=previous_lane,
                    target_lane=target_lane,
                    target_x=round(target_x),
                    strategy="adjacent-lane-anchor",
                    anchor_lanes=list(adjacent_anchor),
                )
                continue
            if contact in state._blind_hold_contacts:
                # A fully blind slide has no visual body between counted
                # nodes, so retain continuous segment interpolation for it.
                segment_index = len(path) - 2
                for index in range(len(path) - 1):
                    if song_now <= path[index + 1][0]:
                        segment_index = index
                        break
                start_time, start_lane = path[segment_index]
                end_time, end_lane = path[segment_index + 1]
                duration = max(0.000001, end_time - start_time)
                progress = min(
                    1.0,
                    max(0.0, (song_now - start_time) / duration),
                )
                target_lane = round(
                    start_lane + (end_lane - start_lane) * progress
                )
                previous_lane = state._active_hold_lane.get(contact, contact)
                if target_lane == previous_lane:
                    continue
                target_x = lane_center_x(target_lane, self.judgement_y)
                actions.append(TouchAction(
                    ActionKind.MOVE,
                    target_lane,
                    now,
                    contact,
                    "chart-slide-move",
                    target_x=round(target_x),
                ))
                state._active_hold_lane[contact] = target_lane
                state._active_hold_x[contact] = float(target_x)
                state.record_diagnostic(
                    "chart_slide_move",
                    now,
                    contact=contact,
                    previous_lane=previous_lane,
                    target_lane=target_lane,
                    segment_index=segment_index,
                    progress=round(progress, 3),
                )
                continue
            point_index = state._chart_slide_next_index.get(contact, 1)
            while (
                point_index < len(path) - 1
                and song_now > path[point_index][0] + 0.03
            ):
                point_index += 1
            state._chart_slide_next_index[contact] = point_index
            if point_index >= len(path):
                continue
            point_time, point_lane = path[point_index]
            if not point_time - 0.04 <= song_now <= point_time + 0.03:
                continue
            target_lane = round(point_lane)
            target_x = lane_center_x(target_lane, self.judgement_y)
            previous_lane = state._active_hold_lane.get(contact, contact)
            previous_x = state._active_hold_x.get(contact, target_x)
            if (
                previous_lane == target_lane
                and abs(previous_x - target_x) < 18
            ):
                continue
            actions.append(TouchAction(
                ActionKind.MOVE,
                target_lane,
                now,
                contact,
                "chart-slide-move",
                target_x=round(target_x),
            ))
            state._active_hold_lane[contact] = target_lane
            state._active_hold_x[contact] = float(target_x)
            state.record_diagnostic(
                "chart_slide_move",
                now,
                contact=contact,
                previous_lane=previous_lane,
                target_lane=target_lane,
                point_index=point_index,
                chart_time_s=round(point_time, 3),
            )

    def update(
        self,
        notes: list[ObservedNote],
        tracked_notes: list[TrackedNote],
        now: float,
        actions: list[TouchAction],
        state: PlannerState,
        holds: HoldPipeline,
        *,
        visual_observed: bool = False,
    ) -> list[TouchAction]:
        """Return ``actions`` plus any chart-predicted inputs."""
        self._relative(now)
        self.observe_tracks(tracked_notes, now)
        if self._pending_opening_semantic_lock is not None:
            state.record_diagnostic(
                "chart_opening_semantic_lock",
                now,
                **self._pending_opening_semantic_lock,
            )
            self._pending_opening_semantic_lock = None
        if self._pending_phase_relock is not None:
            state.record_diagnostic(
                "chart_phase_relocked",
                now,
                **self._pending_phase_relock,
            )
            self._pending_phase_relock = None
        if not visual_observed:
            self.observe_visual_actions(actions)
        if self.disabled_for_run:
            self._fall_back_to_visual(now, actions, state, holds)
            if not self._disabled_diagnosed:
                state.record_diagnostic(
                    "chart_disabled_for_run",
                    now,
                    reason=self.disable_reason or "unknown mismatch",
                )
                self._disabled_diagnosed = True
            return actions
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
        self._retire_visually_released_holds(actions, state)
        # Free lanes for due hold heads regardless of press prediction: the
        # normal hold pipeline can then start the real head next frame.
        song_now = self.input_song_time(now)
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
        self._schedule_hold_tails(actions, state)
        self._drive_chart_slides(now, actions, state)
        self._release_due_holds(now, actions, state, holds)
        return actions
