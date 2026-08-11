from __future__ import annotations

from .actions import ActionKind, TouchAction
from .state import PlannerConfig, PlannerState
from .holds import HoldPipeline
from .ordinary import OrdinaryPipeline
from .suppression import JudgementSuppressor
from ..note_detector import ObservedNote
from ..chart_timeline import ChartTimeline
from ..chart_predictor import ChartPredictor


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
        flick_residue_suppress_seconds: float = 0.45,
        chart_timeline: ChartTimeline | None = None,
        chart_prediction: bool = False,
        chart_predict_presses: bool = False,
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
            flick_residue_suppress_seconds=flick_residue_suppress_seconds,
        )
        self._state = PlannerState(timing_offset_ms=timing_offset_ms)
        self._holds = HoldPipeline(self._config, self._state)
        self._ordinary = OrdinaryPipeline(self._config, self._state)
        self._suppression = JudgementSuppressor(self._config, self._state)
        self._chart_predictor = (
            ChartPredictor(
                chart_timeline,
                judgement_y=float(judgement_y),
                predict_presses=chart_predict_presses,
            )
            if chart_timeline is not None and chart_prediction
            else None
        )
        if self._chart_predictor is not None:
            self._ordinary._chart_gate = self._chart_predictor

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
        tracked_notes = self._ordinary.update_tracker(notes, now)
        selected_by_lane = self._holds.process_frame(notes, now, actions)
        stale_tracked_notes = self._ordinary.process_tracks(
            tracked_notes, now, actions
        )
        self._holds.finish_frame(selected_by_lane, now, actions)
        self._ordinary.finish_frame(notes, now, actions)
        actions = self._suppression.filter(
            actions,
            now,
            {
                tracked.track_id: tracked
                for tracked in tracked_notes + stale_tracked_notes
            },
        )
        if self._chart_predictor is not None:
            if self._chart_predictor.predict_presses:
                # Any transient press far from the chart time is a mistimed
                # junk/rescue/dropout input: drop it and let the chart press
                # the note at the correct moment.  Structural hold actions
                # (DOWN/UP/MOVE) are handled by the hold pipelines.
                actions = [
                    action
                    for action in actions
                    if not (
                        action.kind in (ActionKind.TAP, ActionKind.FLICK)
                        and action.contact is None
                        and not self._chart_predictor.validate_crossing(
                            action.lane, action.timestamp
                        )
                    )
                ]
            actions = self._chart_predictor.update(
                notes,
                tracked_notes,
                now,
                actions,
                self._state,
                self._holds,
            )
        return actions

    def reset(self, now: float) -> list[TouchAction]:
        actions = []
        for contact in sorted(self._state._active_hold_tail):
            lane = self._state._active_hold_lane.get(contact, contact)
            actions.append(TouchAction(
                ActionKind.UP, lane, now, contact, "engine-reset"
            ))
        self._state.reset()
        self._ordinary.reset_tracker()
        if self._chart_predictor is not None:
            self._chart_predictor.reset()
        return actions

    # Backward-compatible accessors for tests and the engine.
    @property
    def judgement_y(self) -> float:
        return self._config.judgement_y

    @property
    def timing_offset(self) -> float:
        return self._state.timing_offset

    @property
    def retrigger_seconds(self) -> float:
        return self._config.retrigger_seconds

    @property
    def hold_grace_seconds(self) -> float:
        return self._config.hold_grace_seconds

    @property
    def track_memory_seconds(self) -> float:
        return self._config.track_memory_seconds

    @property
    def enable_slide(self) -> bool:
        return self._config.enable_slide

    @property
    def rescue_first_visible(self) -> bool:
        return self._config.rescue_first_visible

    @property
    def lane_sweep_interval(self) -> float | None:
        return self._config.lane_sweep_interval

    @property
    def hold_release_y(self) -> float:
        return self._config.hold_release_y

    @property
    def paired_hold_rescue_margin(self) -> float:
        return self._config.paired_hold_rescue_margin

    @property
    def hold_max_seconds(self) -> float:
        return self._config.hold_max_seconds

    @property
    def hold_restart_cooldown_seconds(self) -> float:
        return self._config.hold_restart_cooldown_seconds

    @property
    def post_release_rescue_seconds(self) -> float:
        return self._config.post_release_rescue_seconds

    @property
    def hold_start_suppress_seconds(self) -> float:
        return self._config.hold_start_suppress_seconds

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
    def chart_predicted_presses(self) -> int:
        return (
            self._chart_predictor.predicted_presses
            if self._chart_predictor is not None else 0
        )

    @property
    def chart_predicted_releases(self) -> int:
        return (
            self._chart_predictor.predicted_releases
            if self._chart_predictor is not None else 0
        )

    @property
    def chart_calibrated(self) -> bool:
        return (
            self._chart_predictor.calibrated
            if self._chart_predictor is not None else False
        )

    @property
    def filtered_adjacent_artifacts(self) -> int:
        return self._state.filtered_adjacent_artifacts

    @property
    def rejected_hold_candidates(self) -> int:
        return self._state.rejected_hold_candidates
