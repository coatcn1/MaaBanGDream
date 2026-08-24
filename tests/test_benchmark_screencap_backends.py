from __future__ import annotations

import json
import subprocess
import sys
import weakref
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from agent.realtime.frame_observer import LatestFrameObserver
from scripts.benchmark_screencap_backends import (
    BENCHMARK_ROOT,
    ControllerSettings,
    build_controller_factory,
    execute_benchmark,
    load_controller_settings,
    main,
    run_benchmark_suite,
    write_suite_report,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark_screencap_backends.py"


def test_cli_defaults_to_safe_formal_dry_run() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--backend", "EmulatorExtras"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload == {
        "mode": "dry-run",
        "backends": ["EmulatorExtras"],
        "rounds": 3,
        "duration_seconds": 300.0,
        "frame_timeout_ms": 150,
    }


def test_cli_hard_rejects_lossy_minicap() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--backend", "MinicapStream"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "lossy Minicap backend is forbidden" in completed.stderr


def test_cli_rejects_a_zero_round_benchmark() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--backend",
            "EmulatorExtras",
            "--rounds",
            "0",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "rounds must be in 1..20" in completed.stderr


def test_suite_uses_a_fresh_controller_for_each_backend() -> None:
    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    class Job:
        def __init__(self, clock: Clock, delay: float) -> None:
            self.clock = clock
            self.delay = delay

        def wait(self):
            self.clock.value += self.delay
            return self

        def get(self):
            return np.zeros((1, 1, 3), dtype=np.uint8)

    class Controller:
        def __init__(self, clock: Clock, delay: float) -> None:
            self.clock = clock
            self.delay = delay

        def post_screencap(self) -> Job:
            return Job(self.clock, self.delay)

    clock = Clock()
    opened: list[str] = []
    closed: list[str] = []

    @contextmanager
    def controller_factory(backend: str):
        opened.append(backend)
        try:
            yield Controller(clock, 0.01 if backend == "Encode" else 0.02)
        finally:
            closed.append(backend)

    result = run_benchmark_suite(
        ["Encode", "RawWithGzip"],
        rounds=2,
        duration_seconds=0.1,
        frame_timeout_ms=150,
        controller_factory=controller_factory,
        observer_factory=lambda: LatestFrameObserver(clock),
    )

    assert opened == ["Encode", "RawWithGzip"]
    assert closed == opened
    assert [candidate["backend"] for candidate in result["candidates"]] == opened
    assert all(len(candidate["rounds"]) == 2 for candidate in result["candidates"])


def test_short_smoke_suite_reports_metrics_but_cannot_qualify() -> None:
    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    class Controller:
        def __init__(self, clock: Clock, delay: float) -> None:
            self.clock = clock
            self.delay = delay

        def post_screencap(self):
            clock = self.clock
            delay = self.delay

            class Job:
                def wait(self):
                    clock.value += delay
                    return self

                def get(self):
                    return np.zeros((1, 1, 3), dtype=np.uint8)

            return Job()

    clock = Clock()

    @contextmanager
    def controller_factory(backend: str):
        yield Controller(clock, 0.01 if backend == "Encode" else 0.02)

    result = run_benchmark_suite(
        ["RawWithGzip", "Encode"],
        rounds=2,
        duration_seconds=0.1,
        frame_timeout_ms=150,
        controller_factory=controller_factory,
        observer_factory=lambda: LatestFrameObserver(clock),
        baseline_backend="Encode",
    )

    assert [candidate["backend"] for candidate in result["candidates"]] == [
        "Encode",
        "RawWithGzip",
    ]
    best = result["candidates"][0]["summary"]
    assert best == {
        "maximum_capture_ms": 10.0,
        "capture_p95_ms": 10.0,
        "capture_mean_ms": 10.0,
        "over_150ms_frames": 0,
        "invalid_frames": 0,
        "timing_qualified": False,
        "qualification_reasons": [
            "formal qualification requires at least 3 rounds of 300 seconds"
        ],
    }


