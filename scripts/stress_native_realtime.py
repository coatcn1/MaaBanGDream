"""Native 滚动演奏链路的真实墙钟虚拟设备压力工具。

本工具只产生主机侧、虚拟设备侧证据，不连接 Android，也不能替代真机验收。
默认分别在空闲与外部 CPU 压力下运行 120 秒；短 smoke 可通过 ``--duration``
和放宽门槛显式触发。
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import multiprocessing as mp
import os
import queue
import sys
import time
import traceback
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NATIVE_DIR = PROJECT_ROOT / "agent" / "realtime" / "native"
LANE_CENTERS = (190.0, 340.0, 490.0, 640.0, 790.0, 940.0, 1090.0)
EVIDENCE_SCOPE = "virtual_device_only"
EVIDENCE_WARNING = (
    "仅为主机真实墙钟与虚拟 minitouch 执行器证据；不是 Android 真机验收。"
)


@dataclass(frozen=True)
class StressConfig:
    """单个空闲或 CPU 压力场景的配置。"""

    duration_s: float = 120.0
    mode: str = "idle"
    lookahead_s: float = 0.500
    low_water_s: float = 0.200
    max_queue_s: float = 0.750
    reset_timeout_s: float = 0.100
    cancel_deadline_s: float = 0.500
    action_interval_s: float = 0.050
    startup_lead_s: float = 0.100
    poll_interval_s: float = 0.001
    p95_limit_ms: float = 3.0
    max_drift_limit_ms: float = 8.0
    cancel_limit_ms: float = 500.0
    pressure_workers: int = 1

    def __post_init__(self) -> None:
        if self.mode not in {"idle", "pressure"}:
            raise ValueError(f"未知压力模式：{self.mode}")
        finite_positive = {
            "duration_s": self.duration_s,
            "lookahead_s": self.lookahead_s,
            "low_water_s": self.low_water_s,
            "max_queue_s": self.max_queue_s,
            "reset_timeout_s": self.reset_timeout_s,
            "cancel_deadline_s": self.cancel_deadline_s,
            "action_interval_s": self.action_interval_s,
            "startup_lead_s": self.startup_lead_s,
            "poll_interval_s": self.poll_interval_s,
            "p95_limit_ms": self.p95_limit_ms,
            "max_drift_limit_ms": self.max_drift_limit_ms,
            "cancel_limit_ms": self.cancel_limit_ms,
        }
        for name, value in finite_positive.items():
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} 必须是有限正数")
        if not self.low_water_s < self.lookahead_s <= self.max_queue_s:
            raise ValueError(
                "水位必须满足 low_water < lookahead <= max_queue"
            )
        if not self.reset_timeout_s <= self.cancel_deadline_s:
            raise ValueError("reset_timeout 不能大于 cancel_deadline")
        if self.pressure_workers < 1:
            raise ValueError("pressure_workers 必须至少为 1")


def load_native_module() -> Any:
    """加载已部署 pyd，并拒绝缺少滚动压力接口的旧产物。"""

    native_path = str(
        Path(os.environ.get("MBDR_NATIVE_MODULE_DIR", NATIVE_DIR)).resolve()
    )
    if native_path not in sys.path:
        sys.path.insert(0, native_path)
    try:
        native = importlib.import_module("maabangdream_realtime")
    except Exception as exc:  # pragma: no cover - 取决于本机构建状态。
        raise RuntimeError(
            "Native 模块未构建；请先运行 scripts/build_native_realtime.ps1"
        ) from exc
    missing = [
        name
        for name in ("PlaybackSession", "TouchScriptCompiler")
        if not hasattr(native, name)
    ]
    if not missing:
        compiler = native.TouchScriptCompiler()
        if not hasattr(compiler, "execution_receipts"):
            missing.append("TouchScriptCompiler.execution_receipts")
    if missing:
        raise RuntimeError(
            "已部署 pyd 过旧，缺少公开压力接口：" + ", ".join(missing)
        )
    return native


def _action(
    kind: str,
    due_s: float,
    lane: int,
    note_index: int,
    *,
    contact: int = -1,
    direction: str | None = None,
) -> dict[str, object]:
    return {
        "kind": kind,
        "lane": lane,
        "contact": contact,
        "target_x": LANE_CENTERS[lane],
        "due_s": round(due_s, 9),
        "note_index": note_index,
        "flick_direction": direction,
    }


def _build_actions(config: StressConfig) -> list[dict[str, object]]:
    """生成包含 TAP、FLICK、和弦及完整 HOLD 生命周期的确定性负载。"""

    actions: list[dict[str, object]] = []
    note_index = 0
    last_judgement_s = max(0.0, config.duration_s - 0.100)
    tick = 0
    while True:
        due_s = tick * config.action_interval_s
        if due_s > last_judgement_s + 1e-9:
            break
        lane = tick % len(LANE_CENTERS)
        if tick % 11 == 0:
            actions.append(
                _action(
                    "flick",
                    due_s,
                    lane,
                    note_index,
                    direction="Left" if tick % 22 == 0 else "Right",
                )
            )
        else:
            actions.append(_action("tap", due_s, lane, note_index))
        note_index += 1
        if tick > 0 and tick % 13 == 0:
            chord_lane = (lane + 3) % len(LANE_CENTERS)
            actions.append(_action("tap", due_s, chord_lane, note_index))
            note_index += 1
        tick += 1

    hold_start_s = 0.200
    hold_index = 0
    while hold_start_s + 0.240 <= config.duration_s - 0.020:
        contact = hold_index % 2
        first_lane = (hold_index * 2 + 1) % len(LANE_CENTERS)
        last_lane = (first_lane + 2) % len(LANE_CENTERS)
        actions.extend(
            (
                _action(
                    "down",
                    hold_start_s,
                    first_lane,
                    note_index,
                    contact=contact,
                ),
                _action(
                    "move",
                    hold_start_s + 0.120,
                    last_lane,
                    note_index,
                    contact=contact,
                ),
                _action(
                    "up",
                    hold_start_s + 0.240,
                    last_lane,
                    note_index,
                    contact=contact,
                ),
            )
        )
        note_index += 1
        hold_index += 1
        hold_start_s += 1.000

    final_due_s = max(0.0, config.duration_s - 0.050)
    if not any(abs(float(item["due_s"]) - final_due_s) <= 1e-9 for item in actions):
        actions.append(
            _action(
                "tap",
                final_due_s,
                note_index % len(LANE_CENTERS),
                note_index,
            )
        )
    actions.sort(key=lambda item: float(item["due_s"]))
    return actions


def _interruptible_wait(
    target_s: float,
    reset_event: Any,
    shutdown_event: Any,
) -> str:
    """等待绝对时刻，并保证 reset/关闭不会被长 w 命令阻塞。"""

    while True:
        if reset_event.is_set():
            return "reset"
        if shutdown_event.is_set():
            return "shutdown"
        remaining_s = target_s - time.perf_counter()
        if remaining_s <= 0.0:
            return "deadline"
        if remaining_s > 0.003:
            reset_event.wait(timeout=min(remaining_s - 0.002, 0.010))
            continue
        # 最后 2ms 只检查单调时钟，避免普通 sleep 粒度污染被测漂移。


def _put_event(event_queue: Any, payload: dict[str, object]) -> None:
    try:
        event_queue.put(payload, timeout=1.0)
    except Exception:
        # 主进程已经退出时不再让虚拟设备因诊断队列阻塞。
        return


def _virtual_device_main(
    command_queue: Any,
    event_queue: Any,
    reset_event: Any,
    shutdown_event: Any,
) -> None:
    """在独立进程中按真实墙钟消费 minitouch c/w/d/m/u。"""

    active_contacts: set[int] = set()
    pending: list[tuple[str, int, dict[str, object] | None]] = []
    command_counts = {command: 0 for command in ("c", "w", "d", "m", "u")}
    protocol_errors: list[str] = []
    cursor_s: float | None = None
    expecting_continuation = False
    underflow_latched = False
    underflows = 0
    max_enqueued_depth_ms = 0.0
    cancelled = False

    def acknowledge_reset() -> None:
        nonlocal cancelled
        released = len(active_contacts)
        active_contacts.clear()
        pending.clear()
        reset_event.clear()
        cancelled = True
        _put_event(
            event_queue,
            {
                "type": "reset_ack",
                "actual_s": time.perf_counter(),
                "released_contacts": released,
            },
        )

    while not shutdown_event.is_set():
        if reset_event.is_set():
            acknowledge_reset()
        try:
            message = command_queue.get(timeout=0.002)
        except queue.Empty:
            if (
                expecting_continuation
                and cursor_s is not None
                and time.perf_counter() > cursor_s + 0.0005
                and not underflow_latched
            ):
                underflows += 1
                underflow_latched = True
                _put_event(event_queue, {"type": "underflow"})
            continue
        except (EOFError, OSError):
            break

        kind = str(message.get("type", ""))
        if kind == "stop":
            break
        if kind != "chunk" or cancelled:
            continue

        underflow_latched = False
        expecting_continuation = False
        window_start_s = float(message["window_start_s"])
        window_end_s = float(message["window_end_s"])
        enqueued_at_s = float(message["enqueued_at_s"])
        max_enqueued_depth_ms = max(
            max_enqueued_depth_ms,
            max(0.0, window_end_s - enqueued_at_s) * 1000.0,
        )
        if cursor_s is None:
            cursor_s = window_start_s
        elif abs(cursor_s - window_start_s) > 0.010:
            protocol_errors.append(
                "chunk window 与设备相对时钟相差超过 10ms"
            )

        receipts = {
            int(item["line_index"]): dict(item)
            for item in list(message.get("receipts", []))
        }
        aborted = False
        for line_index, raw_line in enumerate(list(message["lines"])):
            if reset_event.is_set():
                acknowledge_reset()
                aborted = True
                break
            parts = str(raw_line).strip().split()
            if not parts:
                continue
            command = parts[0]
            if command == "w":
                command_counts["w"] += 1
                if pending:
                    protocol_errors.append("w 前存在未 commit 的触控命令")
                cursor_s += int(parts[1]) / 1000.0
                wait_result = _interruptible_wait(
                    cursor_s, reset_event, shutdown_event
                )
                if wait_result == "reset":
                    acknowledge_reset()
                    aborted = True
                    break
                if wait_result == "shutdown":
                    aborted = True
                    break
                continue
            if command in {"d", "m", "u"}:
                command_counts[command] += 1
                contact = int(parts[1])
                receipt = receipts.get(line_index)
                if receipt is not None and str(receipt["command"]) != command:
                    protocol_errors.append("receipt command 与脚本行不一致")
                pending.append((command, contact, receipt))
                continue
            if command != "c":
                protocol_errors.append(f"未知虚拟设备命令：{command}")
                continue

            command_counts["c"] += 1
            wait_result = _interruptible_wait(
                cursor_s, reset_event, shutdown_event
            )
            if wait_result == "reset":
                acknowledge_reset()
                aborted = True
                break
            if wait_result == "shutdown":
                aborted = True
                break
            actual_s = time.perf_counter()
            seen_operations: set[tuple[str, int]] = set()
            for operation, contact, receipt in pending:
                identity = (operation, contact)
                if identity in seen_operations:
                    protocol_errors.append(
                        "同一 commit 含重复触点操作："
                        f"{operation} {contact}"
                    )
                seen_operations.add(identity)
                if operation == "d":
                    if contact in active_contacts:
                        protocol_errors.append(f"重复 DOWN：{contact}")
                    active_contacts.add(contact)
                elif operation == "m":
                    if contact not in active_contacts:
                        protocol_errors.append(f"悬空 MOVE：{contact}")
                else:
                    if contact not in active_contacts:
                        protocol_errors.append(f"悬空 UP：{contact}")
                    active_contacts.discard(contact)
                if receipt is not None:
                    _put_event(
                        event_queue,
                        {
                            "type": "execution",
                            "planned_engine_s": float(
                                receipt["planned_engine_s"]
                            ),
                            "actual_engine_s": actual_s,
                            "action_token": int(receipt["action_token"]),
                            "command": operation,
                        },
                    )
            pending.clear()

        if aborted:
            continue
        final_chunk = bool(message["final_chunk"])
        expecting_continuation = not final_chunk
        if final_chunk:
            _put_event(
                event_queue,
                {
                    "type": "final",
                    "active_contacts": len(active_contacts),
                    "pending_commands": len(pending),
                    "underflows": underflows,
                    "max_enqueued_depth_ms": max_enqueued_depth_ms,
                    "command_counts": command_counts,
                    "protocol_errors": protocol_errors,
                },
            )


def _cpu_pressure_worker(stop_event: Any, seed: int) -> None:
    """独立进程持续占用 CPU，避免 GIL 内循环伪造压力。"""

    value = (seed + 1) * 0x9E3779B1
    while not stop_event.is_set():
        for _ in range(100_000):
            value = ((value << 7) ^ (value >> 3) ^ 0xA5A5A5A5) & 0xFFFFFFFF


class _ChunkPublisher:
    """把公开 PlaybackChunk 编译后送入独立虚拟设备进程。"""

    def __init__(self, native: Any, command_queue: Any) -> None:
        self._compiler = native.TouchScriptCompiler()
        self._command_queue = command_queue
        self.error: str | None = None
        self.max_enqueued_depth_ms = 0.0

    def __call__(self, chunk: dict[str, object]) -> bool:
        try:
            actions: list[dict[str, object]] = []
            for entry_value in list(chunk["actions"]):
                entry = dict(entry_value)
                action = dict(entry["action"])
                action["due_s"] = float(entry["engine_due_s"])
                actions.append(action)
            future_down_reservations: list[dict[str, object]] = []
            for entry_value in list(
                chunk.get("future_down_reservations", [])
            ):
                entry = dict(entry_value)
                action = dict(entry["action"])
                action["due_s"] = float(entry["engine_due_s"])
                future_down_reservations.append(action)
            lines = list(
                self._compiler.compile(
                    actions,
                    dict(chunk["touch_config"]),
                    float(chunk["window_start_s"]),
                    bool(chunk["final_chunk"]),
                    float(chunk["window_end_s"]),
                    future_down_reservations,
                )
            )
            receipts = [
                dict(item) for item in self._compiler.execution_receipts()
            ]
            for receipt in receipts:
                line_index = int(receipt["line_index"])
                if not 0 <= line_index < len(lines):
                    raise RuntimeError("execution receipt line_index 越界")
                command = str(lines[line_index]).lstrip()[:1]
                if command != str(receipt["command"]):
                    raise RuntimeError("execution receipt 与脚本命令不一致")
            enqueued_at_s = time.perf_counter()
            self.max_enqueued_depth_ms = max(
                self.max_enqueued_depth_ms,
                max(0.0, float(chunk["window_end_s"]) - enqueued_at_s)
                * 1000.0,
            )
            self._command_queue.put(
                {
                    "type": "chunk",
                    "sequence": int(chunk["sequence"]),
                    "window_start_s": float(chunk["window_start_s"]),
                    "window_end_s": float(chunk["window_end_s"]),
                    "final_chunk": bool(chunk["final_chunk"]),
                    "enqueued_at_s": enqueued_at_s,
                    "lines": lines,
                    "receipts": receipts,
                },
                timeout=0.100,
            )
            return True
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return False


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[min(rank - 1, len(ordered) - 1)]


def _drain_execution_events(
    event_queue: Any,
    session: Any,
    state: dict[str, object],
) -> None:
    while True:
        try:
            event = dict(event_queue.get_nowait())
        except queue.Empty:
            return
        event_type = str(event.get("type", ""))
        if event_type == "execution":
            planned_s = float(event["planned_engine_s"])
            actual_s = float(event["actual_engine_s"])
            token = int(event["action_token"])
            tokens = state["tokens"]
            if token in tokens:
                state["errors"].append(f"重复 action_token：{token}")
                continue
            tokens.add(token)
            if not bool(session.observe_execution(planned_s, actual_s, 1)):
                state["errors"].append("PlaybackSession 拒绝执行回执")
                continue
            state["drifts_ms"].append(abs(actual_s - planned_s) * 1000.0)
        elif event_type == "underflow":
            state["device_underflows"] += 1
        elif event_type == "final":
            state["final"] = event
        elif event_type == "reset_ack":
            state["reset_ack"] = event
        else:
            state["errors"].append(f"未知虚拟设备事件：{event_type}")


def _stop_process(process: Any, shutdown_event: Any, command_queue: Any) -> None:
    shutdown_event.set()
    try:
        command_queue.put_nowait({"type": "stop"})
    except Exception:
        pass
    process.join(timeout=2.0)
    if process.is_alive():
        process.terminate()
        process.join(timeout=2.0)


def _run_playback_workload(
    config: StressConfig,
    native: Any,
    context: Any,
) -> dict[str, object]:
    command_queue = context.Queue(maxsize=64)
    event_queue = context.Queue(maxsize=32768)
    reset_event = context.Event()
    shutdown_event = context.Event()
    device = context.Process(
        target=_virtual_device_main,
        args=(command_queue, event_queue, reset_event, shutdown_event),
        name="mbdr-virtual-minitouch",
    )
    device.start()
    publisher = _ChunkPublisher(native, command_queue)
    session = native.PlaybackSession(
        publish=publisher,
        request_reset=None,
        fallback_stop=None,
        clock=time.perf_counter,
        config={
            "lookahead_s": config.lookahead_s,
            "low_water_s": config.low_water_s,
            "max_queue_s": config.max_queue_s,
            "reset_timeout_s": config.reset_timeout_s,
            "cancel_deadline_s": config.cancel_deadline_s,
        },
    )
    actions = _build_actions(config)
    engine_config = {
        "judgement_y": 565.0,
        "press_bias_ms": 0,
        "max_wait_ms": 250,
        "tap_duration_ms": 50,
        "flick_duration_ms": 80,
        "slide_step_s": 0.010,
        "song_offset_s": 0.0,
        "lane_centers": list(LANE_CENTERS),
    }
    state: dict[str, object] = {
        "tokens": set(),
        "drifts_ms": [],
        "errors": [],
        "device_underflows": 0,
        "final": None,
        "reset_ack": None,
    }
    started_s = time.perf_counter()
    try:
        if not bool(session.arm(actions, engine_config)):
            raise RuntimeError(f"PlaybackSession arm 失败：{session.report()}")
        anchor_s = time.perf_counter() + config.startup_lead_s
        if not bool(session.start(anchor_s)):
            raise RuntimeError(f"PlaybackSession start 失败：{session.report()}")
        timeout_s = anchor_s + config.duration_s + 5.0
        while time.perf_counter() < timeout_s:
            session.publish()
            _drain_execution_events(event_queue, session, state)
            report = dict(session.report())
            final_event = state["final"]
            if (
                final_event is not None
                and int(report["sent"]) == int(report["planned"])
                and int(report["executed"]) == int(report["planned"])
            ):
                if not bool(session.finish("virtual stress completed")):
                    raise RuntimeError("PlaybackSession 拒绝正常 finish")
                break
            playback_state = str(session.poll())
            if playback_state == "failed":
                publisher_detail = (
                    f"；publisher={publisher.error}"
                    if publisher.error is not None
                    else ""
                )
                raise RuntimeError(
                    "PlaybackSession 失败："
                    f"{session.report()}{publisher_detail}"
                )
            if publisher.error is not None:
                raise RuntimeError(f"切片发布失败：{publisher.error}")
            time.sleep(config.poll_interval_s)
        else:
            raise TimeoutError("虚拟设备墙钟压力运行超时")

        # multiprocessing.Queue 的 feeder 可能略晚于 final 事件到达。
        _drain_execution_events(event_queue, session, state)
        report = dict(session.report())
        final_event = dict(state["final"] or {})
        drifts_ms = list(state["drifts_ms"])
        return {
            "elapsed_wall_s": time.perf_counter() - started_s,
            "planned_actions": int(report["planned"]),
            "sent_actions": int(report["sent"]),
            "executed_actions": int(report["executed"]),
            "chunks": int(report["chunks"]),
            "session_underflows": int(report["underflows"]),
            "device_underflows": int(state["device_underflows"]),
            "queue_underflows": int(report["underflows"])
            + int(state["device_underflows"]),
            "max_queue_depth_ms": max(
                float(report["max_queue_depth_ms"]),
                publisher.max_enqueued_depth_ms,
                float(final_event.get("max_enqueued_depth_ms", 0.0)),
            ),
            "drift_samples": len(drifts_ms),
            "drift_p50_ms": _percentile(drifts_ms, 0.50),
            "drift_p95_ms": _percentile(drifts_ms, 0.95),
            "drift_max_ms": max(drifts_ms, default=0.0),
            "active_contacts_at_end": int(
                final_event.get("active_contacts", -1)
            ),
            "pending_commands_at_end": int(
                final_event.get("pending_commands", -1)
            ),
            "command_counts": dict(final_event.get("command_counts", {})),
            "protocol_errors": list(final_event.get("protocol_errors", []))
            + list(state["errors"]),
            "native_report": report,
        }
    finally:
        _stop_process(device, shutdown_event, command_queue)


def _run_cancel_probe(
    config: StressConfig,
    native: Any,
    context: Any,
) -> dict[str, object]:
    command_queue = context.Queue(maxsize=8)
    event_queue = context.Queue(maxsize=128)
    reset_event = context.Event()
    shutdown_event = context.Event()
    device = context.Process(
        target=_virtual_device_main,
        args=(command_queue, event_queue, reset_event, shutdown_event),
        name="mbdr-virtual-cancel-probe",
    )
    device.start()
    publisher = _ChunkPublisher(native, command_queue)

    def request_reset() -> bool:
        reset_event.set()
        return False

    def fallback_stop() -> bool:
        shutdown_event.set()
        return True

    session = native.PlaybackSession(
        publish=publisher,
        request_reset=request_reset,
        fallback_stop=fallback_stop,
        clock=time.perf_counter,
        config={
            "lookahead_s": config.lookahead_s,
            "low_water_s": config.low_water_s,
            "max_queue_s": config.max_queue_s,
            "reset_timeout_s": config.reset_timeout_s,
            "cancel_deadline_s": config.cancel_deadline_s,
        },
    )
    actions = [
        _action("down", 0.0, 1, 1, contact=0),
        _action("up", 5.0, 1, 1, contact=0),
    ]
    state: dict[str, object] = {
        "tokens": set(),
        "drifts_ms": [],
        "errors": [],
        "device_underflows": 0,
        "final": None,
        "reset_ack": None,
    }
    try:
        if not bool(session.arm(actions, {})):
            raise RuntimeError("取消探针 arm 失败")
        if not bool(session.start(time.perf_counter() + 0.050)):
            raise RuntimeError("取消探针 start 失败")
        session.publish()
        down_deadline_s = time.perf_counter() + 1.0
        while time.perf_counter() < down_deadline_s:
            _drain_execution_events(event_queue, session, state)
            if state["tokens"]:
                break
            time.sleep(config.poll_interval_s)
        else:
            raise TimeoutError("取消探针未观察到 HOLD DOWN")

        cancel_started_s = time.perf_counter()
        if not bool(session.cancel("virtual cancel probe")):
            raise RuntimeError("取消探针 cancel 失败")
        cancel_deadline_s = cancel_started_s + config.cancel_deadline_s + 1.0
        while time.perf_counter() < cancel_deadline_s:
            _drain_execution_events(event_queue, session, state)
            reset_ack = state["reset_ack"]
            if reset_ack is not None:
                if not bool(session.acknowledge_reset()):
                    raise RuntimeError("PlaybackSession 拒绝 reset 确认")
                local_latency_ms = max(
                    0.0,
                    float(reset_ack["actual_s"]) - cancel_started_s,
                ) * 1000.0
                report = dict(session.report())
                return {
                    "state": str(session.state()),
                    "released_contacts": int(
                        reset_ack["released_contacts"]
                    ),
                    "device_release_latency_ms": local_latency_ms,
                    "session_stop_latency_ms": float(
                        report["stop_latency_ms"]
                    ),
                    "max_release_latency_ms": max(
                        local_latency_ms,
                        float(report["stop_latency_ms"]),
                    ),
                    "fallback_used": bool(report["fallback_used"]),
                    "errors": list(state["errors"]),
                }
            if str(session.poll()) == "failed":
                raise RuntimeError(f"取消探针失败：{session.report()}")
            time.sleep(config.poll_interval_s)
        raise TimeoutError("取消探针超过截止时间")
    finally:
        _stop_process(device, shutdown_event, command_queue)


def _evaluate_gates(
    config: StressConfig,
    metrics: dict[str, object],
    cancel_probe: dict[str, object],
) -> list[str]:
    violations: list[str] = []
    if int(metrics["executed_actions"]) != int(metrics["planned_actions"]):
        violations.append("executed_actions != planned_actions")
    if int(metrics["drift_samples"]) != int(metrics["planned_actions"]):
        violations.append("drift_samples != planned_actions")
    if float(metrics["drift_p95_ms"]) > config.p95_limit_ms:
        violations.append(
            f"drift_p95_ms > {config.p95_limit_ms:.3f}"
        )
    if float(metrics["drift_max_ms"]) > config.max_drift_limit_ms:
        violations.append(
            f"drift_max_ms > {config.max_drift_limit_ms:.3f}"
        )
    if int(metrics["queue_underflows"]) != 0:
        violations.append("queue_underflows != 0")
    if float(metrics["max_queue_depth_ms"]) > config.max_queue_s * 1000 + 1.0:
        violations.append("max_queue_depth_ms 超过配置上限")
    if int(metrics["active_contacts_at_end"]) != 0:
        violations.append("虚拟设备结束时仍有 active contact")
    if int(metrics["pending_commands_at_end"]) != 0:
        violations.append("虚拟设备结束时仍有 pending command")
    if list(metrics["protocol_errors"]):
        violations.append("虚拟 minitouch 协议状态错误")
    command_counts = dict(metrics["command_counts"])
    missing_commands = [
        command
        for command in ("c", "w", "d", "m", "u")
        if int(command_counts.get(command, 0)) <= 0
    ]
    if missing_commands:
        violations.append("未覆盖命令：" + ",".join(missing_commands))
    if int(cancel_probe["released_contacts"]) < 1:
        violations.append("取消探针未释放 active contact")
    if float(cancel_probe["max_release_latency_ms"]) > config.cancel_limit_ms:
        violations.append(
            f"cancel_release_ms > {config.cancel_limit_ms:.3f}"
        )
    if list(cancel_probe["errors"]):
        violations.append("取消探针出现事件错误")
    return violations


def run_scenario(
    config: StressConfig,
    *,
    native_module: Any | None = None,
) -> dict[str, object]:
    """运行一个场景并返回可直接 JSON 序列化的证据。"""

    native = native_module or load_native_module()
    context = mp.get_context("spawn")
    pressure_stop = context.Event()
    pressure_processes: list[Any] = []
    result: dict[str, object] = {
        "evidence_scope": EVIDENCE_SCOPE,
        "android_acceptance": False,
        "warning": EVIDENCE_WARNING,
        "mode": config.mode,
        "configuration": asdict(config),
        "native_version": str(native.version()),
        "status": "error",
        "passed": False,
    }
    try:
        if config.mode == "pressure":
            for index in range(config.pressure_workers):
                process = context.Process(
                    target=_cpu_pressure_worker,
                    args=(pressure_stop, index),
                    name=f"mbdr-cpu-pressure-{index}",
                )
                process.start()
                pressure_processes.append(process)
        metrics = _run_playback_workload(config, native, context)
        cancel_probe = _run_cancel_probe(config, native, context)
        violations = _evaluate_gates(config, metrics, cancel_probe)
        result.update(
            {
                "status": "passed" if not violations else "gate_failed",
                "passed": not violations,
                "metrics": {
                    key: value
                    for key, value in metrics.items()
                    if key not in {"command_counts", "protocol_errors"}
                },
                "protocol": {
                    "command_counts": metrics["command_counts"],
                    "errors": metrics["protocol_errors"],
                },
                "cancel_probe": cancel_probe,
                "gates": {
                    "drift_p95_limit_ms": config.p95_limit_ms,
                    "drift_max_limit_ms": config.max_drift_limit_ms,
                    "underflow_limit": 0,
                    "cancel_limit_ms": config.cancel_limit_ms,
                    "violations": violations,
                },
            }
        )
    except Exception as exc:
        result.update(
            {
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
                "gates": {"violations": ["scenario runtime error"]},
            }
        )
    finally:
        pressure_stop.set()
        for process in pressure_processes:
            process.join(timeout=2.0)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2.0)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Native 滚动链路真实墙钟虚拟设备压力测试；结果不是 Android 验收。"
        )
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=120.0,
        help="每个场景的墙钟秒数，默认 120",
    )
    parser.add_argument(
        "--mode",
        choices=("idle", "pressure", "both"),
        default="both",
        help="空闲、外部 CPU 压力或两者，默认 both",
    )
    parser.add_argument("--lookahead-ms", type=float, default=500.0)
    parser.add_argument("--low-water-ms", type=float, default=200.0)
    parser.add_argument("--max-queue-ms", type=float, default=750.0)
    parser.add_argument("--p95-limit-ms", type=float, default=3.0)
    parser.add_argument("--max-drift-limit-ms", type=float, default=8.0)
    parser.add_argument("--cancel-limit-ms", type=float, default=500.0)
    parser.add_argument("--pressure-workers", type=int, default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        base_config = StressConfig(
            duration_s=args.duration,
            mode="idle",
            lookahead_s=args.lookahead_ms / 1000.0,
            low_water_s=args.low_water_ms / 1000.0,
            max_queue_s=args.max_queue_ms / 1000.0,
            p95_limit_ms=args.p95_limit_ms,
            max_drift_limit_ms=args.max_drift_limit_ms,
            cancel_limit_ms=args.cancel_limit_ms,
            pressure_workers=args.pressure_workers,
        )
        native = load_native_module()
        modes = ("idle", "pressure") if args.mode == "both" else (args.mode,)
        scenarios = [
            run_scenario(
                replace(base_config, mode=mode), native_module=native
            )
            for mode in modes
        ]
        payload = {
            "schema_version": 1,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "evidence_scope": EVIDENCE_SCOPE,
            "android_acceptance": False,
            "warning": EVIDENCE_WARNING,
            "duration_is_per_scenario": True,
            "scenarios": scenarios,
            "passed": all(bool(item["passed"]) for item in scenarios),
        }
        exit_code = 0 if payload["passed"] else 1
        if any(item["status"] == "error" for item in scenarios):
            exit_code = 2
    except Exception as exc:
        payload = {
            "schema_version": 1,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "evidence_scope": EVIDENCE_SCOPE,
            "android_acceptance": False,
            "warning": EVIDENCE_WARNING,
            "passed": False,
            "setup_error": f"{type(exc).__name__}: {exc}",
        }
        exit_code = 2

    text = json.dumps(
        payload,
        # Windows 控制台代码页不固定；ASCII 转义保证 stdout 始终是合法 JSON。
        ensure_ascii=True,
        indent=2 if args.pretty else None,
        sort_keys=True,
    )
    print(text)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
