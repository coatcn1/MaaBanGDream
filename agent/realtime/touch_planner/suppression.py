from __future__ import annotations

from ..note_tracker import TrackedNote
from .actions import ActionKind, TouchAction
from .geometry import trusted_crossing_track
from .state import PlannerConfig, PlannerState


class JudgementSuppressor:
    """Final gate: merge fragments, keep validated chord partners."""

    def __init__(self, config: PlannerConfig, state: PlannerState) -> None:
        self._config = config
        self._state = state

    def filter(
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
        # 同一帧内已经通过 current_down_lanes 处理；这里补上跨帧场景：
        # hold 起手后，其头部碎片仍可能在下几帧被 ordinary 当成同轨道
        # TAP，若不拦截就会对同一颗长条按两次。
        recent_hold_lanes = {
            lane
            for lane, started in self._state._hold_started_at_by_lane.items()
            if now - started < self._config.hold_start_suppress_seconds
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
            covered_by_recent_hold_start = (
                action.kind in (ActionKind.TAP, ActionKind.FLICK)
                and action.lane in recent_hold_lanes
            )
            if (
                recently_covered
                or covered_by_hold_start
                or covered_by_recent_hold_start
            ):
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

