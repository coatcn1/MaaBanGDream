from __future__ import annotations

import os
import json
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import psutil
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    from .realtime.profile_store import RealtimeProfileStore
    from .task_reporting import log_task, record_failure_reason
except ImportError:
    from realtime.profile_store import RealtimeProfileStore
    from task_reporting import log_task, record_failure_reason


_ALAS_PATH_MARKER = "/azurlaneautoscript/"


def _normalized_path(value: str) -> str:
    return str(value or "").replace("\\", "/").lower()


@dataclass(frozen=True, order=True)
class ProcessIdentity:
    pid: int
    parent_pid: int
    create_time: float
    executable: str
    command_line: tuple[str, ...]
    name: str

    @property
    def fingerprint(self) -> tuple[int, float, str]:
        return (
            self.pid,
            round(self.create_time, 6),
            _normalized_path(self.executable),
        )

    @property
    def display_path(self) -> str:
        return self.executable or (self.command_line[0] if self.command_line else self.name)


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    action: str
    processes: tuple[ProcessIdentity, ...] = ()
    errors: tuple[str, ...] = ()


def is_alas_process(identity: ProcessIdentity) -> bool:
    executable = _normalized_path(identity.executable)
    if _ALAS_PATH_MARKER in executable:
        return True
    for raw_token in identity.command_line:
        token = _normalized_path(raw_token)
        if _ALAS_PATH_MARKER not in token:
            continue
        if token.endswith((".py", ".pyw", ".exe", ".bat", ".cmd")):
            return True
    return False


def _identity(process: Any) -> ProcessIdentity:
    info = process.info
    command_line = tuple(str(value) for value in (info.get("cmdline") or ()))
    return ProcessIdentity(
        pid=int(info.get("pid") or process.pid),
        parent_pid=int(info.get("ppid") or 0),
        create_time=float(info.get("create_time") or 0.0),
        executable=str(info.get("exe") or ""),
        command_line=command_line,
        name=str(info.get("name") or ""),
    )