def test_full_three_by_five_minute_suite_can_qualify() -> None:
    from agent.realtime.frame_observer_core import ObservationStats

    complete = ObservationStats(
        frames=18_000,
        elapsed_seconds=300.0,
        effective_fps=60.0,
        capture_mean_ms=4.0,
        capture_p50_ms=3.5,
        capture_p95_ms=6.0,
        maximum_capture_ms=20.0,
        over_100ms_frames=0,
        over_150ms_frames=0,
        timed_out_frames=0,
        invalid_frames=0,
        stopped=False,
    )

    class Observer:
        def run(self, *_args, **_kwargs):
            return complete

    @contextmanager
    def controller_factory(_backend: str):
        yield object()

    result = run_benchmark_suite(
        ["EmulatorExtras"],
        rounds=3,
        duration_seconds=300.0,
        frame_timeout_ms=150,
        controller_factory=controller_factory,
        observer_factory=Observer,
        baseline_backend="EmulatorExtras",
    )

    summary = result["candidates"][0]["summary"]
    assert summary["timing_qualified"] is True
    assert summary["qualification_reasons"] == []


def test_requested_formal_window_cannot_qualify_with_short_actual_rounds() -> None:
    from agent.realtime.frame_observer_core import ObservationStats

    short = ObservationStats(
        frames=60,
        elapsed_seconds=1.0,
        effective_fps=60.0,
        capture_mean_ms=4.0,
        capture_p50_ms=3.5,
        capture_p95_ms=6.0,
        maximum_capture_ms=20.0,
        over_100ms_frames=0,
        over_150ms_frames=0,
        timed_out_frames=0,
        invalid_frames=0,
        stopped=False,
    )

    class Observer:
        def run(self, *_args, **_kwargs):
            return short

    @contextmanager
    def controller_factory(_backend: str):
        yield object()

    result = run_benchmark_suite(
        ["EmulatorExtras"],
        rounds=3,
        duration_seconds=300.0,
        frame_timeout_ms=150,
        controller_factory=controller_factory,
        observer_factory=Observer,
        baseline_backend="EmulatorExtras",
    )

    summary = result["candidates"][0]["summary"]
    assert summary["timing_qualified"] is False
    assert "one or more rounds did not cover 300 seconds" in (
        summary["qualification_reasons"]
    )


def test_suite_report_contains_only_benchmark_data(tmp_path: Path) -> None:
    path = write_suite_report(
        tmp_path,
        {
            "backends": ["EmulatorExtras"],
            "rounds": 3,
            "duration_seconds": 300.0,
            "baseline_backend": "EmulatorExtras",
            "candidates": [{"backend": "EmulatorExtras", "rounds": []}],
            "adb_path": "DO-NOT-WRITE",
            "device_serial": "DO-NOT-WRITE",
        },
        started_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert path.parent == tmp_path
    assert path.name == "screencap-benchmark-suite-20260809-000000-000000.json"
    assert payload["schema_version"] == 1
    assert payload["baseline_backend"] == "EmulatorExtras"
    assert payload["candidates"][0]["backend"] == "EmulatorExtras"
    assert "DO-NOT-WRITE" not in path.read_text(encoding="utf-8")


def test_runtime_controller_secrets_are_redacted_from_diagnostics(
    tmp_path: Path,
) -> None:
    mfa_root = tmp_path / "mfa"
    instance = tmp_path / "instance.json"
    instance.write_text(json.dumps({
        "AdbDevice": {
            "AdbPath": "C:/private/adb.exe",
            "AdbSerial": "private-device-serial",
            "InputMethods": 7,
            "Config": "{\"extras\":{}}",
        }
    }), encoding="utf-8")

    settings = load_controller_settings(instance, mfa_root=mfa_root)

    assert settings.adb_path == "C:/private/adb.exe"
    assert settings.adb_serial == "private-device-serial"
    assert settings.input_methods == 7
    assert settings.config == {"extras": {}}
    assert settings.agent_path == mfa_root / "libs" / "MaaAgentBinary"
    assert "private" not in repr(settings)


def test_controller_factory_pins_one_explicit_lossless_method(tmp_path: Path) -> None:
    created: list[dict[str, object]] = []

    class ConnectionJob:
        succeeded = True

        def wait(self):
            return self

    class FakeController:
        connected = True

        def __init__(self, **kwargs) -> None:
            created.append(kwargs)

        def post_connection(self) -> ConnectionJob:
            return ConnectionJob()

    settings = ControllerSettings(
        adb_path="secret-adb",
        adb_serial="secret-serial",
        input_methods=7,
        config={"extras": {}},
        agent_path=tmp_path / "MaaAgentBinary",
    )
    factory = build_controller_factory(
        settings,
        controller_type=FakeController,
        method_values={"Encode": 2},
    )

    with factory("Encode") as controller:
        assert controller.connected

    assert len(created) == 1
    assert created[0]["screencap_methods"] == 2


def test_observer_core_can_load_without_agent_server_side_effects() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from agent.realtime.frame_observer_core import LatestFrameObserver; "
                "assert LatestFrameObserver; "
                "assert 'maa.agent.agent_server' not in sys.modules"
            ),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_execute_writes_rounds_and_suite_only_under_benchmark_root(
    tmp_path: Path,
) -> None:
    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    class Controller:
        def __init__(self, clock: Clock) -> None:
            self.clock = clock

        def post_screencap(self):
            clock = self.clock

            class Job:
                def wait(self):
                    clock.value += 0.02
                    return self

                def get(self):
                    return np.zeros((1, 1, 3), dtype=np.uint8)

            return Job()

    clock = Clock()

    @contextmanager
    def controller_factory(_backend: str):
        yield Controller(clock)

    suite_path, result = execute_benchmark(
        ["EmulatorExtras"],
        rounds=2,
        duration_seconds=0.1,
        frame_timeout_ms=150,
        baseline_backend="EmulatorExtras",
        output_root=tmp_path,
        controller_factory=controller_factory,
        observer_factory=lambda: LatestFrameObserver(clock),
        started_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )

    artifacts = sorted(tmp_path.glob("*.json"))
    assert suite_path in artifacts
    assert len(artifacts) == 3
    assert result["candidates"][0]["summary"]["timing_qualified"] is False
    assert all(path.parent == tmp_path for path in artifacts)


