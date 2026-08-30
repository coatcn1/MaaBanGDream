"""Benchmark lossless MaaFramework screenshot backends without sending input."""
from __future__ import annotations

import argparse
import gc
import json
import sys
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ContextManager


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
BENCHMARK_ROOT = ROOT / "debug" / "screencap-benchmarks"
DEFAULT_MFA_ROOT = ROOT.parent / ".tools" / "MFAAvalonia-profile-v3"


LOSSLESS_BACKENDS = (
    "EmulatorExtras",
    "RawByNetcat",
    "RawWithGzip",
    "Encode",
    "EncodeToFileAndPull",
)
LOSSY_MINICAP_BACKENDS = {"MinicapDirect", "MinicapStream"}
FORMAL_MIN_ROUNDS = 3
FORMAL_MIN_DURATION_SECONDS = 300.0
FORMAL_WINDOW_REASON = (
    "formal qualification requires at least 3 rounds of 300 seconds"
)


@dataclass(frozen=True, repr=False)
class ControllerSettings:
    adb_path: str
    adb_serial: str
    input_methods: int
    config: dict[str, object]
    agent_path: Path

    def __repr__(self) -> str:
        return "ControllerSettings(<redacted>)"


def load_controller_settings(
    instance_config: Path,
    *,
    mfa_root: Path,
) -> ControllerSettings:
    payload = json.loads(instance_config.read_text(encoding="utf-8-sig"))
    device = payload.get("AdbDevice")
    if not isinstance(device, dict):
        raise ValueError("MFA instance does not contain an ADB device")
    raw_config = device.get("Config", {})
    config = json.loads(raw_config) if isinstance(raw_config, str) else raw_config
    if not isinstance(config, dict):
        raise ValueError("MFA ADB device config must be a JSON object")
    adb_path = str(device.get("AdbPath", ""))
    adb_serial = str(device.get("AdbSerial", ""))
    if not adb_path or not adb_serial:
        raise ValueError("MFA instance ADB device is incomplete")
    return ControllerSettings(
        adb_path=adb_path,
        adb_serial=adb_serial,
        input_methods=int(device.get("InputMethods", 0)),
        config=config,
        agent_path=mfa_root / "libs" / "MaaAgentBinary",
    )


def build_controller_factory(
    settings: ControllerSettings,
    *,
    controller_type=None,
    method_values: dict[str, int] | None = None,
) -> Callable[[str], ContextManager[Any]]:
    if controller_type is None or method_values is None:
        from maa.controller import AdbController
        from maa.define import MaaAdbScreencapMethodEnum

        controller_type = controller_type or AdbController
        method_values = method_values or {
            name: int(getattr(MaaAdbScreencapMethodEnum, name))
            for name in LOSSLESS_BACKENDS
        }

    @contextmanager
    def create(backend: str):
        if backend not in LOSSLESS_BACKENDS or backend not in method_values:
            raise ValueError("backend is not in the verified lossless whitelist")
        controller = controller_type(
            adb_path=settings.adb_path,
            address=settings.adb_serial,
            screencap_methods=method_values[backend],
            input_methods=settings.input_methods,
            config=settings.config,
            agent_path=settings.agent_path,
        )
        try:
            connection = controller.post_connection().wait()
            if not connection.succeeded or not controller.connected:
                raise RuntimeError(f"{backend} controller connection failed")
            yield controller
        finally:
            del controller
            gc.collect()

    return create


