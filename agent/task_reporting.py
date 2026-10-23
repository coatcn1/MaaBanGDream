from __future__ import annotations

import json
import traceback
from dataclasses import dataclass
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition


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


def _performance_count(value: Any) -> int:
    count = int(value)
    if isinstance(value, bool) or str(value) != str(count) or not 0 <= count <= 999:
        raise ValueError("演出次数必须是 0..999 的整数，0 表示无限")
    return count


def _state(
    context: Context,
    argv: CustomAction.RunArg,
    params: dict[str, Any],
) -> tuple[int, _TaskState]:
    task_id = _task_id(context, argv)
    detail = getattr(argv, "task_detail", None)
    entry = str(getattr(detail, "entry", "Task"))
    total = _performance_count(params.get("total", 1))
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


def _safe_console(*args, **kwargs):
    try:
        print(*args, **kwargs)
    except Exception:
        pass


def _safe_traceback():
    try:
        traceback.print_exc()
    except Exception:
        pass


def log_task(label: str, stage: str, level: str, message: str) -> None:
    _safe_console(f"[任务][{label}][{stage}][{level}] {message}", flush=True)


def _safe_log_task(*args):
    try:
        log_task(*args)
    except Exception:
        # 调用方报告器或 stdout 故障都不能改变内存中的完成数。
        pass


def record_failure_reason(reason: str) -> None:
    global _latest_failure_reason
    _latest_failure_reason = str(reason).strip()


def latest_failure_reason() -> str:
    """只读返回最近失败原因，供恢复策略分类，不提前消费终态信息。"""
    return _latest_failure_reason


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
        _safe_traceback()
        return False


def report_task_configuration(context, label: str, message: str) -> None:
    if context.tasker.stopping:
        return
    try:
        _safe_log_task(label,"配置","INFO",message)
        _visible_log(context,message)
    except Exception as exc:
        _safe_console(f"TaskConfiguration display_warning={exc}",flush=True)


def report_task_warning(context, label: str, message: str) -> None:
    """附加功能警告尽力显示到任务日志，日志通道故障不能改变业务结果。"""
    if context.tasker.stopping:
        return
    try:
        _safe_log_task(label,"体力补充","WARN",message)
        _visible_log(context,f"⚠ {message}")
    except Exception as exc:
        _safe_console(f"TaskWarning display_warning={exc}",flush=True)


def clear_states() -> None:
    global _latest_failure_reason
    _states.clear()
    _latest_failure_reason = ""


def remaining_task_rounds(argv) -> int | None:
    """只按可信任务身份读取剩余完成数；无限或未初始化返回未知，不复用包装对象。"""
    value = getattr(getattr(argv, "task_detail", None), "task_id", None)
    if type(value) is not int or value <= 0:
        raise ValueError("剩余局数缺少可信 task_detail.task_id")
    state = _states.get(value)
    if state is None or state.total == 0:
        return None
    return max(0, state.total - state.completed)


def active_task_ids() -> set[int]:
    return set(_states)


@AgentServer.custom_recognition("TaskRoundAvailable")
class TaskRoundAvailable(CustomRecognition):
    """以实际入口命中数限制有限任务；无限任务不使用 max_hit。"""

    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        if context.tasker.stopping:
            return None
        try:
            total = _performance_count(_params(argv.custom_recognition_param).get("total", 1))
            if total == 0 or context.get_hit_count(argv.node_name) < total:
                return (0, 0, 1, 1)
            return None
        except Exception as exc:
            # 非法参数交给同节点的 TaskProgress 显式失败，不能误走成功终点。
            record_failure_reason(f"演出次数无效：{exc}")
            _safe_traceback()
            return (0, 0, 1, 1)


@AgentServer.custom_action("TaskProgress")
class TaskProgress(CustomAction):
    """记录有限或无限演出进度；入口次数门禁由 TaskRoundAvailable 负责。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        if context.tasker.stopping:
            return True
        try:
            params = _params(argv.custom_action_param)
            _task_id_value, state = _state(context, argv, params)
            phase = str(params.get("phase", "start"))
            limit = str(state.total) if state.total else "无限"

            if phase == "start":
                try:
                    hit_count = int(context.get_hit_count(argv.node_name))
                except Exception as exc:
                    if context.tasker.stopping:
                        return True
                    hit_count = 0
                    _safe_console(f"TaskProgress count_observation_warning={type(exc).__name__}: {exc}", flush=True)
                state.current = max(1, hit_count, state.current + 1)
                if state.total:
                    state.current = min(state.current, state.total)
                message = (
                    f"{state.label}演奏次数：当前 {state.current}/{limit}，"
                    f"已完成 {state.completed}/{limit}"
                )
                _safe_log_task(state.label, "进度", "INFO", message)
                _visible_log(context, message)
                return True

            if phase == "completed":
                if state.current <= state.completed:
                    state.current = state.completed + 1
                state.completed = max(state.completed, state.current)
                if state.total:
                    state.current = min(state.total, state.current)
                    state.completed = min(state.total, state.completed)
                message = (
                    f"{state.label}演奏次数：已完成 "
                    f"{state.completed}/{limit}"
                )
                _safe_log_task(state.label, "进度", "INFO", message)
                _visible_log(context, message)
                return True

            if phase == "restore":
                try:
                    completed = int(params["completed"])
                except (KeyError, TypeError, ValueError):
                    _safe_log_task(
                        state.label,
                        "进度",
                        "ERROR",
                        "恢复进度缺少有效 completed",
                    )
                    return False
                if completed < 0 or (state.total and completed > state.total):
                    _safe_log_task(
                        state.label,
                        "进度",
                        "ERROR",
                        f"恢复进度超出范围：{completed}/{state.total}",
                    )
                    return False
                next_started = bool(params.get("next_started", True))
                state.completed = completed
                state.current = (
                    completed + 1
                    if next_started and (state.total == 0 or completed < state.total)
                    else completed
                )
                message = (
                    f"{state.label}进度已恢复：当前 {state.current}/{limit}，"
                    f"已完成 {state.completed}/{limit}"
                )
                _safe_log_task(state.label, "进度", "INFO", message)
                _visible_log(context, message)
                return True

            _safe_log_task(
                state.label,
                "进度",
                "ERROR",
                f"未知进度阶段：{phase}",
            )
            return False
        except Exception as exc:
            _safe_traceback()
            _safe_console(
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
                _safe_log_task(state.label, "结束", "INFO", "用户已停止任务")
                _states.pop(task_id, None)
                _take_failure_reason()
                return True

            if status == "success":
                completed = state.total or state.completed
                message = (
                    f"{state.label}任务成功：已完成 "
                    f"{completed}/{state.total or '无限'}"
                )
                _safe_log_task(state.label, "结束", "SUCCESS", message)
                _visible_log(context, message, toast=True)
                _states.pop(task_id, None)
                _take_failure_reason()
                return True

            if str(params.get("reason_source", "")).lower() == "latest":
                reason = _take_failure_reason() or reason
            message = (
                f"任务失败，已完成 {state.completed}/{state.total or '无限'}"
                f"：{reason or '未提供失败原因'}"
            )
            _safe_log_task(state.label, "结束", "ERROR", message)
            _visible_log(context, message)
            _states.pop(task_id, None)
            return False
        except Exception as exc:
            _safe_traceback()
            _safe_console(
                f"[任务][结束][ERROR] {type(exc).__name__}: {exc}",
                flush=True,
            )
            return False