def test_execute_cli_wires_the_fixed_private_artifact_root(
    tmp_path: Path,
    capsys,
) -> None:
    calls: list[dict[str, object]] = []

    def executor(backends, **kwargs):
        calls.append({"backends": backends, **kwargs})
        return (
            BENCHMARK_ROOT / "suite.json",
            {
                "candidates": [{
                    "backend": "EmulatorExtras",
                    "summary": {"timing_qualified": True},
                }]
            },
        )

    code = main(
        [
            "--backend",
            "EmulatorExtras",
            "--rounds",
            "1",
            "--duration-seconds",
            "0.1",
            "--mfa-root",
            str(tmp_path / "private-mfa-root"),
            "--execute",
        ],
        executor=executor,
    )

    assert code == 0
    assert calls[0]["output_root"] == BENCHMARK_ROOT
    assert calls[0]["baseline_backend"] == "EmulatorExtras"
    output = json.loads(capsys.readouterr().out)
    assert output["suite_report"] == "debug/screencap-benchmarks/suite.json"
    assert "private-mfa-root" not in json.dumps(output)


def test_unsupported_lossless_backend_is_recorded_without_aborting_suite() -> None:
    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    class Controller:
        def __init__(self, clock: Clock) -> None:
            self.clock = clock

        def post_screencap(self):
            clock = self.clock

            class Job:
                def wait(self):
                    clock.value += 0.02
                    return self

                def get(self):
                    return np.zeros((1, 1, 3), dtype=np.uint8)

            return Job()

    clock = Clock()

    @contextmanager
    def controller_factory(backend: str):
        if backend == "RawByNetcat":
            raise RuntimeError("secret device details must not escape")
        yield Controller(clock)

    result = run_benchmark_suite(
        ["RawByNetcat", "EmulatorExtras"],
        rounds=1,
        duration_seconds=0.1,
        frame_timeout_ms=150,
        controller_factory=controller_factory,
        observer_factory=lambda: LatestFrameObserver(clock),
        baseline_backend="EmulatorExtras",
    )

    assert result["candidates"][0]["backend"] == "EmulatorExtras"
    failed = result["candidates"][1]
    assert failed["backend"] == "RawByNetcat"
    assert failed["summary"]["timing_qualified"] is False
    assert failed["error"] == "RuntimeError: controller setup or capture failed"
    assert "secret device" not in json.dumps(result)


