from __future__ import annotations

import json
import traceback
from datetime import datetime
from pathlib import Path

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from .profile_action import PROJECT_ROOT
from .profile_store import EnvironmentSignature, RealtimeProfileStore
from .rehearsal_action import frame_resolution
from .result_parser import LiveResult, adjusted_timing_offset
from .runtime_options import calibration_difficulty, debug_enabled
from .difficulty_action import DIFFICULTY_TARGETS
from .game_effect_settings_action import verified_game_visual_settings
from .live_session import current_song_id


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
) -> tuple[dict, dict]:
    """Build the self-contained pipeline override for one calibration round."""
    play_params = {
        "difficulty": difficulty, "require_profile": False,
        "target_fps": 60, "timing_offset_ms": offset,
        "debug_recording": calibration_debug,
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
        "RealtimeLiveSongSelectMarker": {"next": ["RealtimeLiveDifficulty"]},
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


def calibration_passed(result: LiveResult, survived: bool) -> bool:
    return bool(survived and result.hit_rate >= .80)


class CalibrationRunner:
    """Run three valid rehearsals and one valid formal validation.

    Calibration targets timing-offset feedback, not song diversity, so
    repeated songs are accepted and no song-identity validation is applied.
    """

    def __init__(self, run_round, *, max_attempts: int = 10):
        self.run_round = run_round
        self.max_attempts = max_attempts

    def run(self, initial_offset: int = 0):
        offset = int(initial_offset)
        rehearsals = []
        for _ in range(self.max_attempts):
            if len(rehearsals) == 3:
                break
            record = self.run_round(False, offset)
            if record.get("valid") is False:
                continue
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
                "passed": calibration_passed(result, bool(record["survived"])),
                "initial_timing_offset_ms": round_initial_offset,
                "suggested_timing_offset_ms": offset,
            }
            rehearsals.append(record)
        if len(rehearsals) != 3:
            raise RuntimeError("十次尝试内未取得三次有效排练结果")
        if not any(record["passed"] for record in rehearsals):
            raise RuntimeError("三首排练全部失败，校准已终止")
        formal = None
        for _ in range(self.max_attempts):
            candidate = self.run_round(True, offset)
            if candidate.get("valid") is False:
                continue
            result = result_from_mapping(candidate)
            formal = {
                **candidate,
                "passed": calibration_passed(result, bool(candidate["survived"])),
            }
            break
        if formal is None:
            raise RuntimeError("十次尝试内未取得有效正式验证结果")
        return offset, rehearsals, formal


@AgentServer.custom_action("RealtimeCalibrationRun")
class RealtimeCalibration(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            return self._run(context, json.loads(argv.custom_action_param or "{}"))
        except Exception as exc:
            traceback.print_exc()
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
        round_number = 0

        def run_round(formal: bool, offset: int) -> dict:
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
            report_path = PROJECT_ROOT / (
                "screencap/calibration-round-"
                f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.json"
            )
            play_params, override = calibration_round_plan(
                difficulty=difficulty,
                note_speed=note_speed,
                calibration_debug=calibration_debug,
                formal=formal,
                play_node=play_node,
                offset=offset,
                report_path=report_path,
            )
            detail = context.run_task(CALIBRATION_ROUND_ENTRY, override)
            if detail is None or not detail.status.succeeded:
                status = None if detail is None else detail.status
                raise RuntimeError(f"校准单轮 Maa 任务执行失败: {status}")
            report = (
                json.loads(report_path.read_text(encoding="utf-8"))
                if report_path.exists()
                else latest_result_report_since(
                    PROJECT_ROOT / "screencap", reports_before, current_song_id()
                )
            )
            print(
                f"RealtimeCalibration round={round_number} complete "
                f"song={report['song_id']} hit_rate={report.get('hit_rate')}",
                flush=True,
            )
            return report

        runner = CalibrationRunner(run_round)
        offset, rehearsals, formal = runner.run(int(params.get("timing_offset_ms", 0)))
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
        payload = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "difficulty": difficulty,
            "accepted": bool(formal["passed"]),
            "accepted_at": datetime.now().isoformat(timespec="seconds") if formal["passed"] else None,
            "environment": signature.to_mapping(),
            "settings": {"target_fps": 60, "timing_offset_ms": offset, "frame_timeout_ms": 150, "playfield_timeout_ms": 1500},
            "rehearsals": rehearsals,
            "formal": formal,
        }
        path = RealtimeProfileStore(PROJECT_ROOT / "profiles").write(payload)
        print(f"RealtimeCalibration profile={path.name} accepted={payload['accepted']} offset={offset}ms", flush=True)
        return bool(payload["accepted"])