def write_suite_report(
    root: Path,
    result: dict[str, object],
    *,
    started_at: datetime,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    utc_started = started_at.astimezone(timezone.utc)
    stamp = utc_started.strftime("%Y%m%d-%H%M%S-%f")
    path = root / f"screencap-benchmark-suite-{stamp}.json"
    payload = {
        "schema_version": 1,
        "started_at": utc_started.isoformat().replace("+00:00", "Z"),
        "backends": result["backends"],
        "rounds": result["rounds"],
        "duration_seconds": result["duration_seconds"],
        "baseline_backend": result["baseline_backend"],
        "candidates": result["candidates"],
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def run_benchmark_suite(
    backends: list[str],
    *,
    rounds: int,
    duration_seconds: float,
    frame_timeout_ms: int,
    controller_factory: Callable[[str], ContextManager[Any]],
    observer_factory: Callable[[], Any],
    baseline_backend: str | None = None,
    on_round_completed: (
        Callable[[str, int, dict[str, object]], str | None] | None
    ) = None,
) -> dict[str, object]:
    formal_window = (
        rounds >= FORMAL_MIN_ROUNDS
        and duration_seconds >= FORMAL_MIN_DURATION_SECONDS
    )
    candidates: list[dict[str, object]] = []
    for backend in backends:
        round_results: list[dict[str, object]] = []
        candidate: dict[str, object] = {
            "backend": backend,
            "rounds": round_results,
        }
        controller = None
        try:
            with controller_factory(backend) as controller:
                for round_index in range(1, rounds + 1):
                    stats = observer_factory().run(
                        lambda: controller.post_screencap().wait().get(),
                        lambda: False,
                        duration_seconds=duration_seconds,
                        frame_timeout_ms=frame_timeout_ms,
                    )
                    round_result = asdict(stats)
                    if on_round_completed is not None:
                        artifact = on_round_completed(
                            backend, round_index, round_result
                        )
                        if artifact is not None:
                            round_result["artifact"] = artifact
                    round_results.append(round_result)
        except Exception as exc:
            candidate["error"] = (
                f"{type(exc).__name__}: controller setup or capture failed"
            )
        finally:
            controller = None
            gc.collect()
        candidates.append(candidate)
    baseline_name = baseline_backend or backends[0]
    by_backend = {candidate["backend"]: candidate for candidate in candidates}
    if baseline_name not in by_backend:
        raise ValueError("baseline backend must be included in the benchmark plan")
    baseline_candidate = by_backend[baseline_name]
    baseline_rounds = baseline_candidate["rounds"]
    baseline_complete = (
        len(baseline_rounds) == rounds
        and "error" not in baseline_candidate
        and all(
            not bool(result["stopped"])
            and int(result["frames"]) > 0
            and (
                not formal_window
                or float(result["elapsed_seconds"])
                + 1e-6 >= FORMAL_MIN_DURATION_SECONDS
            )
            for result in baseline_rounds
        )
    )
    baseline_p95_ms = (
        max(float(result["capture_p95_ms"]) for result in baseline_rounds)
        if baseline_complete else None
    )
    for candidate in candidates:
        results = candidate["rounds"]
        if not results:
            reasons = ["controller setup or capture failed"]
            if not formal_window:
                reasons.append(FORMAL_WINDOW_REASON)
            if baseline_p95_ms is None:
                reasons.append("baseline backend did not complete")
            candidate["summary"] = {
                "maximum_capture_ms": 0.0,
                "capture_p95_ms": 0.0,
                "capture_mean_ms": 0.0,
                "over_150ms_frames": 0,
                "invalid_frames": 0,
                "timing_qualified": False,
                "qualification_reasons": reasons,
            }
            continue
        maximum = max(float(result["maximum_capture_ms"]) for result in results)
        p95 = max(float(result["capture_p95_ms"]) for result in results)
        weights = [
            int(result["frames"]) + int(result["invalid_frames"])
            for result in results
        ]
        total_weight = sum(weights)
        mean = (
            sum(
                float(result["capture_mean_ms"]) * weight
                for result, weight in zip(results, weights)
            ) / total_weight
            if total_weight else 0.0
        )
        over_150ms = sum(int(result["over_150ms_frames"]) for result in results)
        invalid = sum(int(result["invalid_frames"]) for result in results)
        reasons = [] if formal_window else [FORMAL_WINDOW_REASON]
        if maximum > 150:
            reasons.append("capture exceeded 150 ms")
        if baseline_p95_ms is None:
            reasons.append("baseline backend did not complete")
        elif p95 > baseline_p95_ms + 5:
            reasons.append("capture P95 regressed by more than 5 ms")
        if invalid:
            reasons.append("invalid screenshots observed")
        if formal_window and any(
            float(result["elapsed_seconds"]) + 1e-6
            < FORMAL_MIN_DURATION_SECONDS
            for result in results
        ):
            reasons.append("one or more rounds did not cover 300 seconds")
        if (
            len(results) != rounds
            or "error" in candidate
            or any(
                bool(result["stopped"]) or int(result["frames"]) == 0
                for result in results
            )
        ):
            reasons.append("one or more rounds did not complete")
        candidate["summary"] = {
            "maximum_capture_ms": round(maximum, 3),
            "capture_p95_ms": round(p95, 3),
            "capture_mean_ms": round(mean, 3),
            "over_150ms_frames": over_150ms,
            "invalid_frames": invalid,
            "timing_qualified": not reasons,
            "qualification_reasons": reasons,
        }
    candidates.sort(key=lambda candidate: (
        not bool(candidate["summary"]["timing_qualified"]),
        "error" in candidate,
        float(candidate["summary"]["maximum_capture_ms"]),
        float(candidate["summary"]["capture_p95_ms"]),
        float(candidate["summary"]["capture_mean_ms"]),
        str(candidate["backend"]),
    ))
    return {"candidates": candidates}


def execute_benchmark(
    backends: list[str],
    *,
    rounds: int,
    duration_seconds: float,
    frame_timeout_ms: int,
    baseline_backend: str,
    output_root: Path,
    instance_config: Path | None = None,
    mfa_root: Path | None = None,
    controller_factory: Callable[[str], ContextManager[Any]] | None = None,
    observer_factory: Callable[[], Any] | None = None,
    started_at: datetime | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[Path, dict[str, object]]:
    from agent.realtime.frame_observer_core import (
        LatestFrameObserver,
        ObservationStats,
        write_observation_report,
    )

    if controller_factory is None:
        if instance_config is None or mfa_root is None:
            raise ValueError("MFA root and instance config are required")
        settings = load_controller_settings(instance_config, mfa_root=mfa_root)
        controller_factory = build_controller_factory(settings)
    observer_factory = observer_factory or LatestFrameObserver
    started_at = started_at or datetime.now(timezone.utc)
    artifact_index = 0

    def persist_round(
        backend: str,
        round_index: int,
        round_result: dict[str, object],
    ) -> str:
        nonlocal artifact_index
        artifact_index += 1
        stats = ObservationStats(**round_result)
        round_path = write_observation_report(
            output_root,
            stats,
            method_label=f"{backend}-round-{round_index}",
            started_at=started_at + timedelta(microseconds=artifact_index),
        )
        if progress is not None:
            progress(
                f"completed backend={backend} round={round_index}/{rounds} "
                f"report={round_path.name}"
            )
        return round_path.name

    result = run_benchmark_suite(
        backends,
        rounds=rounds,
        duration_seconds=duration_seconds,
        frame_timeout_ms=frame_timeout_ms,
        baseline_backend=baseline_backend,
        controller_factory=controller_factory,
        observer_factory=observer_factory,
        on_round_completed=persist_round,
    )
    result.update({
        "backends": list(backends),
        "rounds": rounds,
        "duration_seconds": duration_seconds,
        "baseline_backend": baseline_backend,
    })
    suite_path = write_suite_report(output_root, result, started_at=started_at)
    return suite_path, result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend",
        action="append",
        required=True,
    )
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--duration-seconds", type=float, default=300.0)
    parser.add_argument("--frame-timeout-ms", type=int, default=150)
    parser.add_argument("--baseline-backend", default=None)
    parser.add_argument("--mfa-root", type=Path, default=DEFAULT_MFA_ROOT)
    parser.add_argument("--instance-config", type=Path, default=None)
    parser.add_argument("--execute", action="store_true")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    executor: Callable[..., tuple[Path, dict[str, object]]] = execute_benchmark,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if any(name in LOSSY_MINICAP_BACKENDS for name in args.backend):
        parser.error("lossy Minicap backend is forbidden")
    unknown = [name for name in args.backend if name not in LOSSLESS_BACKENDS]
    if unknown:
        parser.error(
            "backend is not in the verified lossless whitelist: "
            + ", ".join(unknown)
        )
    if not 1 <= args.rounds <= 20:
        parser.error("rounds must be in 1..20")
    if not 0.1 <= args.duration_seconds <= 900:
        parser.error("duration-seconds must be in 0.1..900")
    if not 50 <= args.frame_timeout_ms <= 5000:
        parser.error("frame-timeout-ms must be in 50..5000")
    baseline_backend = args.baseline_backend or args.backend[0]
    if baseline_backend not in args.backend:
        parser.error("baseline backend must be included in --backend")
    if not args.execute:
        print(json.dumps({
            "mode": "dry-run",
            "backends": args.backend,
            "rounds": args.rounds,
            "duration_seconds": args.duration_seconds,
            "frame_timeout_ms": args.frame_timeout_ms,
        }))
        return 0
    instance_config = (
        args.instance_config
        if args.instance_config is not None
        else args.mfa_root / "config" / "instances" / "default.json"
    )
    suite_path, result = executor(
        args.backend,
        rounds=args.rounds,
        duration_seconds=args.duration_seconds,
        frame_timeout_ms=args.frame_timeout_ms,
        baseline_backend=baseline_backend,
        output_root=BENCHMARK_ROOT,
        instance_config=instance_config,
        mfa_root=args.mfa_root,
        progress=lambda message: print(message, file=sys.stderr, flush=True),
    )
    print(json.dumps({
        "mode": "completed",
        "suite_report": suite_path.relative_to(ROOT).as_posix(),
        "candidates": [
            {
                "backend": candidate["backend"],
                "summary": candidate["summary"],
            }
            for candidate in result["candidates"]
        ],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
