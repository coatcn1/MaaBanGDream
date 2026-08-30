from __future__ import annotations

import json
import statistics
import traceback
from datetime import datetime
from pathlib import Path

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    from ..task_reporting import record_failure_reason
except ImportError:  # AgentServer imports realtime as a top-level package.
    from task_reporting import record_failure_reason

from .profile_action import PROJECT_ROOT
from .profile_store import EnvironmentSignature, RealtimeProfileStore
from .calibration_session import (
    CalibrationSessionStore,
    FORMAL_STAGE,
    REHEARSAL_STAGES,
)
from .rehearsal_action import frame_resolution
from .result_parser import LiveResult, adjusted_timing_offset
from .runtime_options import (
    calibration_difficulty,
    calibration_resume_mode,
    calibration_song_mode,
    debug_enabled,
    diagnostic_trace_enabled,
)
from .difficulty_action import DIFFICULTY_TARGETS
from .game_effect_settings_action import verified_game_visual_settings
from .live_session import current_song_id
from .song_identity import UNKNOWN_SONG_ID


PLAY_NODES = {
    "Easy": "RealtimeLivePlay", "Normal": "RealtimeLivePlayNormal",
    "Hard": "RealtimeLivePlayHard", "Expert": "RealtimeLivePlayExpert",
    "Special": "RealtimeLivePlaySpecial",
}
CALIBRATION_ROUND_ENTRY = "RealtimeCalibrationSingleLive"


def calibration_round_plan(
    *,
    difficulty: str,
    note_speed: float,
    calibration_debug: bool,
    formal: bool,
    play_node: str,
    offset: int,
    report_path: Path,
    song_mode: str = "current",
    excluded_song_ids: list[str] | None = None,
    diagnostic_trace: bool = True,
) -> tuple[dict, dict]:
    """Build the self-contained pipeline override for one calibration round."""
    play_params = {
        "difficulty": difficulty, "require_profile": False,
        "target_fps": 60, "timing_offset_ms": offset,
        "debug_recording": calibration_debug,
        "diagnostic_trace": diagnostic_trace,
        "duration_seconds": 600, "dpi": 240, "game_fps": 60,
        "render_quality": "standard", "note_speed": note_speed,
        "settings_gate_required": True,
        "run_mode": (
            "calibration-formal"
            if formal else "calibration-rehearsal"
        ),
        "wait_for_completion": True, "completion_missing_frames": 120,
        "require_completion": True, "save_result_frame": True,
        "result_back_attempts": 30, "result_back_interval_seconds": 1.5,
        "rehearsal_mode": not formal,
        "calibration_report": str(report_path),
    }
    start_next = [play_node]
    override = {
        "RealtimeLiveFreeLive": {
            "next": ["RealtimeLiveSongSelectMarker"]
        },
        "RealtimeLiveSongSelectMarker": {
            "next": [
                "RealtimeLiveRandomSong"
                if song_mode == "random" else "RealtimeLiveDifficulty"
            ]
        },
        "RealtimeLiveDifficulty": {
            "custom_action_param": {
                "difficulty": difficulty,
                "max_attempts": 3,
                "mode": (
                    "calibration-formal"
                    if formal else "calibration-rehearsal"
                ),
                "note_speed": note_speed,
                "debug_recording": calibration_debug,
            }
        },
        "RealtimeLiveFormalSettingsGate": {
            "custom_action_param": {
                "difficulty": difficulty,
                "require_profile": False,
                "dpi": 240,
                "game_fps": 60,
                "render_quality": "standard",
            }
        },
        "RealtimeLiveRehearsalSettingsGate": {
            "custom_action_param": {
                "difficulty": difficulty,
                "require_profile": False,
                "dpi": 240,
                "game_fps": 60,
                "render_quality": "standard",
            }
        },
        "RealtimeLiveRehearsalStart": {"next": start_next},
        "RealtimeLiveFormalStart": {"next": start_next},
        "RealtimeLiveReturnHome": {
            "next": ["RealtimeCalibrationRoundComplete"]
        },
        play_node: {"custom_action_param": play_params},
    }
    if song_mode == "random":
        override["RealtimeLiveRandomSong"] = {
            "custom_action_param": {
                "max_attempts": 12,
                "preserve_filter": True,
                "excluded_song_ids": list(excluded_song_ids or []),
            }
        }
    if formal:
        override["RealtimeLiveFormalModeGate"] = {
            "next": ["RealtimeLiveRehearsalToFormal", "RealtimeLiveFormalReady"]
        }
    return play_params, override


def result_report_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        path.name: (path.stat().st_mtime_ns, path.stat().st_size)
        for path in root.glob("realtime-result-*.json")
    }


