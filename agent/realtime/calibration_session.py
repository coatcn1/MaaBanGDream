"""Persistent fixed-stage realtime calibration sessions.

The session file is deliberately separate from the candidate Profile.  The
session is the crash-safe workflow journal; the Profile is the runtime artefact
that remains unaccepted until the single formal validation succeeds.
"""

from __future__ import annotations

import json
import os
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .profile_store import EnvironmentSignature, RealtimeProfileStore


# 局内 FAST/SLOW 自适应控制已能在一首歌内收敛（实测 0→35ms），且结算后
# 会把收敛结果写回 Profile。排练只需要给新环境定一个起始偏移，保留一首
# 排练 + 一首正式验证即可，不再需要三首排练逐曲平均。
REHEARSAL_STAGES = ("rehearsal-1",)
FORMAL_STAGE = "formal-validation"
CALIBRATION_STAGES = (*REHEARSAL_STAGES, FORMAL_STAGE)


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _formal_accepted(result: dict[str, Any]) -> bool:
    return bool(
        result.get("valid") is not False
        and result.get("completed") is True
        and result.get("survived") is True
        and int(result.get("miss", 10)) < 10
    )


class CalibrationSessionStore:
    SCHEMA_VERSION = 1

    def __init__(
        self,
        root: str | Path,
        profiles: RealtimeProfileStore,
    ) -> None:
        self.root = Path(root)
        self.profiles = profiles

    @staticmethod
    def _clean(session: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in session.items() if key != "_path"}

    def _atomic_write(self, path: Path, session: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self._clean(session), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def _save(self, session: dict[str, Any]) -> dict[str, Any]:
        session["updated_at"] = _now()
        path = Path(session["_path"])
        self._atomic_write(path, session)
        return session

    def _load(self, path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            return None
        value["_path"] = path
        return value

    def _sessions(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        sessions = [self._load(path) for path in self.root.glob("*.json")]
        return sorted(
            (item for item in sessions if item is not None),
            key=lambda item: str(item.get("updated_at", "")),
            reverse=True,
        )

    @staticmethod
    def _compatible(
        session: dict[str, Any],
        *,
        difficulty: str,
        song_mode: str,
        environment: EnvironmentSignature,
        current_song_id: str | None,
    ) -> bool:
        if session.get("difficulty") != difficulty:
            return False
        if session.get("song_mode") != song_mode:
            return False
        if session.get("environment") != environment.to_mapping():
            return False
        if song_mode == "current":
            before = session.get("current_song_id")
            if before and current_song_id and before != current_song_id:
                return False
        return True

    def start(
        self,
        *,
        difficulty: str,
        song_mode: str,
        environment: EnvironmentSignature,
        initial_offset_ms: int,
        current_song_id: str | None = None,
        resume_mode: str = "auto",
    ) -> dict[str, Any]:
        if song_mode not in {"current", "random"}:
            raise ValueError(f"不支持的校准歌曲模式: {song_mode}")
        if resume_mode not in {"auto", "restart"}:
            raise ValueError(f"不支持的校准进度模式: {resume_mode}")
        environment.validate()
        compatible = [
            session for session in self._sessions()
            if session.get("status") in {"active", "paused"}
            and self._compatible(
                session,
                difficulty=difficulty,
                song_mode=song_mode,
                environment=environment,
                current_song_id=current_song_id,
            )
        ]
        if compatible and resume_mode == "auto":
            session = compatible[0]
            attempts = session.setdefault("attempts", [])
            if attempts and attempts[-1].get("status") == "running":
                attempts[-1].update({
                    "status": "interrupted",
                    "ended_at": _now(),
                    "technical_reason": "process ended before round completion",
                })
            session["status"] = "active"
            session["terminal_reason"] = None
            return self._save(session)
        if resume_mode == "restart":
            for old in compatible:
                old["status"] = "superseded"
                old["terminal_reason"] = "user_requested_restart"
                self._save(old)

        created_at = _now()
        profile_path = self.profiles.write({
            "created_at": created_at,
            "difficulty": difficulty,
            "accepted": False,
            "accepted_at": None,
            "environment": environment.to_mapping(),
            "settings": {
                "target_fps": 60,
                "timing_offset_ms": int(initial_offset_ms),
                "frame_timeout_ms": 150,
                "playfield_timeout_ms": 1500,
            },
            "rehearsals": [],
            "formal_attempts": [],
            "formal": None,
        })
        session_id = uuid4().hex
        path = self.root / f"calibration-{session_id}.json"
        session: dict[str, Any] = {
            "schema_version": self.SCHEMA_VERSION,
            "session_id": session_id,
            "created_at": created_at,
            "updated_at": created_at,
            "status": "active",
            "terminal_reason": None,
            "next_stage": REHEARSAL_STAGES[0],
            "difficulty": difficulty,
            "song_mode": song_mode,
            "current_song_id": current_song_id if song_mode == "current" else None,
            "environment": environment.to_mapping(),
            "current_offset_ms": int(initial_offset_ms),
            "candidate_profile": profile_path.name,
            "used_song_ids": [],
            "attempts": [],
            "_path": path,
        }
        return self._save(session)

    def begin_round(
        self,
        session: dict[str, Any],
        stage: str,
        offset_ms: int,
        *,
        report_path: str | None = None,
        recording_path: str | None = None,
    ) -> dict[str, Any]:
        if stage not in CALIBRATION_STAGES:
            raise ValueError(f"未知校准阶段: {stage}")
        if session.get("next_stage") != stage:
            raise RuntimeError(
                f"校准阶段不一致: expected={session.get('next_stage')} actual={stage}"
            )
        attempts = session.setdefault("attempts", [])
        if attempts and attempts[-1].get("status") == "running":
            raise RuntimeError("上一局尚未结束，不能开始新一局")
        attempts.append({
            "attempt": len(attempts) + 1,
            "stage": stage,
            "status": "running",
            "started_at": _now(),
            "ended_at": None,
            "initial_timing_offset_ms": int(offset_ms),
            "report_path": report_path,
            "recording_path": recording_path,
        })
        session["status"] = "active"
        session["terminal_reason"] = None
        return self._save(session)

    def _update_candidate(self, session: dict[str, Any]) -> None:
        profile = self.profiles.load(session["candidate_profile"])
        path = profile.pop("_path")
        completed = [
            attempt for attempt in session.get("attempts", [])
            if attempt.get("status") == "completed"
        ]
        rehearsals = [
            attempt for attempt in completed
            if attempt.get("stage") in REHEARSAL_STAGES
        ]
        formal_attempts = [
            attempt for attempt in session.get("attempts", [])
            if attempt.get("stage") == FORMAL_STAGE
            and attempt.get("status") != "running"
        ]
        formal = formal_attempts[-1] if formal_attempts else None
        accepted = bool(
            session.get("status") == "accepted"
            and formal is not None
            and _formal_accepted(formal.get("result", {}))
        )
        profile.update({
            "accepted": accepted,
            "accepted_at": _now() if accepted else None,
            "modified_at": _now(),
            "settings": {
                **profile.get("settings", {}),
                "timing_offset_ms": int(session.get("current_offset_ms", 0)),
            },
            "rehearsals": rehearsals,
            "formal_attempts": formal_attempts,
            "formal": formal,
            "calibration_session": session.get("session_id"),
            "calibration_status": session.get("status"),
        })
        self.profiles.replace(path.name, profile)

    def finish_round(
        self,
        session: dict[str, Any],
        stage: str,
        result: dict[str, Any],
        *,
        suggested_offset_ms: int,
        termination_reason: str | None = None,
    ) -> dict[str, Any]:
        attempts = session.setdefault("attempts", [])
        if not attempts or attempts[-1].get("status") != "running":
            raise RuntimeError("没有正在运行的校准局")
        attempt = attempts[-1]
        if attempt.get("stage") != stage:
            raise RuntimeError("结束阶段与正在运行阶段不一致")
        attempt.update({
            "ended_at": _now(),
            "result": dict(result),
            "song_id": result.get("song_id"),
            "suggested_timing_offset_ms": int(suggested_offset_ms),
            "termination_reason": termination_reason,
        })
        if result.get("recording_path"):
            attempt["recording_path"] = result["recording_path"]
        valid = bool(result.get("valid", True))
        completed = bool(result.get("completed", False))
        if not valid or not completed:
            attempt["status"] = "technical-failure"
            attempt["technical_reason"] = (
                termination_reason
                or result.get("technical_reason")
                or "invalid or incomplete result"
            )
            session["status"] = "paused"
            session["terminal_reason"] = attempt["technical_reason"]
            # The failed attempt is retained, but the stage is not consumed.
            session["next_stage"] = stage
            self._save(session)
            self._update_candidate(session)
            return session

        if session.get("song_mode") == "current":
            expected_song = session.get("current_song_id")
            actual_song = result.get("song_id")
            if expected_song and actual_song and expected_song != actual_song:
                attempt["status"] = "technical-failure"
                attempt["technical_reason"] = "current song changed during calibration"
                session["status"] = "paused"
                session["terminal_reason"] = attempt["technical_reason"]
                session["next_stage"] = stage
                self._save(session)
                self._update_candidate(session)
                return session
            if not expected_song and actual_song:
                session["current_song_id"] = actual_song

        attempt["status"] = "completed"
        song_id = result.get("song_id")
        if song_id and song_id not in session.setdefault("used_song_ids", []):
            session["used_song_ids"].append(song_id)
        session["current_offset_ms"] = int(suggested_offset_ms)
        if stage in REHEARSAL_STAGES:
            next_index = REHEARSAL_STAGES.index(stage) + 1
            if next_index < len(REHEARSAL_STAGES):
                session["next_stage"] = REHEARSAL_STAGES[next_index]
            else:
                suggestions = [
                    int(item["suggested_timing_offset_ms"])
                    for item in attempts
                    if item.get("stage") in REHEARSAL_STAGES
                    and item.get("status") == "completed"
                ]
                session["current_offset_ms"] = int(round(statistics.median(suggestions)))
                session["next_stage"] = FORMAL_STAGE
        else:
            accepted = _formal_accepted(result)
            session["status"] = "accepted" if accepted else "rejected"
            session["terminal_reason"] = (
                "formal_validation_passed"
                if accepted else "formal_validation_failed"
            )
            session["next_stage"] = None
        self._save(session)
        self._update_candidate(session)
        return session

    def stop(
        self,
        session: dict[str, Any],
        reason: str = "user_stopped",
    ) -> dict[str, Any]:
        attempts = session.setdefault("attempts", [])
        if attempts and attempts[-1].get("status") == "running":
            attempts[-1].update({
                "status": "interrupted",
                "ended_at": _now(),
                "technical_reason": reason,
            })
        session["status"] = "paused"
        session["terminal_reason"] = reason
        self._save(session)
        self._update_candidate(session)
        return session
