from __future__ import annotations

from dataclasses import dataclass

from ..note_detector import NoteKind, ObservedNote
from ..note_tracker import TrackedNote
from .actions import ActionKind


@dataclass(frozen=True)
class PlannerConfig:
    judgement_y: float
    retrigger_seconds: float
    hold_grace_seconds: float
    track_memory_seconds: float
    enable_slide: bool
    rescue_first_visible: bool
    lane_sweep_interval: float | None
    hold_release_y: float
    paired_hold_rescue_margin: float
    hold_max_seconds: float
    hold_restart_cooldown_seconds: float
    post_release_rescue_seconds: float
    hold_start_suppress_seconds: float
    flick_residue_suppress_seconds: float


class PlannerState:
    """All mutable planner state; reset() mirrors the original per-song reset."""

    def __init__(self, *, timing_offset_ms: int = 10) -> None:
        self.timing_offset = timing_offset_ms / 1000
        self._last_lane_sweep = float("-inf")
        self._last_update_at: float | None = None
        self._frame_interval_seconds = 1 / 60
        self._previous: dict[tuple[NoteKind, int], ObservedNote] = {}
        self._last_trigger: dict[int, float] = {}
        self._last_trigger_note: dict[int, ObservedNote] = {}
        self._last_trigger_track: dict[int, TrackedNote] = {}
        self._last_trigger_action_kind: dict[int, ActionKind] = {}
        self._last_trigger_reason: dict[int, str] = {}
        self._recent_ordinary: dict[int, list[ObservedNote]] = {}
        self._pending_ordinary_rescue: dict[
            int, tuple[float, NoteKind, int]
        ] = {}
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
        self._blind_hold_contacts: set[int] = set()
        self._chart_tail_lane: dict[int, int] = {}
        self._blind_slide_path: dict[int, tuple[int, int, float, float]] = {}
        self._blind_slide_last_lane: dict[int, int] = {}
        self._hold_last_moved_at: dict[int, float] = {}
        self._diagnostics: list[dict[str, object]] = []
        self.filtered_adjacent_artifacts = 0
        self.rejected_hold_candidates = 0

    def record_diagnostic(self, event: str, now: float, **fields: object) -> None:
        self._diagnostics.append({"event": event, "timestamp": now, **fields})

    def drain_diagnostics(self) -> list[dict[str, object]]:
        result = self._diagnostics
        self._diagnostics = []
        return result

    def reset(self) -> None:
        # timing_offset, counters and the diagnostics queue deliberately
        # survive: the original reset never touched them.
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
        self._blind_hold_contacts.clear()
        self._chart_tail_lane.clear()
        self._blind_slide_path.clear()
        self._blind_slide_last_lane.clear()
        self._hold_last_moved_at.clear()
        self._hold_tail_flick.clear()
        self._previous.clear()
        self._recent_ordinary.clear()
        self._last_trigger_note.clear()
        self._last_trigger_track.clear()
        self._last_trigger_action_kind.clear()
        self._last_trigger_reason.clear()
        self._pending_ordinary_rescue.clear()
        self._last_trigger.clear()
        self._last_lane_sweep = float("-inf")
        self._last_update_at = None
        self._frame_interval_seconds = 1 / 60