def latest_result_report_since(
    root: Path,
    before: dict[str, tuple[int, int]],
    song_id: str,
) -> dict:
    candidates = sorted(
        root.glob("realtime-result-*.json"),
        key=lambda path: path.name,
        reverse=True,
    )
    for path in candidates:
        signature = (path.stat().st_mtime_ns, path.stat().st_size)
        if before.get(path.name) == signature:
            continue
        result = json.loads(path.read_text(encoding="utf-8"))
        return {**result, "song_id": song_id, "survived": True, "completed": True}
    raise RuntimeError("校准单轮未找到本轮已保存的结算报告")


def result_from_mapping(value: dict) -> LiveResult:
    return LiveResult(**{key: value[key] for key in ("perfect", "great", "good", "bad", "miss", "fast", "slow")}, confidence=float(value.get("confidence", 1)))


def formal_validation_passed(
    result: LiveResult,
    *,
    survived: bool,
    completed: bool,
) -> bool:
    return bool(survived and completed and result.miss < 10)


class CalibrationRunner:
    """Pure fixed 3+1 runner used by unit tests and non-persistent callers."""

    def __init__(self, run_round, *, max_attempts: int | None = None):
        self.run_round = run_round
        # Retained as a source-compatibility parameter.  It intentionally does
        # not create a retry budget: one invocation owns exactly four rounds.
        self.max_attempts = max_attempts

    def run(self, initial_offset: int = 0):
        offset = int(initial_offset)
        rehearsals: list[dict] = []
        suggestions: list[int] = []
        for index in range(3):
            record = self.run_round(False, offset)
            if record.get("valid") is False or record.get("completed") is not True:
                raise RuntimeError(
                    f"排练{index + 1}结算无效，本次任务已结束；下次从该阶段续跑"
                )
            result = result_from_mapping(record)
            round_initial_offset = offset
            effective_offset = int(record.get("timing_offset_ms", round_initial_offset))
            suggested_offset = adjusted_timing_offset(effective_offset, result)
            offset = max(
                round_initial_offset - 15,
                min(round_initial_offset + 15, suggested_offset),
            )
            record = {
                **record,
                "passed": True,
                "initial_timing_offset_ms": round_initial_offset,
                "suggested_timing_offset_ms": offset,
            }
            rehearsals.append(record)
            suggestions.append(offset)
        offset = int(round(statistics.median(suggestions)))
        candidate = self.run_round(True, offset)
        if candidate.get("valid") is False or candidate.get("completed") is not True:
            raise RuntimeError(
                "正式验证结算无效，本次任务已结束；下次只补正式验证"
            )
        result = result_from_mapping(candidate)
        formal = {
            **candidate,
            "passed": formal_validation_passed(
                result,
                survived=bool(candidate["survived"]),
                completed=bool(candidate["completed"]),
            ),
        }
        return offset, rehearsals, formal


