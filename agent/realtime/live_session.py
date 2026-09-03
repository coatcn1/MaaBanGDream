from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import uuid4

from .song_identity import UNKNOWN_SONG_ID


@dataclass(frozen=True, slots=True)
class LiveRunContext:
    run_id: str
    started_at: datetime
    mode: str
    difficulty: str
    profile_name: str | None = None
    song_id: str = UNKNOWN_SONG_ID
    song_id_method: str = "unknown"
    song_level: int | None = None
    song_title: str | None = None
    song_title_confidence: float | None = None
    expected_note_speed: float | None = None
    actual_note_speed: float | None = None
    note_skin_type: int | None = None
    tap_effect: int | None = None
    judgement_assist: bool | None = None
    debug_recording: bool = False
    recording_path: str | None = None
    final_cover_confirmed: bool = False
    final_cover_song_id: str | None = None
    final_cover_status: str = "not-observed"
    final_cover_reason: str | None = None
    # Internal one-shot handoff from a verified difficulty screen to Play.
    # Deliberately omitted from serialized session metadata.
    prepared_for_play: bool = False

    def to_mapping(self) -> dict:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat().replace("+00:00", "Z"),
            "mode": self.mode,
            "difficulty": self.difficulty,
            "profile_name": self.profile_name,
            "song_id": self.song_id,
            "song_id_method": self.song_id_method,
            "song_level": self.song_level,
            "song_title": self.song_title,
            "song_title_confidence": self.song_title_confidence,
            "settings": {
                "expected_note_speed": self.expected_note_speed,
                "actual_note_speed": self.actual_note_speed,
                "note_skin_type": self.note_skin_type,
                "tap_effect": self.tap_effect,
                "judgement_assist": self.judgement_assist,
            },
            "debug_recording": self.debug_recording,
            "recording_path": self.recording_path,
            "final_cover": {
                "confirmed": self.final_cover_confirmed,
                "song_id": self.final_cover_song_id,
                "status": self.final_cover_status,
                "reason": self.final_cover_reason,
            },
        }


_LOCK = RLock()
_CURRENT_LIVE_RUN: LiveRunContext | None = None


def reset_live_run(
    *,
    mode: str,
    difficulty: str,
    profile_name: str | None = None,
    expected_note_speed: float | None = None,
    actual_note_speed: float | None = None,
    note_skin_type: int | None = None,
    tap_effect: int | None = None,
    judgement_assist: bool | None = None,
    debug_recording: bool = False,
    prepared_for_play: bool = False,
) -> LiveRunContext:
    """Start a fresh round and discard all identity from the prior round."""
    global _CURRENT_LIVE_RUN
    current = LiveRunContext(
        run_id=str(uuid4()),
        started_at=datetime.now(timezone.utc),
        mode=str(mode),
        difficulty=str(difficulty),
        profile_name=profile_name,
        expected_note_speed=expected_note_speed,
        actual_note_speed=actual_note_speed,
        note_skin_type=note_skin_type,
        tap_effect=tap_effect,
        judgement_assist=judgement_assist,
        debug_recording=bool(debug_recording),
        prepared_for_play=bool(prepared_for_play),
    )
    with _LOCK:
        _CURRENT_LIVE_RUN = current
    return current


def current_live_run() -> LiveRunContext | None:
    with _LOCK:
        return _CURRENT_LIVE_RUN


def update_live_run(**changes) -> LiveRunContext:
    """Atomically replace fields on the current immutable round context."""
    global _CURRENT_LIVE_RUN
    with _LOCK:
        if _CURRENT_LIVE_RUN is None:
            raise RuntimeError("live run has not been reset for this round")
        _CURRENT_LIVE_RUN = replace(_CURRENT_LIVE_RUN, **changes)
        return _CURRENT_LIVE_RUN


def current_song_id() -> str:
    current = current_live_run()
    return UNKNOWN_SONG_ID if current is None else current.song_id


def append_current_run_event(
    project_root: Path,
    phase: str,
    status: str,
    *,
    details: dict[str, object] | None = None,
) -> Path | None:
    """把外层恢复、重试等决定关联到刚结束的演奏证据包。"""
    current = current_live_run()
    if current is None or not current.recording_path:
        return None
    output_dir = Path(current.recording_path)
    if not output_dir.is_absolute():
        output_dir = Path(project_root) / output_dir
    from .debug_recorder import append_lifecycle_event

    return append_lifecycle_event(
        output_dir,
        phase,
        status,
        details=details,
    )
