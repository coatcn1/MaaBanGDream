from __future__ import annotations

from dataclasses import dataclass

from agent.process_conflict_guard import (
    ProcessConflictGuardService,
    ProcessIdentity,
    is_alas_process,
)


@dataclass
class FakeProcess:
    pid: int
    ppid: int
    create_time: float
    exe: str
    cmdline: list[str]
    name: str = "python.exe"
    alive: bool = True
    terminate_error: Exception | None = None
    kill_error: Exception | None = None
    calls: list[tuple[str, int]] | None = None

    @property
    def info(self):
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "create_time": self.create_time,
            "exe": self.exe,
            "cmdline": self.cmdline,
            "name": self.name,
        }

    def terminate(self):
        if self.calls is not None:
            self.calls.append(("terminate", self.pid))
        if self.terminate_error:
            raise self.terminate_error

    def kill(self):
        if self.calls is not None:
            self.calls.append(("kill", self.pid))
        if self.kill_error:
            raise self.kill_error
        self.alive = False


class FakeProcessTable:
    def __init__(self, processes: list[FakeProcess]):
        self.processes = processes
        self.wait_calls: list[tuple[list[int], float]] = []

    def process_iter(self, _attrs):
        return [process for process in self.processes if process.alive]

    def wait_procs(self, processes, timeout):
        process_list = list(processes)
        self.wait_calls.append(([process.pid for process in process_list], timeout))
        for process in process_list:
            if process.terminate_error is None:
                process.alive = False
        gone = [process for process in process_list if not process.alive]
        alive = [process for process in process_list if process.alive]
        return gone, alive


def alas_process(
    pid: int,
    *,
    ppid: int = 1,
    create_time: float = 100.0,
    calls: list[tuple[str, int]] | None = None,
) -> FakeProcess:
    executable = r"E:\alas\AzurLaneAutoScript\toolkit\python.exe"
    return FakeProcess(
        pid=pid,
        ppid=ppid,
        create_time=create_time,
        exe=executable,
        cmdline=[executable, "-c", "from multiprocessing.spawn import spawn_main"],
        calls=calls,
    )


def test_alas_match_requires_an_explicit_install_path_or_script():
    assert is_alas_process(
        ProcessIdentity(
            pid=10,
            parent_pid=1,
            create_time=1.0,
            executable=r"E:\alas\AzurLaneAutoScript\toolkit\python.exe",
            command_line=(),
            name="python.exe",
        )
    )
    assert is_alas_process(
        ProcessIdentity(
            pid=11,
            parent_pid=1,
            create_time=1.0,
            executable=r"C:\Python312\python.exe",
            command_line=(
                r"C:\Python312\python.exe",
                r"E:\alas\AzurLaneAutoScript\alas.py",
            ),
            name="python.exe",
        )
    )
    assert not is_alas_process(
        ProcessIdentity(
            pid=12,
            parent_pid=1,
            create_time=1.0,
            executable=r"C:\Python312\python.exe",
            command_line=(
                r"C:\Python312\python.exe",
                "print('AzurLaneAutoScript documentation')",
            ),
            name="python.exe",
        )
    )
    assert not is_alas_process(
        ProcessIdentity(
            pid=13,
            parent_pid=1,
            create_time=1.0,
            executable=r"D:\Documents\workplace\.tools\Miniconda3\python.exe",
            command_line=("python.exe", "agent/server.py"),
            name="python.exe",
        )
    )


def test_first_detection_prompts_without_terminating():
    calls: list[tuple[str, int]] = []
    process = alas_process(20, calls=calls)
    table = FakeProcessTable([process])
    guard = ProcessConflictGuardService(table.process_iter, table.wait_procs)

    result = guard.check()

    assert not result.allowed
    assert result.action == "prompt"
    assert [identity.pid for identity in result.processes] == [20]
    assert calls == []


def test_second_detection_terminates_children_before_parents():
    calls: list[tuple[str, int]] = []
    parent = alas_process(30, calls=calls)
    child = alas_process(31, ppid=30, calls=calls)
    table = FakeProcessTable([parent, child])
    guard = ProcessConflictGuardService(table.process_iter, table.wait_procs)
    assert guard.check().action == "prompt"

    result = guard.check()

    assert result.allowed
    assert result.action == "terminated"
    assert calls == [("terminate", 31), ("terminate", 30)]
    assert table.wait_calls == [([31, 30], 3.0)]


