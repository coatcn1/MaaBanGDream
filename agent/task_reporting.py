from __future__ import annotations

import json
import traceback
from dataclasses import dataclass
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction


@dataclass
class _TaskState:
    task_name: str
    label: str
    total: int
    current: int = 0
    completed: int = 0


_states: dict[int, _TaskState] = {}
_latest_failure_reason = ""


def _params(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        return json.loads(raw)
    return {}


def _task_id(context: Context, argv: CustomAction.RunArg) -> int:
    detail = getattr(argv, "task_detail", None)
    value = getattr(detail, "task_id", None)
    if value is not None:
        return int(value)
    return id(context.tasker)


def _positive_int(value: Any, default: int = 1) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _state(
    context: Context,
    argv: CustomAction.RunArg,
    params: dict[str, Any],
) -> tuple[int, _TaskState]:
    task_id = _task_id(context, argv)
    detail = getattr(argv, "task_detail", None)
    entry = str(getattr(detail, "entry", "Task"))
    total = _positive_int(params.get("total", 1))
    task_name = str(params.get("task_name", entry))
    label = str(params.get("label", task_name))
    current = _states.get(task_id)
    if current is None:
        current = _TaskState(task_name=task_name, label=label, total=total)
        _states[task_id] = current
    else:
        if "task_name" in params:
            current.task_name = task_name
        if "label" in params:
            current.label = label
        if "total" in params:
            current.total = total
    return task_id, current


def log_task(label: str, stage: str, level: str, message: str) -> None:
    print(f"[任务][{label}][{stage}][{level}] {message}", flush=True)


def record_failure_reason(reason: str) -> None:
    global _latest_failure_reason
    _latest_failure_reason = str(reason).strip()


def _take_failure_reason() -> str:
    global _latest_failure_reason
    reason = _latest_failure_reason
    _latest_failure_reason = ""
    return reason


def _visible_log(
    context: Context,
    content: str,
    *,
    toast: bool = False,
) -> bool:
    display = ["log", "toast"] if toast else ["log"]
    try:
        detail = context.run_task(
            "TaskReportVisible",
            {
                "TaskReportVisible": {
                    "focus": {
                        "Node.Action.Succeeded": {
                            "content": content,
                            "display": display,
                        }
                    }
                }
            },
        )
        return bool(detail and detail.status.succeeded)
    except Exception:
        traceback.print_exc()
        return False


def clear_states() -> None:
    global _latest_failure_reason
    _states.clear()
    _latest_failure_reason = ""


def active_task_ids() -> set[int]:
    return set(_states)


@AgentServer.custom_action("TaskProgress")
class TaskProgress(CustomAction):
    """Report round progress without replacing MaaFramework max_hit."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            params = _params(argv.custom_action_param)
            _task_id_value, state = _state(context, argv, params)
            phase = str(params.get("phase", "start"))

            if phase == "start":
                hit_count = int(context.get_hit_count(argv.node_name))
                state.current = max(1, hit_count, state.current + 1)
                state.current = min(state.current, state.total)
                message = (
                    f"{state.label}演奏次数：当前 {state.current}/{state.total}，"
                    f"已完成 {state.completed}/{state.total}"
                )
                log_task(state.label, "进度", "INFO", message)
                _visible_log(context, message)
                return True

            if phase == "completed":
                if state.current <= state.completed:
                    state.current = min(state.total, state.completed + 1)
                state.completed = min(state.total, max(state.completed, state.current))
                message = (
                    f"{state.label}演奏次数：已完成 "
                    f"{state.completed}/{state.total}"
                )
                log_task(state.label, "进度", "INFO", message)
                _visible_log(context, message)
                return True

            log_task(
                state.label,
                "进度",
                "ERROR",
                f"未知进度阶段：{phase}",
            )
            return False
        except Exception as exc:
            traceback.print_exc()
            print(
                f"[任务][进度][ERROR] {type(exc).__name__}: {exc}",
                flush=True,
            )
            return False


@AgentServer.custom_action("TaskOutcome")
class TaskOutcome(CustomAction):
    """Emit an explicit terminal outcome and preserve Framework failure."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            params = _params(argv.custom_action_param)
            task_id, state = _state(context, argv, params)
            status = str(params.get("status", "success")).lower()
            reason = str(params.get("reason", "")).strip()

            if context.tasker.stopping:
                log_task(state.label, "结束", "INFO", "用户已停止任务")
                _states.pop(task_id, None)
                _take_failure_reason()
                return True

            if status == "success":
                completed = state.total
                message = (
                    f"{state.label}任务成功：已完成 "
                    f"{completed}/{state.total}"
                )
                log_task(state.label, "结束", "SUCCESS", message)
                _visible_log(context, message, toast=True)
                _states.pop(task_id, None)
                _take_failure_reason()
                return True

            if str(params.get("reason_source", "")).lower() == "latest":
                reason = _take_failure_reason() or reason
            message = (
                f"任务失败，已完成 {state.completed}/{state.total}"
                f"：{reason or '未提供失败原因'}"
            )
            log_task(state.label, "结束", "ERROR", message)
            _visible_log(context, message)
            _states.pop(task_id, None)
            return False
        except Exception as exc:
            traceback.print_exc()
            print(
                f"[任务][结束][ERROR] {type(exc).__name__}: {exc}",
                flush=True,
            )
            return False
