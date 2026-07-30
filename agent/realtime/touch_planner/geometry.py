from __future__ import annotations

from ..note_detector import NoteDetector, ObservedNote
from ..note_tracker import TrackedNote


def lane_center_x(lane: int, y: float) -> float:
    progress = min(1.08, max(0.0, (y - NoteDetector.VANISHING_Y) / (
        NoteDetector.JUDGEMENT_Y - NoteDetector.VANISHING_Y
    )))
    return 640 + (NoteDetector.DEFAULT_LANE_CENTERS[lane] - 640) * progress


def touch_x(note: ObservedNote) -> int:
    return max(120, min(1160, round(note.x)))


def trusted_crossing_track(tracked: TrackedNote, judgement_y: float) -> bool:
    lateral_residual = float("inf")
    if tracked.previous_x is not None and tracked.previous_y is not None:
        previous_residual = (
            tracked.previous_x
            - lane_center_x(tracked.note.lane, tracked.previous_y)
        )
        current_residual = (
            tracked.note.x
            - lane_center_x(tracked.note.lane, tracked.note.y)
        )
        lateral_residual = abs(current_residual - previous_residual)
    return (
        tracked.previous_y is not None
        and tracked.velocity_y > 0
        and (
            tracked.previous_y >= judgement_y - 35
            or (
                tracked.velocity_y >= 350
                and tracked.minimum_y <= judgement_y - 40
                and (
                    lateral_residual <= 40
                    or (
                        tracked.motion_samples >= 3
                        and tracked.downward_motion_frames >= 2
                    )
                )
            )
        )
    )
