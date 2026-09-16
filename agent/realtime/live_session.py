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
    requested_difficulty: str | None = None
    profile_name: str | None = None
    song_id: str = UNKNOWN_SONG_ID
    song_id_method: str = "unknown"
    song_level: int | None = None
    song_title: str | None = None
    song_title_confidence: float | None = None
    # 准备页身份缺失或冲突时必须在最终封面重新实读；期间不得复用旧谱面
    # 或提前预武装 Native。实际难度按钮仍由独立门禁确认。
    preparation_title_pending_final_cover: bool = False
    # 准备页缺少可确认的封面候选，需要由最终封面独立解析；与标题缺失
    # 分开记录，以便保留已可信的实际标题作为最终解析约束。
    preparation_identity_pending_final_cover: bool = False
    preparation_identity_pending_reason: str | None = None
    expected_note_speed: float | None = None
    actual_note_speed: float | None = None
    debug_recording: bool = False
    recording_path: str | None = None
    final_cover_confirmed: bool = False
    final_cover_song_id: str | None = None
    final_cover_status: str = "not-observed"
    final_cover_reason: str | None = None
    # 本局生命归零并已请求“断网跳车”；仅作为 Play 与外层协力流程之间的
    # 一次性信号，不进入序列化会话元数据。
    disconnect_jump_requested: bool = False
    # Internal one-shot handoff from a verified difficulty screen to Play.
    # Deliberately omitted from serialized session metadata.
    prepared_for_play: bool = False
    # 保留本局准备页证据，待演奏记录器建立后归入同一个证据包，不序列化像素。
    preparation_identity_image: object | None = None
    # 协力跳过设置页时保留最新准备页截图，供模式确认和准备按钮立即复用；
    # 与身份取证截图分开，避免打开过设置页后误用旧画面。
    cooperative_prestart_image: object | None = None
    # 漏掉短黑场但已确认最终封面时，把同一帧和解析结果交给演奏入口；
    # 这些对象只在本局内存中传递，不进入结果 JSON。
    startup_final_cover_image: object | None = None
    startup_final_cover_resolution: object | None = None

    def to_mapping(self) -> dict:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat().replace("+00:00", "Z"),
            "mode": self.mode,
            "difficulty": self.difficulty,
            "requested_difficulty": (
                self.requested_difficulty or self.difficulty
            ),
            "effective_difficulty": self.difficulty,
            "profile_name": self.profile_name,
            "song_id": self.song_id,
            "song_id_method": self.song_id_method,
            "song_level": self.song_level,
            "song_title": self.song_title,
            "song_title_confidence": self.song_title_confidence,
            "preparation_title_pending_final_cover": (
                self.preparation_title_pending_final_cover
            ),
            "preparation_identity_pending_final_cover": (
                self.preparation_identity_pending_final_cover
            ),
            "preparation_identity_pending_reason": (
                self.preparation_identity_pending_reason
            ),
            "settings": {
                "expected_note_speed": self.expected_note_speed,
                "actual_note_speed": self.actual_note_speed,
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
    requested_difficulty: str | None = None,
    profile_name: str | None = None,
    expected_note_speed: float | None = None,
    actual_note_speed: float | None = None,
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
        requested_difficulty=str(requested_difficulty or difficulty),
        profile_name=profile_name,
        expected_note_speed=expected_note_speed,
        actual_note_speed=actual_note_speed,
        debug_recording=bool(debug_recording),
        prepared_for_play=bool(prepared_for_play),
    )
    with _LOCK:
        _CURRENT_LIVE_RUN = current
    return current


def current_live_run() -> LiveRunContext | None:
    with _LOCK:
        return _CURRENT_LIVE_RUN


def effective_difficulty_for_current_run(requested_difficulty: str) -> str:
    """仅把已确认的 Special→Expert 回退传给本局后续开演节点。"""
    requested = str(requested_difficulty)
    run = current_live_run()
    if run is None or not run.prepared_for_play:
        return requested
    recorded_request = str(
        run.requested_difficulty or run.difficulty
    ).casefold()
    if recorded_request != requested.casefold():
        return requested
    if (
        requested.casefold() == "special"
        and str(run.difficulty).casefold() == "expert"
    ):
        return str(run.difficulty)
    return requested


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
