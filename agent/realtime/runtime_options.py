from __future__ import annotations

import json
from pathlib import Path

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    from ..task_reporting import latest_failure_reason
except ImportError:  # AgentServer imports realtime as a top-level package.
    from task_reporting import latest_failure_reason

from .live_session import append_current_run_event, current_live_run
from .native_prearm import discard_prearmed_backend
from .profile_store import RealtimeProfileStore


_DEBUG_ENABLED = False
_DIAGNOSTIC_TRACE_ENABLED = True
_CALIBRATION_DIFFICULTY = "Easy"
_CALIBRATION_SONG_MODE = "current"
_CALIBRATION_RESUME_MODE = "auto"
_PLAY_RETRY_COUNTS: dict[int, int] = {}
_NON_RETRYABLE_MARKERS = (
    "valueerror:",
    "typeerror:",
    "keyerror:",
    "assertionerror:",
    "difficulty conflicts with selected chart",
    "song level conflicts with selected chart",
    "song title conflicts with shared jacket",
    "身份冲突",
    "难度冲突",
    "等级冲突",
    "标题冲突",
    "配置冲突",
    "环境签名",
    "不支持的难度",
    "profile 检查失败",
    "没有可用的已接受 profile",
    "演出失败",
)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def debug_enabled() -> bool:
    return _DEBUG_ENABLED


def diagnostic_trace_enabled() -> bool:
    return _DIAGNOSTIC_TRACE_ENABLED


def calibration_difficulty() -> str:
    return _CALIBRATION_DIFFICULTY


def calibration_song_mode() -> str:
    return _CALIBRATION_SONG_MODE


def calibration_resume_mode() -> str:
    return _CALIBRATION_RESUME_MODE


@AgentServer.custom_action("RealtimeRuntimeOptions")
class RealtimeRuntimeOptions(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        global _DEBUG_ENABLED, _DIAGNOSTIC_TRACE_ENABLED
        global _CALIBRATION_DIFFICULTY
        global _CALIBRATION_SONG_MODE, _CALIBRATION_RESUME_MODE
        if context.tasker.stopping:
            return True
        params = json.loads(argv.custom_action_param or "{}")
        if "debug_recording" in params:
            _DEBUG_ENABLED = bool(params["debug_recording"])
        if "diagnostic_trace" in params:
            _DIAGNOSTIC_TRACE_ENABLED = bool(params["diagnostic_trace"])
        if "difficulty" in params:
            _CALIBRATION_DIFFICULTY = str(params["difficulty"])
        if "calibration_song_mode" in params:
            value = str(params["calibration_song_mode"])
            if value not in {"current", "random"}:
                raise ValueError(f"invalid calibration_song_mode: {value}")
            _CALIBRATION_SONG_MODE = value
        if "calibration_resume_mode" in params:
            value = str(params["calibration_resume_mode"])
            if value not in {"auto", "restart"}:
                raise ValueError(f"invalid calibration_resume_mode: {value}")
            _CALIBRATION_RESUME_MODE = value
        return True


def retryable_play_failure(reason: str) -> bool:
    """只允许瞬时技术故障进入重试，静态配置和身份冲突直接保留现场。"""
    normalized = str(reason).strip().lower()
    return not any(marker in normalized for marker in _NON_RETRYABLE_MARKERS)


@AgentServer.custom_action("RealtimePlayRetryControl")
class RealtimePlayRetryControl(CustomAction):
    """管理普通单人单局的有界重试；校准由其外层状态机负责。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        key = id(context.tasker)
        try:
            params = json.loads(argv.custom_action_param or "{}")
            operation = str(params.get("operation", "check"))
            if operation == "reset":
                _PLAY_RETRY_COUNTS.pop(key, None)
                return True
            if operation != "check":
                raise ValueError(f"invalid retry operation: {operation}")
            if context.tasker.stopping:
                return False

            run = current_live_run()
            run_mode = "unknown" if run is None else str(run.mode)
            if run_mode.startswith("calibration-") or run_mode == "challenge":
                return False

            reason = latest_failure_reason() or "实时演奏返回未知技术失败"
            if not retryable_play_failure(reason):
                self._record_decision(
                    "skipped-non-retryable",
                    reason=reason,
                    run_mode=run_mode,
                )
                print(
                    "RealtimePlayRetry retry=false class=non-retryable "
                    f"mode={run_mode} reason={reason}",
                    flush=True,
                )
                return False

            retry_limit = int(
                RealtimeProfileStore(PROJECT_ROOT / "profiles")
                .runtime_options()
                .get("play_failure_retry_count", 1)
            )
            used = _PLAY_RETRY_COUNTS.get(key, 0)
            if used >= retry_limit:
                self._record_decision(
                    "exhausted",
                    reason=reason,
                    run_mode=run_mode,
                    attempt=used + 1,
                    attempt_limit=retry_limit + 1,
                )
                _PLAY_RETRY_COUNTS.pop(key, None)
                print(
                    "RealtimePlayRetry retry=false class=exhausted "
                    f"attempt={used + 1}/{retry_limit + 1} reason={reason}",
                    flush=True,
                )
                return False

            used += 1
            _PLAY_RETRY_COUNTS[key] = used
            discard_prearmed_backend("single-play-retry")
            self._record_decision(
                "scheduled",
                reason=reason,
                run_mode=run_mode,
                attempt=used + 1,
                attempt_limit=retry_limit + 1,
            )
            print(
                "RealtimePlayRetry retry=true "
                f"attempt={used + 1}/{retry_limit + 1} reason={reason}",
                flush=True,
            )
            return True
        except Exception as exc:
            print(
                "RealtimePlayRetry failed="
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            return False

    @staticmethod
    def _record_decision(status: str, **details: object) -> None:
        try:
            append_current_run_event(
                PROJECT_ROOT,
                "retry",
                status,
                details={"mode": "single", **details},
            )
        except Exception as exc:
            print(
                "RealtimePlayRetry evidence_failed="
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