@AgentServer.custom_action("RealtimeCalibrationRun")
class RealtimeCalibration(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            return self._run(context, json.loads(argv.custom_action_param or "{}"))
        except InterruptedError as exc:
            if context.tasker.stopping:
                print(
                    f"RealtimeCalibration stopped=true reason={exc}",
                    flush=True,
                )
                return True
            traceback.print_exc()
            record_failure_reason(f"实时演奏校准中断：{exc}")
            print(f"RealtimeCalibration failed=InterruptedError: {exc}", flush=True)
            return False
        except Exception as exc:
            traceback.print_exc()
            record_failure_reason(
                f"实时演奏校准异常：{type(exc).__name__}: {exc}"
            )
            print(f"RealtimeCalibration failed={type(exc).__name__}: {exc}", flush=True)
            return False

    def _run(self, context: Context, params: dict) -> bool:
        difficulty = calibration_difficulty()
        if difficulty not in DIFFICULTY_TARGETS:
            raise ValueError(f"不支持的难度: {difficulty}")
        store = RealtimeProfileStore(PROJECT_ROOT / "profiles")
        note_speed = float(
            store.runtime_options()["calibration_note_speeds"][difficulty]
        )
        play_node = PLAY_NODES[difficulty]
        calibration_debug = debug_enabled()
        calibration_trace = diagnostic_trace_enabled()
        song_mode = calibration_song_mode()
        resume_mode = calibration_resume_mode()
        round_number = 0

        visual = verified_game_visual_settings()
        if visual is None:
            raise RuntimeError("校准未经过游戏视觉设置读回验证")
        image = context.tasker.controller.post_screencap().wait().get()
        signature = EnvironmentSignature(
            frame_resolution(image), 240, 60, "standard", note_speed,
            visual.note_skin_type,
            visual.tap_effect,
            visual.judgement_assist_effect,
        )
        session_store = CalibrationSessionStore(
            PROJECT_ROOT / "profiles" / "calibration-sessions",
            store,
        )
        selected_song = current_song_id()
        if selected_song == UNKNOWN_SONG_ID:
            selected_song = None
        session = session_store.start(
            difficulty=difficulty,
            song_mode=song_mode,
            environment=signature,
            initial_offset_ms=int(params.get("timing_offset_ms", 0)),
            current_song_id=selected_song,
            resume_mode=resume_mode,
        )
        print(
            f"RealtimeCalibration session={session['session_id']} "
            f"resume={resume_mode} next={session['next_stage']} "
            f"profile={session['candidate_profile']}",
            flush=True,
        )

        def run_round(
            formal: bool,
            offset: int,
            report_path: Path,
        ) -> dict:
            nonlocal round_number
            round_number += 1
            if context.tasker.stopping:
                raise InterruptedError("校准已停止")
            print(
                f"RealtimeCalibration round={round_number} start "
                f"mode={'formal' if formal else 'rehearsal'} "
                f"entry={CALIBRATION_ROUND_ENTRY} offset={offset}ms",
                flush=True,
            )
            reports_before = result_report_snapshot(PROJECT_ROOT / "screencap")
            play_params, override = calibration_round_plan(
                difficulty=difficulty,
                note_speed=note_speed,
                calibration_debug=calibration_debug,
                diagnostic_trace=calibration_trace,
                formal=formal,
                play_node=play_node,
                offset=offset,
                report_path=report_path,
                song_mode=song_mode,
                excluded_song_ids=list(session.get("used_song_ids", [])),
            )
            detail = context.run_task(CALIBRATION_ROUND_ENTRY, override)
            if context.tasker.stopping:
                raise InterruptedError("校准已停止")
            if detail is None or not detail.status.succeeded:
                status = None if detail is None else detail.status
                return {
                    "valid": False,
                    "completed": False,
                    "technical_reason": f"校准单轮 Maa 任务执行失败: {status}",
                }
            try:
                report = (
                    json.loads(report_path.read_text(encoding="utf-8"))
                    if report_path.exists()
                    else latest_result_report_since(
                        PROJECT_ROOT / "screencap", reports_before,
                        current_song_id(),
                    )
                )
            except Exception as exc:
                return {
                    "valid": False,
                    "completed": False,
                    "technical_reason": f"结算报告不可用: {type(exc).__name__}: {exc}",
                }
            report["valid"] = bool(
                report.get("valid", report.get("result_status", "stable") == "stable")
            )
            print(
                f"RealtimeCalibration round={round_number} complete "
                f"song={report['song_id']} hit_rate={report.get('hit_rate')}",
                flush=True,
            )
            return report

        try:
            while session.get("next_stage") is not None:
                stage = str(session["next_stage"])
                formal = stage == FORMAL_STAGE
                offset = int(session.get("current_offset_ms", 0))
                report_path = PROJECT_ROOT / (
                    "screencap/calibration-round-"
                    f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.json"
                )
                session = session_store.begin_round(
                    session,
                    stage,
                    offset,
                    report_path=str(report_path),
                )
                try:
                    record = run_round(formal, offset, report_path)
                except InterruptedError:
                    raise
                except Exception as exc:
                    record = {
                        "valid": False,
                        "completed": False,
                        "technical_reason": (
                            f"{type(exc).__name__}: {exc}"
                        ),
                    }
                suggested = offset
                if record.get("valid") is not False and record.get("completed") is True:
                    result = result_from_mapping(record)
                    if not formal:
                        effective = int(record.get("timing_offset_ms", offset))
                        raw = adjusted_timing_offset(effective, result)
                        suggested = max(offset - 15, min(offset + 15, raw))
                session = session_store.finish_round(
                    session,
                    stage,
                    record,
                    suggested_offset_ms=suggested,
                    termination_reason=(
                        record.get("technical_reason") or record.get("reason")
                    ),
                )
                if session["status"] == "paused":
                    reason = str(
                        session.get("terminal_reason")
                        or "校准单轮结算无效"
                    )
                    record_failure_reason(
                        f"实时演奏校准暂停于 {stage}：{reason}；"
                        "下次选择自动续跑会从本阶段继续"
                    )
                    print(
                        f"RealtimeCalibration paused stage={stage} "
                        f"reason={reason}",
                        flush=True,
                    )
                    return False
        except InterruptedError:
            session_store.stop(session)
            raise

        accepted = session.get("status") == "accepted"
        print(
            f"RealtimeCalibration session={session['session_id']} "
            f"status={session['status']} profile={session['candidate_profile']} "
            f"offset={session['current_offset_ms']}ms",
            flush=True,
        )
        if not accepted:
            formal = next(
                (
                    item.get("result", {})
                    for item in reversed(session.get("attempts", []))
                    if item.get("stage") == FORMAL_STAGE
                ),
                {},
            )
            record_failure_reason(
                "实时演奏校准正式验证未通过："
                f"miss={formal.get('miss', '未知')}，要求 miss < 10；"
                "候选 Profile 已保留但未接受"
            )
        return accepted