class ProcessConflictGuardService:
    def __init__(
        self,
        process_iter: Callable[[Sequence[str]], Iterable[Any]] = psutil.process_iter,
        wait_procs: Callable[[Iterable[Any], float], tuple[list[Any], list[Any]]] = (
            psutil.wait_procs
        ),
        *,
        state_path: str | Path | None = None,
        session_id: str = "",
        clock: Callable[[], float] = time.monotonic,
        timeout_seconds: float = 30.0,
    ):
        self._process_iter = process_iter
        self._wait_procs = wait_procs
        self._state_path = Path(state_path) if state_path else None
        self._session_id = str(session_id)
        self._clock = clock
        self._timeout_seconds = max(0.0, float(timeout_seconds))
        self._previous: frozenset[tuple[int, float, str]] = frozenset()

    def _load_previous(self) -> frozenset[tuple[int, float, str]]:
        if self._state_path is None or not self._state_path.is_file():
            return self._previous
        try:
            value = json.loads(self._state_path.read_text(encoding="utf-8"))
            if value.get("session_id") != self._session_id:
                return frozenset()
            return frozenset(
                (int(item[0]), float(item[1]), str(item[2]))
                for item in value.get("processes", [])
                if isinstance(item, list) and len(item) == 3
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return frozenset()

    def _store_previous(
        self,
        fingerprints: frozenset[tuple[int, float, str]],
    ) -> None:
        self._previous = fingerprints
        if self._state_path is None:
            return
        if not fingerprints:
            self._state_path.unlink(missing_ok=True)
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_name(
            f"{self._state_path.name}.tmp-{os.getpid()}"
        )
        temporary.write_text(
            json.dumps(
                {
                    "session_id": self._session_id,
                    "processes": [list(item) for item in sorted(fingerprints)],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self._state_path)

    def _ensure_budget(self, deadline: float) -> None:
        if self._clock() >= deadline:
            raise TimeoutError("process conflict guard exceeded its time budget")

    def _scan(self, deadline: float) -> list[tuple[ProcessIdentity, Any]]:
        attrs = ("pid", "ppid", "create_time", "exe", "cmdline", "name")
        matches: list[tuple[ProcessIdentity, Any]] = []
        self._ensure_budget(deadline)
        try:
            processes = self._process_iter(attrs)
        except (OSError, psutil.Error):
            raise
        for process in processes:
            self._ensure_budget(deadline)
            try:
                identity = _identity(process)
            except (OSError, psutil.Error, KeyError, TypeError, ValueError):
                continue
            if is_alas_process(identity):
                matches.append((identity, process))
        matches.sort(key=lambda item: item[0].pid)
        return matches

    @staticmethod
    def _children_first(
        matches: list[tuple[ProcessIdentity, Any]],
    ) -> list[tuple[ProcessIdentity, Any]]:
        by_pid = {identity.pid: identity for identity, _ in matches}

        def depth(identity: ProcessIdentity) -> int:
            result = 0
            seen: set[int] = set()
            parent = identity.parent_pid
            while parent in by_pid and parent not in seen:
                seen.add(parent)
                result += 1
                parent = by_pid[parent].parent_pid
            return result

        return sorted(matches, key=lambda item: (-depth(item[0]), item[0].pid))

    def check(self, *, skip_cleanup: bool = False) -> GuardResult:
        deadline = self._clock() + self._timeout_seconds
        try:
            matches = self._scan(deadline)
        except TimeoutError:
            return GuardResult(True, "timeout")
        if not matches:
            self._store_previous(frozenset())
            return GuardResult(True, "clear")

        current = frozenset(identity.fingerprint for identity, _ in matches)
        identities = tuple(identity for identity, _ in matches)
        if skip_cleanup:
            self._store_previous(current)
            return GuardResult(True, "skipped", identities)
        previous = self._load_previous()
        if not previous or not current.issubset(previous):
            self._store_previous(current)
            return GuardResult(False, "prompt", identities)

        errors: list[str] = []
        ordered = self._children_first(matches)
        handles = [process for _, process in ordered]
        for identity, process in ordered:
            if self._clock() >= deadline:
                return GuardResult(True, "timeout", identities, tuple(errors))
            try:
                process.terminate()
            except (OSError, psutil.Error) as exc:
                errors.append(f"PID {identity.pid} terminate: {exc}")
            except Exception as exc:
                errors.append(f"PID {identity.pid} terminate: {exc}")

        try:
            remaining_time = max(0.0, deadline - self._clock())
            if remaining_time <= 0:
                return GuardResult(True, "timeout", identities, tuple(errors))
            _, alive = self._wait_procs(handles, min(3.0, remaining_time))
        except (OSError, psutil.Error) as exc:
            errors.append(f"等待进程退出失败：{exc}")
            alive = handles

        for process in alive:
            if self._clock() >= deadline:
                return GuardResult(True, "timeout", identities, tuple(errors))
            try:
                process.kill()
            except (OSError, psutil.Error) as exc:
                errors.append(f"PID {process.pid} kill: {exc}")
            except Exception as exc:
                errors.append(f"PID {process.pid} kill: {exc}")
        if alive:
            try:
                remaining_time = max(0.0, deadline - self._clock())
                if remaining_time <= 0:
                    return GuardResult(True, "timeout", identities, tuple(errors))
                self._wait_procs(alive, min(1.0, remaining_time))
            except (OSError, psutil.Error) as exc:
                errors.append(f"等待强制结束失败：{exc}")

        try:
            remaining = self._scan(deadline)
        except TimeoutError:
            return GuardResult(True, "timeout", identities, tuple(errors))
        if remaining:
            remaining_identities = tuple(identity for identity, _ in remaining)
            self._store_previous(frozenset(
                identity.fingerprint for identity in remaining_identities
            ))
            return GuardResult(
                False,
                "failed",
                remaining_identities,
                tuple(errors),
            )

        self._store_previous(frozenset())
        return GuardResult(True, "terminated", identities, tuple(errors))


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SESSION_ID = os.environ.get(
    "MAABANGDREAM_MFA_SESSION_ID",
    f"mfa-parent-{os.getppid()}",
)
_SERVICE = ProcessConflictGuardService(
    state_path=_PROJECT_ROOT / "profiles" / ".process-conflict-guard.json",
    session_id=_SESSION_ID,
)


@AgentServer.custom_action("ProcessConflictGuard")
class ProcessConflictGuard(CustomAction):
    """Prevent two automation tools from controlling the same emulator."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        del argv
        if context.tasker.stopping:
            return True
        try:
            options = RealtimeProfileStore(_PROJECT_ROOT / "profiles").runtime_options()
            result = _SERVICE.check(
                skip_cleanup=bool(options["skip_process_conflict_cleanup"])
            )
        except Exception:
            reason = "无法完成其他程序占用检测；本次任务已停止"
            record_failure_reason(reason)
            log_task("任务前检查", "进程互斥", "ERROR", reason)
            traceback.print_exc()
            return False

        if result.action == "clear":
            log_task("任务前检查", "进程互斥", "SUCCESS", "未检测到其他程序占用")
            return True
        if result.action == "prompt":
            reason = (
                "检测到其他程序占用。本次任务已停止；请关闭占用程序后重试，"
                "或直接再次运行以允许自动清理。"
            )
            record_failure_reason(reason)
            log_task("任务前检查", "进程互斥", "WARN", reason)
            return False
        if result.action == "terminated":
            log_task("任务前检查", "进程互斥", "SUCCESS", "其他程序占用已清理")
            return True
        if result.action == "skipped":
            log_task(
                "任务前检查", "进程互斥", "WARN",
                "检测到其他程序占用；已按设置跳过清理并继续任务",
            )
            return True
        if result.action == "timeout":
            log_task(
                "任务前检查", "进程互斥", "WARN",
                "其他程序占用检测或清理超过 30 秒；已跳过剩余清理并继续任务",
            )
            return True

        reason = "检测到其他程序占用，自动清理未完成；本次任务已停止"
        record_failure_reason(reason)
        log_task("任务前检查", "进程互斥", "ERROR", reason)
        return False
