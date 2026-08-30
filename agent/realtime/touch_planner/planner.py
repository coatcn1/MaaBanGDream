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
        self._chart_predictor = (
            ChartPredictor(
                chart_timeline,
                judgement_y=float(judgement_y),
                predict_presses=chart_predict_presses,
                press_bias_ms=timing_offset_ms,
            )
            if chart_timeline is not None and chart_prediction
            else None
        )
        self._ordinary = OrdinaryPipeline(
            self._config,
            self._state,
            chart_gate=self._chart_predictor,
        )
        self._suppression = JudgementSuppressor(self._config, self._state)
        self._chart_input_finished = False

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
        if self._chart_predictor is not None:
            self._chart_predictor.set_press_bias_ms(int(value))

    def _record_diagnostic(self, event: str, now: float, **fields: object) -> None:
        self._state._diagnostics.append({"event": event, "timestamp": now, **fields})

    def drain_diagnostics(self) -> list[dict[str, object]]:
        result = self._state._diagnostics
        self._state._diagnostics = []
        return result

    def update(self, notes: list[ObservedNote], now: float) -> list[TouchAction]:
        actions: list[TouchAction] = []
        if self._chart_input_finished:
            return actions
        if (
            self._chart_predictor is not None
            and self._chart_predictor.input_window_finished(now)
        ):
            # A locked local chart gives an authoritative end to the input
            # window.  The result transition remains visually busy for several
            # seconds and can otherwise be mistaken for a HOLD body, as in the
            # 2026-08-27 formal run (DOWN/MOVE/FLICK 4.8 s after chart end).
            # Release any genuine tail contact, clear visual tracking, then
            # latch input off until the per-song planner is reset.
            for contact in sorted(self._state._active_hold_tail):
                lane = self._state._active_hold_lane.get(contact, contact)
                actions.append(TouchAction(
                    ActionKind.UP,
                    lane,
                    now,
                    contact,
                    "chart-input-finished",
                ))
            self._state.reset()
            self._ordinary.reset_tracker()
            self._chart_input_finished = True
            self._record_diagnostic(
                "chart_input_finished",
                now,
                song_time_s=round(self._chart_predictor.song_time(now), 3),
                chart_end_time_s=round(
                    self._chart_predictor.chart.end_time_s, 3
                ),
            )
            return actions
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
        if self._chart_predictor is not None:
            self._chart_predictor.observe_tracks(tracked_notes, now)
            self._chart_predictor.observe_visual_actions(actions)
            if (
                self._chart_predictor.predict_presses
                and not self._chart_predictor.disabled_for_run
            ):
                # A chart-owned transient must be removed before the ordinary
                # suppression/deduplication pass records it in _last_trigger.
                # Recording first made the chart scheduler believe a removed
                # rescue had actually reached the device, so it consumed the
                # corresponding judgement and emitted no replacement press.
                visual_actions = actions
                actions = []
                for action in visual_actions:
                    chart_owned = (
                        action.kind in (ActionKind.TAP, ActionKind.FLICK)
                        and action.contact is None
                        and not self._chart_predictor.validate_crossing(
                            action.lane, action.timestamp
                        )
                    )
                    if chart_owned:
                        self._record_diagnostic(
                            "chart_mistimed_crossing_suppressed",
                            now,
                            lane=action.lane,
                            track_id=action.track_id,
                            reason=action.reason,
                        )
                        continue
                    actions.append(action)
                actions = self._chart_predictor.filter_chart_owned_holds(
                    actions,
                    now,
                    self._state,
                    self._holds,
                )
        actions = self._suppression.filter(
            actions,
            now,
            {
                tracked.track_id: tracked
                for tracked in tracked_notes + stale_tracked_notes
            },
        )
        if self._chart_predictor is not None:
            actions = self._chart_predictor.apply_chart_flick_semantics(
                actions,
                self._state,
            )
            actions = self._chart_predictor.update(
                notes,
                tracked_notes,
                now,
                actions,
                self._state,
                self._holds,
                visual_observed=True,
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
        self._chart_input_finished = False
        return actions

    def recover_touch_state(self, now: float) -> None:
        """Synchronize planner state after an out-of-band device release.

        Keep visual tracker identities and the calibrated chart clock: both
        remain valid observations of the current song.  Only state which
        claims that a finger is physically down may survive neither the
        controller release nor the next planning frame.
        """
        self._state.reset()
        if self._chart_predictor is not None:
            self._chart_predictor.recover_touch_state()
        self._record_diagnostic("planner_touch_state_recovered", now)

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
    def chart_disabled_for_run(self) -> bool:
        return (
            self._chart_predictor.disabled_for_run
            if self._chart_predictor is not None else False
        )

    @property
    def chart_song_offset_ms(self) -> float | None:
        if self._chart_predictor is None or not self._chart_predictor.calibrated:
            return None
        return round(self._chart_predictor.song_offset_s * 1000, 3)

    @property
    def chart_disable_reason(self) -> str | None:
        return (
            self._chart_predictor.disable_reason
            if self._chart_predictor is not None else None
        )

    @property
    def filtered_adjacent_artifacts(self) -> int:
        return self._state.filtered_adjacent_artifacts

    @property
    def rejected_hold_candidates(self) -> int:
        return self._state.rejected_hold_candidates
