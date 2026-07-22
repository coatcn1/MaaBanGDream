from __future__ import annotations

import hashlib
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


_CURRENT_SONG_ID = "unknown"
DIFFICULTY_TARGETS = {
    "Easy": [715, 545], "Normal": [827, 545], "Hard": [940, 545],
    "Expert": [1051, 545], "Special": [1180, 545],
}
PLAY_NODES = {
    "Easy": "RealtimeLivePlay", "Normal": "RealtimeLivePlayNormal",
    "Hard": "RealtimeLivePlayHard", "Expert": "RealtimeLivePlayExpert",
    "Special": "RealtimeLivePlaySpecial",
}
CALIBRATION_ROUND_ENTRY = "RealtimeCalibrationSingleLive"


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


def current_song_id() -> str:
    return _CURRENT_SONG_ID


def result_from_mapping(value: dict) -> LiveResult:
    return LiveResult(**{key: value[key] for key in ("perfect", "great", "good", "bad", "miss", "fast", "slow")}, confidence=float(value.get("confidence", 1)))


def calibration_passed(result: LiveResult, survived: bool) -> bool:
    return bool(survived and result.hit_rate >= .80)


@AgentServer.custom_action("CalibrationSongIdentity")
class CalibrationSongIdentity(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        global _CURRENT_SONG_ID
        if context.tasker.stopping:
            return False
        image = context.tasker.controller.post_screencap().wait().get()
        if context.tasker.stopping:
            return False
        crop = image[110:600, 40:450]
        _CURRENT_SONG_ID = "random-" + hashlib.sha256(crop.tobytes()).hexdigest()[:16]
        print(f"RealtimeCalibration song={_CURRENT_SONG_ID}", flush=True)
        return True


class CalibrationRunner:
    """Decision logic for three distinct rehearsals and one formal validation."""

    def __init__(self, run_round, *, max_attempts: int = 10):
        self.run_round = run_round
        self.max_attempts = max_attempts

    def run(self, initial_offset: int = 0):
        offset = int(initial_offset)
        rehearsals, used = [], set()
        for _ in range(self.max_attempts):
            if len(rehearsals) == 3:
                break
            record = self.run_round(False, offset)
            result = result_from_mapping(record)
            offset = adjusted_timing_offset(offset, result)
            record = {**record, "passed": calibration_passed(result, bool(record["survived"])), "suggested_timing_offset_ms": offset}
            if record["passed"] and record["song_id"] not in used:
                rehearsals.append(record)
                used.add(record["song_id"])
        if len(rehearsals) != 3:
            raise RuntimeError("十次尝试内未取得三首不同歌曲的有效排练结果")
        formal = None
        for _ in range(self.max_attempts):
            candidate = self.run_round(True, offset)
            if candidate["song_id"] not in used:
                result = result_from_mapping(candidate)
                formal = {**candidate, "passed": calibration_passed(result, bool(candidate["survived"]))}
                break
        if formal is None:
            raise RuntimeError("十次尝试内未选到不同的第四首正式验证歌曲")
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
            play_params = {
                "difficulty": difficulty, "require_profile": False,
                "target_fps": 60, "timing_offset_ms": offset,
                "debug_recording": calibration_debug,
                "duration_seconds": 300, "dpi": 240, "game_fps": 60,
                "render_quality": "standard", "note_speed": 2.0,
                "wait_for_completion": True, "completion_missing_frames": 120,
                "require_completion": True, "save_result_frame": True,
                "result_back_attempts": 30, "result_back_interval_seconds": 1.5,
            }
            start_next = [play_node]
            override = {
                "RealtimeLiveSongSelectMarker": {"next": ["RealtimeLiveRandomSong"]},
                "RealtimeLiveRandomSong": {"next": ["CalibrationCaptureSong"]},
                "RealtimeLiveDifficulty": {"target": DIFFICULTY_TARGETS[difficulty]},
                "RealtimeLiveRehearsalStart": {"next": start_next},
                "RealtimeLiveFormalStart": {"next": start_next},
                "RealtimeLiveReturnHome": {
                    "next": ["RealtimeCalibrationRoundComplete"]
                },
                play_node: {"custom_action_param": play_params},
            }
            if formal:
                override["RealtimeLiveFormalModeGate"] = {"next": ["RealtimeLiveRehearsalToFormal", "RealtimeLiveFormalReady"]}
            detail = context.run_task(CALIBRATION_ROUND_ENTRY, override)
            if detail is None or not detail.status.succeeded:
                status = None if detail is None else detail.status
                raise RuntimeError(f"校准单轮 Maa 任务执行失败: {status}")
            report = latest_result_report_since(
                PROJECT_ROOT / "screencap", reports_before, current_song_id()
            )
            print(
                f"RealtimeCalibration round={round_number} complete "
                f"song={report['song_id']} hit_rate={report.get('hit_rate')}",
                flush=True,
            )
            return report

        runner = CalibrationRunner(run_round)
        offset, rehearsals, formal = runner.run(int(params.get("timing_offset_ms", 0)))
        image = context.tasker.controller.post_screencap().wait().get()
        signature = EnvironmentSignature(frame_resolution(image), 240, 60, "standard", 2.0)
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