def test_previous_controller_is_released_before_next_backend_connects() -> None:
    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    class Controller:
        def __init__(self, clock: Clock) -> None:
            self.clock = clock

        def post_screencap(self):
            clock = self.clock

            class Job:
                def wait(self):
                    clock.value += 0.02
                    return self

                def get(self):
                    return np.zeros((1, 1, 3), dtype=np.uint8)

            return Job()

    clock = Clock()
    previous: weakref.ReferenceType | None = None

    @contextmanager
    def controller_factory(backend: str):
        nonlocal previous
        if previous is not None:
            assert previous() is None, "previous backend controller is still alive"
        controller = Controller(clock)
        previous = weakref.ref(controller)
        yield controller

    result = run_benchmark_suite(
        ["Encode", "RawWithGzip"],
        rounds=1,
        duration_seconds=0.1,
        frame_timeout_ms=150,
        controller_factory=controller_factory,
        observer_factory=lambda: LatestFrameObserver(clock),
        baseline_backend="Encode",
    )

    assert all("error" not in candidate for candidate in result["candidates"])


def test_partial_candidate_can_never_qualify() -> None:
    class Observer:
        calls = 0

        def run(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("capture failed")
            from agent.realtime.frame_observer_core import ObservationStats

            return ObservationStats(
                frames=10,
                elapsed_seconds=0.1,
                effective_fps=100.0,
                capture_mean_ms=5.0,
                capture_p50_ms=5.0,
                capture_p95_ms=5.0,
                maximum_capture_ms=5.0,
                over_100ms_frames=0,
                over_150ms_frames=0,
                timed_out_frames=0,
                invalid_frames=0,
                stopped=False,
            )

    @contextmanager
    def controller_factory(_backend: str):
        yield object()

    observer = Observer()
    result = run_benchmark_suite(
        ["EmulatorExtras"],
        rounds=2,
        duration_seconds=0.1,
        frame_timeout_ms=150,
        controller_factory=controller_factory,
        observer_factory=lambda: observer,
        baseline_backend="EmulatorExtras",
    )

    summary = result["candidates"][0]["summary"]
    assert summary["timing_qualified"] is False
    assert "one or more rounds did not complete" in summary["qualification_reasons"]


def test_candidates_cannot_qualify_against_an_incomplete_baseline() -> None:
    from agent.realtime.frame_observer_core import ObservationStats

    complete = ObservationStats(
        frames=10,
        elapsed_seconds=0.1,
        effective_fps=100.0,
        capture_mean_ms=5.0,
        capture_p50_ms=5.0,
        capture_p95_ms=5.0,
        maximum_capture_ms=5.0,
        over_100ms_frames=0,
        over_150ms_frames=0,
        timed_out_frames=0,
        invalid_frames=0,
        stopped=False,
    )

    class Observer:
        calls = 0

        def run(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("baseline failed")
            return complete

    @contextmanager
    def controller_factory(_backend: str):
        yield object()

    observer = Observer()
    result = run_benchmark_suite(
        ["EmulatorExtras", "Encode"],
        rounds=2,
        duration_seconds=0.1,
        frame_timeout_ms=150,
        controller_factory=controller_factory,
        observer_factory=lambda: observer,
        baseline_backend="EmulatorExtras",
    )

    encode = next(
        candidate
        for candidate in result["candidates"]
        if candidate["backend"] == "Encode"
    )
    assert encode["summary"]["timing_qualified"] is False
    assert (
        "baseline backend did not complete"
        in encode["summary"]["qualification_reasons"]
    )


def test_completed_round_is_persisted_before_a_later_interrupt(
    tmp_path: Path,
) -> None:
    from agent.realtime.frame_observer_core import ObservationStats

    complete = ObservationStats(
        frames=10,
        elapsed_seconds=0.1,
        effective_fps=100.0,
        capture_mean_ms=5.0,
        capture_p50_ms=5.0,
        capture_p95_ms=5.0,
        maximum_capture_ms=5.0,
        over_100ms_frames=0,
        over_150ms_frames=0,
        timed_out_frames=0,
        invalid_frames=0,
        stopped=False,
    )

    class Observer:
        calls = 0

        def run(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 2:
                raise KeyboardInterrupt
            return complete

    @contextmanager
    def controller_factory(_backend: str):
        yield object()

    observer = Observer()
    with pytest.raises(KeyboardInterrupt):
        execute_benchmark(
            ["EmulatorExtras"],
            rounds=2,
            duration_seconds=0.1,
            frame_timeout_ms=150,
            baseline_backend="EmulatorExtras",
            output_root=tmp_path,
            controller_factory=controller_factory,
            observer_factory=lambda: observer,
            started_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
        )

    round_reports = list(tmp_path.glob("screencap-benchmark-*.json"))
    assert len(round_reports) == 1
