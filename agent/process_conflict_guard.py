from __future__ import annotations

import os
import json
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import psutil
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    from .task_reporting import log_task, record_failure_reason
except ImportError:
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
    ):
        self._process_iter = process_iter
        self._wait_procs = wait_procs
        self._state_path = Path(state_path) if state_path else None
        self._session_id = str(session_id)
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

    def _scan(self) -> list[tuple[ProcessIdentity, Any]]:
        attrs = ("pid", "ppid", "create_time", "exe", "cmdline", "name")
        matches: list[tuple[ProcessIdentity, Any]] = []
        try:
            processes = self._process_iter(attrs)
        except (OSError, psutil.Error):
            raise
        for process in processes:
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

    def check(self) -> GuardResult:
        matches = self._scan()
        if not matches:
            self._store_previous(frozenset())
            return GuardResult(True, "clear")

        current = frozenset(identity.fingerprint for identity, _ in matches)
        identities = tuple(identity for identity, _ in matches)
        previous = self._load_previous()
        if not previous or not current.issubset(previous):
            self._store_previous(current)
            return GuardResult(False, "prompt", identities)

        errors: list[str] = []
        ordered = self._children_first(matches)
        handles = [process for _, process in ordered]
        for identity, process in ordered:
            try:
                process.terminate()
            except (OSError, psutil.Error) as exc:
                errors.append(f"PID {identity.pid} terminate: {exc}")
            except Exception as exc:
                errors.append(f"PID {identity.pid} terminate: {exc}")

        try:
            _, alive = self._wait_procs(handles, 3.0)
        except (OSError, psutil.Error) as exc:
            errors.append(f"等待进程退出失败：{exc}")
            alive = handles

        for process in alive:
            try:
                process.kill()
            except (OSError, psutil.Error) as exc:
                errors.append(f"PID {process.pid} kill: {exc}")
            except Exception as exc:
                errors.append(f"PID {process.pid} kill: {exc}")
        if alive:
            try:
                self._wait_procs(alive, 1.0)
            except (OSError, psutil.Error) as exc:
                errors.append(f"等待强制结束失败：{exc}")

        remaining = self._scan()
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


def _process_list(processes: tuple[ProcessIdentity, ...]) -> str:
    return "；".join(
        f"PID {process.pid} ({process.display_path})" for process in processes
    )


@AgentServer.custom_action("ProcessConflictGuard")
class ProcessConflictGuard(CustomAction):
    """Prevent two automation tools from controlling the same emulator."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        del argv
        if context.tasker.stopping:
            return True
        try:
            result = _SERVICE.check()
        except Exception as exc:
            reason = f"无法检查 ALAS 冲突进程：{type(exc).__name__}: {exc}"
            record_failure_reason(reason)
            log_task("任务前检查", "进程互斥", "ERROR", reason)
            traceback.print_exc()
            return False

        processes = _process_list(result.processes)
        if result.action == "clear":
            log_task("任务前检查", "进程互斥", "SUCCESS", "未发现 ALAS 冲突进程")
            return True
        if result.action == "prompt":
            reason = (
                "检测到 ALAS/AzurLaneAutoScript 仍在运行，为避免 ADB 和模拟器输入竞争，"
                f"本次任务已阻止：{processes}。请先关闭 ALAS；若直接再次运行同一任务，"
                "MaaBanGDream 将只结束上述同一组进程后继续。"
            )
            record_failure_reason(reason)
            log_task("任务前检查", "进程互斥", "WARN", reason)
            return False
        if result.action == "terminated":
            detail = f"已结束上次提示的 ALAS 冲突进程：{processes}"
            if result.errors:
                detail += f"；温和退出异常后已复核清理：{'；'.join(result.errors)}"
            log_task("任务前检查", "进程互斥", "SUCCESS", detail)
            return True

        reason = f"ALAS 冲突进程未能完全结束，任务保持阻止：{processes}"
        if result.errors:
            reason += f"；{'；'.join(result.errors)}"
        record_failure_reason(reason)
        log_task("任务前检查", "进程互斥", "ERROR", reason)
        return False
