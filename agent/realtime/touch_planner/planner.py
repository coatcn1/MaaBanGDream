from __future__ import annotations

from .actions import ActionKind, TouchAction
from .state import PlannerConfig, PlannerState
from .geometry import lane_center_x, touch_x, trusted_crossing_track
from .holds import HoldPipeline
from .ordinary import OrdinaryPipeline
from ..note_detector import NoteKind, ObservedNote
from ..note_tracker import TrackedNote


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
        self._holds = HoldPipeline(self._config, self._state)
        self._ordinary = OrdinaryPipeline(self._config, self._state)

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
        self._ordinary.reset_tracker()
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