def test_pid_reuse_or_restarted_process_requires_a_new_prompt():
    original = alas_process(40, create_time=100.0)
    table = FakeProcessTable([original])
    guard = ProcessConflictGuardService(table.process_iter, table.wait_procs)
    assert guard.check().action == "prompt"

    original.alive = False
    replacement = alas_process(40, create_time=200.0)
    table.processes.append(replacement)
    result = guard.check()

    assert not result.allowed
    assert result.action == "prompt"
    assert replacement.alive


def test_partial_manual_cleanup_allows_remaining_original_process_to_be_closed():
    first = alas_process(50)
    second = alas_process(51)
    table = FakeProcessTable([first, second])
    guard = ProcessConflictGuardService(table.process_iter, table.wait_procs)
    assert guard.check().action == "prompt"

    first.alive = False
    result = guard.check()

    assert result.allowed
    assert result.action == "terminated"
    assert not second.alive


def test_failed_graceful_termination_forces_kill():
    calls: list[tuple[str, int]] = []
    process = alas_process(60, calls=calls)
    process.terminate_error = PermissionError("terminate denied")
    table = FakeProcessTable([process])
    guard = ProcessConflictGuardService(table.process_iter, table.wait_procs)
    assert guard.check().action == "prompt"

    result = guard.check()

    assert result.allowed
    assert result.action == "terminated"
    assert calls == [("terminate", 60), ("kill", 60)]


def test_process_that_survives_terminate_and_kill_keeps_task_blocked():
    process = alas_process(70)
    process.terminate_error = PermissionError("terminate denied")
    process.kill_error = PermissionError("kill denied")
    table = FakeProcessTable([process])
    guard = ProcessConflictGuardService(table.process_iter, table.wait_procs)
    assert guard.check().action == "prompt"

    result = guard.check()

    assert not result.allowed
    assert result.action == "failed"
    assert [identity.pid for identity in result.processes] == [70]


def test_clear_scan_resets_the_previous_authorization():
    process = alas_process(80)
    table = FakeProcessTable([process])
    guard = ProcessConflictGuardService(table.process_iter, table.wait_procs)
    assert guard.check().action == "prompt"
    process.alive = False
    assert guard.check().action == "clear"

    replacement = alas_process(81)
    table.processes.append(replacement)
    result = guard.check()

    assert not result.allowed
    assert result.action == "prompt"
    assert replacement.alive


def test_prompt_authorization_survives_agent_restart_within_mfa_session(tmp_path):
    process = alas_process(90)
    table = FakeProcessTable([process])
    state_path = tmp_path / "process-conflict-guard.json"
    first_agent = ProcessConflictGuardService(
        table.process_iter,
        table.wait_procs,
        state_path=state_path,
        session_id="mfa-session-1",
    )
    assert first_agent.check().action == "prompt"
    assert state_path.is_file()

    second_agent = ProcessConflictGuardService(
        table.process_iter,
        table.wait_procs,
        state_path=state_path,
        session_id="mfa-session-1",
    )
    result = second_agent.check()

    assert result.allowed
    assert result.action == "terminated"
    assert not state_path.exists()


def test_restarting_mfa_invalidates_previous_termination_authorization(tmp_path):
    process = alas_process(100)
    table = FakeProcessTable([process])
    state_path = tmp_path / "process-conflict-guard.json"
    first_session = ProcessConflictGuardService(
        table.process_iter,
        table.wait_procs,
        state_path=state_path,
        session_id="mfa-session-1",
    )
    assert first_session.check().action == "prompt"

    restarted_mfa = ProcessConflictGuardService(
        table.process_iter,
        table.wait_procs,
        state_path=state_path,
        session_id="mfa-session-2",
    )
    result = restarted_mfa.check()

    assert not result.allowed
    assert result.action == "prompt"
    assert process.alive

def test_skip_cleanup_warns_but_never_terminates():
    calls: list[tuple[str, int]] = []
    process = alas_process(110, calls=calls)
    table = FakeProcessTable([process])
    guard = ProcessConflictGuardService(table.process_iter, table.wait_procs)

    result = guard.check(skip_cleanup=True)

    assert result.allowed
    assert result.action == "skipped"
    assert process.alive
    assert calls == []


def test_scan_timeout_skips_remaining_cleanup_and_allows_task():
    ticks = iter((0.0, 0.0, 31.0))
    process = alas_process(120)
    table = FakeProcessTable([process])
    guard = ProcessConflictGuardService(
        table.process_iter,
        table.wait_procs,
        clock=lambda: next(ticks),
        timeout_seconds=30.0,
    )

    result = guard.check()

    assert result.allowed
    assert result.action == "timeout"
    assert process.alive
