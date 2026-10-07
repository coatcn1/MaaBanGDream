"""Native minitouch 设备编排：push/启动/forward/发布/回读/清理。

职责边界：
- 设备上的进程与端口编排（adb push、chmod、启动、forward、清理）由本模块
  完成，属于 Python 侧的 MFA 编排层；
- 时序相关的脚本编译、发布字节流与 jlog 解析/统计全部在 C++
  （TouchScriptCompiler / MinitouchClient / LatencyCalibrator）；
- 本模块默认不开任何后台进程，只有显式调用 start() 才动设备。
"""

from __future__ import annotations

import random
import json
import math
import queue
import os
import socket
import string
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from . import native_engine


_DEVICE_BINARY = "/data/local/tmp/minitouch_maabangdream"
_VENDOR_ROOT = Path(__file__).resolve().parent / "native" / "vendor" / "minitouch"
_JLOG_WRITER_LOCK = threading.Lock()
_JLOG_WRITERS: dict[object, "_JlogWriter"] = {}


def _parse_surface_rotation(dumpsys_input: str) -> int:
    """从 `dumpsys input` 输出解析 SurfaceOrientation，失败默认 0。"""
    for line in str(dumpsys_input).splitlines():
        line = line.strip()
        if line.startswith("SurfaceOrientation:"):
            value = line.split(":", 1)[1].strip()
            try:
                rotation = int(value)
            except ValueError:
                return 0
            if 0 <= rotation <= 3:
                return rotation
            return 0
    return 0


class MinitouchStartError(RuntimeError):
    """minitouch 无法在设备上启动或握手失败。"""


class _JlogWriter:
    """单 generation 的诊断 IO；状态锁从不覆盖文件系统操作。"""

    def __init__(self, path, generation, *, stream=None, capacity=4096, key=None):
        self.path = path
        self.key = key
        self.generation = generation
        self._stream = stream
        self._queue = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._done = threading.Event()
        self._drain_deadline = None
        self._accepted = self._written = self._dropped = 0
        self._slow_batches = 0
        self._max_batch_ms = 0.0
        self._error = None
        self.thread = threading.Thread(target=self._run, name="native-jlog-writer", daemon=True)

    def enqueue(self, line):
        with self._lock:
            self._accepted += 1
            if self._stop.is_set() or self._error is not None:
                self._dropped += 1
                return
            try:
                self._queue.put_nowait(line)
            except queue.Full:
                self._dropped += 1

    def stop(self):
        # 停止不等待磁盘；健康 writer 自行排空，超时后不再开始新的批写。
        with self._lock:
            if not self._stop.is_set():
                self._drain_deadline = time.perf_counter() + .020
                self._stop.set()

    def snapshot(self):
        with self._lock:
            pending = self._accepted - self._written - self._dropped
            done = self._done.is_set()
            timed_out = bool(self._stop.is_set() and not done
                             and time.perf_counter() >= self._drain_deadline)
            warnings = []
            if self._error:
                warnings.append("disk-error")
            if self._dropped:
                warnings.append("diagnostic-lines-dropped")
            if timed_out:
                warnings.append("drain-timeout")
            if self._slow_batches:
                warnings.append("slow-disk")
            return {"enabled": True, "generation": self.generation,
                    "accepted_lines": self._accepted, "written_lines": self._written,
                    "dropped_lines": self._dropped, "pending_lines": pending,
                    "missing_lines": self._accepted - self._written,
                    "complete": bool(done and not self._error and not self._dropped and not pending),
                    "drain_status": "closed" if done else ("timeout" if timed_out else
                                    "draining" if self._stop.is_set() else "running"),
                    "writer_alive": self.thread.is_alive(), "error": self._error,
                    "slow_batches": self._slow_batches, "max_batch_ms": self._max_batch_ms,
                    "warnings": warnings}

    def _discard_pending(self):
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
                dropped += 1
            except queue.Empty:
                break
        with self._lock:
            self._dropped += dropped

    def _run(self):
        stream = self._stream
        batch = []
        try:
            if stream is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                stream = self.path.open("a", encoding="utf-8", newline="\n")
            while True:
                if self._stop.is_set() and (self._queue.empty()
                        or time.perf_counter() >= self._drain_deadline):
                    break
                try:
                    batch = [self._queue.get(timeout=.010)]
                except queue.Empty:
                    continue
                for _ in range(63):
                    try:
                        batch.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
                started = time.perf_counter()
                # 文件句柄仅归此线程；异常、慢盘和阻塞均不触及输入与回执锁。
                stream.write("".join(line + "\n" for line in batch))
                stream.flush()
                elapsed_ms = (time.perf_counter() - started) * 1000
                with self._lock:
                    self._written += len(batch)
                    self._max_batch_ms = max(self._max_batch_ms, elapsed_ms)
                    self._slow_batches += int(elapsed_ms > 20)
                batch = []
        except Exception as exc:  # noqa: BLE001 - 诊断失败不能杀死 socket reader
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
                self._dropped += len(batch)
        finally:
            # 不可中断的系统 IO 可能仍阻塞。线程只保留旧私有状态，不能修改新局。
            self._discard_pending()
            if stream is not None:
                try:
                    stream.close()
                except Exception as exc:  # noqa: BLE001
                    with self._lock:
                        self._error = self._error or f"{type(exc).__name__}: {exc}"
            self._done.set()
            with _JLOG_WRITER_LOCK:
                if _JLOG_WRITERS.get(self.key) is self:
                    del _JLOG_WRITERS[self.key]


class NativeMinitouchDevice:
    """在模拟器/真机上运行 EvATive7 minitouch 并通过 TCP 发布脚本。"""

    def __init__(
        self,
        adb_path: str,
        serial: str,
        *,
        jlog_path: str | Path | None = None,
    ) -> None:
        self._adb = adb_path
        self._serial = serial
        self._abi: str | None = None
        self._socket_name = "minitouch_maabangdream_" + "".join(
            random.choices(string.ascii_lowercase, k=7)
        )
        self._port = 0
        self._process: subprocess.Popen[str] | None = None
        self._client: Any | None = None
        self._pid: int | None = None
        self._max_x = 0
        self._max_y = 0
        self._max_contacts = 0
        self._surface_rotation = 0
        self._stderr_thread: threading.Thread | None = None
        self._stderr_lines: deque[str] = deque(maxlen=40)
        self._log_thread: threading.Thread | None = None
        self._log_lines: deque[str] = deque(maxlen=4096)
        self._log_records: deque[tuple[int, str, float]] = deque(maxlen=4096)
        self._log_sequence = 0
        self._jlog_path = Path(jlog_path) if jlog_path is not None else None
        self._jlog_writer: _JlogWriter | None = None
        self._jlog_blocking_writer: _JlogWriter | None = None
        self._jlog_disabled_reason: str | None = None
        self._jlog_disabled_lines = 0
        self._log_lock = threading.Lock()
        self._closed = True
        self._spawned = False
        self._last_reset_sent = False
        self._last_release_error: str | None = None
        self._reset_lock = threading.Lock()
        self._reset_thread: threading.Thread | None = None
        self._reset_generation = 0
        self._reset_requested = False
        self._reset_request_cursor: int | None = None
        self._reset_requested_at_s: float | None = None
        self._reset_sent = False
        self._reset_executed = False
        self._reset_execution_latency_ms: float | None = None
        self._reset_executed_event = threading.Event()
        self._release_proof = "no-touch-possible"
        self._forced_kill_used = False
        self._touch_possible = False
        self._local_stop_lock = threading.Lock()
        self._full_stop_lock = threading.Lock()
        self._publish_lock = threading.Lock()
        self._publishing_closed = False
        self._published_tail_command: str | None = None
        self._release_commands: tuple[str, ...] = ()
        self._release_contacts: tuple[int, ...] = ()
        self._release_send_cursor: int | None = None
        self._release_match_index = 0
        self._release_last_log_sequence = 0
        self._release_last_end_ms: float | None = None
        self._release_complete_at_s: float | None = None
        self._release_sequence_confirmed = False
        self._release_sequence_error: str | None = None
        self._release_log_batches = 0
        self._release_log_carry_pending = False

    # -- 基本属性 --
    @property
    def connected(self) -> bool:
        return (
            not self._closed
            and self._client is not None
            and self._client.connected
        )

    @property
    def last_reset_sent(self) -> bool:
        """最近一次 panic reset 是否已成功写入本地传输。"""
        return self._last_reset_sent

    @property
    def last_release_error(self) -> str | None:
        """最近一次有界释放无法确认时的原因。"""
        return self._last_release_error

    @property
    def reset_executed(self) -> bool:
        """本轮 reset 是否已由设备 jlog 明确回执。"""
        with self._reset_lock:
            return self._reset_executed

    @property
    def release_diagnostics(self) -> dict[str, object]:
        """返回本轮释放证据；旧日志不能跨 generation 复用。"""
        with self._reset_lock:
            return {
                "reset_requested": self._reset_requested,
                "reset_sent": self._reset_sent,
                "reset_executed": self._reset_executed,
                "reset_execution_latency_ms": self._reset_execution_latency_ms,
                "release_proof": self._release_proof,
                "forced_kill_used": self._forced_kill_used,
                "release_sequence_confirmed": self._release_sequence_confirmed,
                "release_contacts": list(self._release_contacts),
                "release_scope": "minitouch-protocol-and-cleanup",
                "release_sequence_error": self._release_sequence_error,
                "release_generation": self._reset_generation,
                "release_send_cursor": self._release_send_cursor,
            }

    @property
    def max_x(self) -> int:
        return self._max_x

    @property
    def max_y(self) -> int:
        return self._max_y

    @property
    def max_contacts(self) -> int:
        return self._max_contacts

    @property
    def surface_rotation(self) -> int:
        return self._surface_rotation

    @property
    def recent_stderr(self) -> list[str]:
        return list(self._stderr_lines)

    @property
    def recent_logs(self) -> list[str]:
        with self._log_lock:
            return list(self._log_lines)

    @property
    def jlog_io_diagnostics(self) -> dict[str, object]:
        writer = self._jlog_writer
        if self._jlog_disabled_reason is not None:
            return {"enabled": False, "complete": False,
                    "generation": self._reset_generation,
                    "missing_lines": self._jlog_disabled_lines,
                    "warnings": [self._jlog_disabled_reason],
                    "previous_writer": self._jlog_blocking_writer.snapshot()
                                       if self._jlog_blocking_writer else None}
        return writer.snapshot() if writer else {"enabled": False, "complete": True, "warnings": []}

    def _start_jlog_writer(self, *, stream=None, capacity=4096):
        previous = self._jlog_writer
        self._jlog_disabled_lines = 0
        if previous is not None and previous.thread.is_alive():
            previous.stop()
            # 同一路径仍有旧 IO 时禁用本局诊断，不能跨 generation 交叉追加。
            self._jlog_disabled_reason = "previous-generation-writer-pending"
            self._jlog_blocking_writer = previous
            self._jlog_writer = None
            return None
        self._jlog_disabled_reason = None
        self._jlog_blocking_writer = None
        if self._jlog_path is not None or stream is not None:
            key = (("path", os.path.normcase(os.path.abspath(self._jlog_path)))
                   if self._jlog_path is not None else ("stream", id(stream)))
            with _JLOG_WRITER_LOCK:
                previous = _JLOG_WRITERS.get(key)
                if previous is not None and previous.thread.is_alive():
                    # 预武装重建会换 Device，但同 run 路径仍不能与旧诊断 IO 重叠。
                    self._jlog_writer = None
                    self._jlog_blocking_writer = previous
                    self._jlog_disabled_reason = "previous-generation-writer-pending"
                    return None
                self._jlog_writer = _JlogWriter(self._jlog_path, self._reset_generation,
                                               stream=stream, capacity=capacity, key=key)
                _JLOG_WRITERS[key] = self._jlog_writer
                self._jlog_writer.thread.start()
        return self._jlog_writer

    @property
    def last_publish_diagnostics(self) -> dict[str, object] | None:
        """透传最近一次 C++ socket publish 的只读计数。"""
        client = self._client
        if client is None:
            return None
        diagnostics = getattr(client, "last_publish_diagnostics", None)
        if diagnostics is None:
            return None
        try:
            return dict(diagnostics)
        except (TypeError, ValueError):
            return None

    def logs_since(self, after_sequence: int) -> tuple[int, list[str]]:
        """按单调序号取出新日志，不用文本去重丢掉重复命令。"""
        with self._log_lock:
            requested = int(after_sequence)
            current = self._validate_log_cursor_locked(requested)
            rows = [
                line
                for sequence, line, _ in self._log_records
                if sequence > requested
            ]
        return current, rows

    def log_records_since(
        self, after_sequence: int
    ) -> tuple[int, list[tuple[str, float]]]:
        """返回日志及接收线程时间戳，供探测两端单调时钟差。"""
        with self._log_lock:
            requested = int(after_sequence)
            current = self._validate_log_cursor_locked(requested)
            rows = [
                (line, received_s)
                for sequence, line, received_s in self._log_records
                if sequence > requested
            ]
        return current, rows

    def _validate_log_cursor_locked(self, requested: int) -> int:
        current = self._log_sequence
        if requested > current:
            raise RuntimeError(
                f"jlog 游标超前：requested={requested} current={current}"
            )
        if self._log_records and requested < self._log_records[0][0] - 1:
            raise RuntimeError(
                "jlog 内存队列已溢出："
                f"requested={requested} oldest={self._log_records[0][0]} "
                f"current={current}"
            )
        return current

    def _run_adb(self, *args: str, check: bool = True) -> str:
        command = [self._adb, "-s", self._serial, *args]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if check and completed.returncode != 0:
            raise MinitouchStartError(
                f"adb {' '.join(args)} 失败：{completed.stderr.strip()}"
            )
        return (completed.stdout or "").strip()

    def _run_adb_cleanup(self, *args: str, timeout_s: float) -> bool:
        """在剩余释放预算内执行一次 ADB 清理并返回可核验结果。"""
        if timeout_s <= 0:
            return False
        command = [self._adb, "-s", self._serial, *args]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(0.01, float(timeout_s)),
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0

    @staticmethod
    def _raise_if_cancelled(
        cancel_event: threading.Event | None,
    ) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise MinitouchStartError("minitouch 准备已取消")

    @classmethod
    def _cancel_aware_wait(
        cls,
        cancel_event: threading.Event | None,
        timeout_s: float,
    ) -> None:
        if cancel_event is None:
            time.sleep(timeout_s)
            return
        if cancel_event.wait(timeout_s):
            cls._raise_if_cancelled(cancel_event)

    def _detect_abi(self) -> str:
        abi = self._run_adb("shell", "getprop", "ro.product.cpu.abi")
        if not abi or "not found" in abi:
            raise MinitouchStartError("无法探测设备 ABI")
        return abi

    def _binary_path(self, abi: str) -> Path:
        candidates = [_VENDOR_ROOT / abi / "minitouch"]
        if abi == "arm64-v8a":
            candidates.insert(0, _VENDOR_ROOT / "arm64" / "minitouch")
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise MinitouchStartError(f"没有适配 {abi} 的 minitouch 二进制")

    def _free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        for raw in process.stderr:
            line = raw.strip()
            if line:
                self._stderr_lines.append(line)

    def _read_logs(self, client: Any, generation: int | None = None) -> None:
        # 持续排空 minitouch 的 jlog 输出；不读会导致设备端输出缓冲写满、
        # 命令执行被阻塞，进而拖慢整条时间线。
        carry = ""
        if generation is None:
            generation = self._reset_generation
        while not self._closed and client.connected:
            try:
                chunk = client.receive(65536, 500)
            except Exception:  # noqa: BLE001 - 停止阶段套接字可能已关闭
                break
            with self._reset_lock:
                if generation != self._reset_generation or (
                    self._client is not client and self._client is not None
                ):
                    return
                self._release_log_batches += 1
            try:
                if not chunk:
                    continue
                parts = (carry + chunk.replace("\r\n", "\n")).split("\n")
                carry = parts.pop()
                with self._reset_lock:
                    if generation == self._reset_generation:
                        # TCP 尾行尚未完整接收时不能提前确认，尾部可能是迟到的 DOWN。
                        self._release_log_carry_pending = bool(carry)
                for line in parts:
                    line = line.strip()
                    if line:
                        self._record_log_line(line, source_generation=generation, source_client=client)
            finally:
                with self._reset_lock:
                    if generation == self._reset_generation:
                        self._release_log_batches -= 1
                        # 同批已收到的 late DOWN 必须先检查完，不能让末 commit 提前唤醒清理。
                        self._confirm_release_locked()

    def _record_log_line(
        self, line: str, received_s: float | None = None,
        *, source_generation: int | None = None, source_client: Any | None = None,
    ) -> None:
        """先处理内存回执，再非阻塞入队诊断；磁盘不能拖慢 reader。"""
        if received_s is None:
            received_s = time.perf_counter()
        with self._log_lock:
            with self._reset_lock:
                if source_generation is not None and source_generation != self._reset_generation:
                    return
                if source_client is not None and self._client is not source_client and self._client is not None:
                    return
                generation = self._reset_generation
                self._log_sequence += 1
                sequence = self._log_sequence
                self._log_lines.append(line)
                self._log_records.append((sequence, line, float(received_s)))
                writer = self._jlog_writer
                if self._jlog_disabled_reason is not None:
                    self._jlog_disabled_lines += 1
        self._observe_reset_execution(sequence, line, float(received_s), generation)
        if writer is not None and writer.generation == generation and self._jlog_disabled_reason is None:
            writer.enqueue(line)

    def _observe_reset_execution(
        self,
        sequence: int,
        line: str,
        received_s: float,
        source_generation: int | None = None,
    ) -> None:
        """连续匹配本轮保留标记、全部 UP、commit、reset 和末 commit。"""
        with self._reset_lock:
            request_cursor = self._release_send_cursor
            requested_at_s = self._reset_requested_at_s
            if (
                not self._reset_requested
                or (source_generation is not None and source_generation != self._reset_generation)
                or request_cursor is None
                or sequence <= request_cursor
                or self._release_sequence_error is not None
            ):
                return
            if sequence <= self._release_last_log_sequence:
                self._invalidate_release_locked("out-of-order-jlog")
                return
            self._release_last_log_sequence = sequence
            if not line.startswith("jlog "):
                if self._release_match_index:
                    self._invalidate_release_locked("interrupted-release-sequence")
                return
            try:
                def unique_fields(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError("duplicate-jlog-field")
                        result[key] = value
                    return result
                event = json.loads(line[5:], object_pairs_hook=unique_fields)
                values = [event[key] for key in ("st", "et", "c")]
                if (any(type(value) not in (int, float) or not math.isfinite(value) for value in values)
                    or not math.isfinite(received_s) or requested_at_s is None
                    or received_s < requested_at_s or values[0] < 0
                    or values[1] < values[0] or values[2] < 0
                    or (self._release_last_end_ms is not None and values[0] < self._release_last_end_ms)):
                    raise ValueError("invalid-jlog-time")
                command = event["cmd"]
                if not isinstance(command, str) or not command or command != command.strip():
                    raise ValueError("invalid-jlog-command")
            except Exception:
                self._invalidate_release_locked("malformed-release-jlog")
                return
            self._release_last_end_ms = values[1]
            if self._release_complete_at_s is not None:
                if command.split(" ", 1)[0] in {"d", "m"}:
                    self._invalidate_release_locked("late-touch-after-release")
                return
            index = self._release_match_index
            if index == 1 and command != "w 0":
                # 单个零等待可能来自排队 probe；第二个标记未成立前继续扫描。
                self._release_match_index = 0
                return
            if not index and command != self._release_commands[0]:
                # 已发布谱面可继续排空；普通 UP/c/r 不能成为释放序列的起点。
                return
            if command != self._release_commands[index]:
                self._invalidate_release_locked("release-sequence-mismatch")
                return
            self._release_match_index += 1
            if self._release_match_index == len(self._release_commands):
                self._release_complete_at_s = received_s
                self._confirm_release_locked()

    def _invalidate_release_locked(self, reason: str) -> None:
        self._release_sequence_error = reason
        self._release_sequence_confirmed = False
        self._reset_executed = False
        self._touch_possible = True
        self._reset_executed_event.clear()
        self._release_proof = "release-sequence-unconfirmed"

    def _confirm_release_locked(self) -> None:
        # 完整发送和完整执行是独立证据；同步回读可能先于 publish() 返回。
        if (self._closed or self._release_log_batches or self._release_log_carry_pending or not self._reset_sent
            or self._release_sequence_error or self._release_complete_at_s is None):
            return
        self._release_sequence_confirmed = True
        self._reset_executed = True
        self._touch_possible = False
        self._reset_execution_latency_ms = max(0.0, (
            self._release_complete_at_s - self._reset_requested_at_s) * 1000)
        self._release_proof = "current-release-sequence-jlog"
        self._reset_executed_event.set()

    def start(
        self,
        *,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """push 二进制、启动 minitouch、forward 并完成握手。"""
        self._raise_if_cancelled(cancel_event)
        self._closed = False
        with self._reset_lock:
            self._reset_generation += 1
            self._last_reset_sent = False
            self._reset_requested = False
            self._reset_request_cursor = None
            self._reset_requested_at_s = None
            self._reset_sent = False
            self._reset_executed = False
            self._reset_execution_latency_ms = None
            self._reset_executed_event.clear()
            self._release_proof = "no-touch-possible"
            self._forced_kill_used = False
            self._touch_possible = False
            self._publishing_closed = False
            self._published_tail_command = None
            self._release_commands = ()
            self._release_contacts = ()
            self._release_send_cursor = None
            self._release_match_index = 0
            self._release_last_log_sequence = 0
            self._release_last_end_ms = None
            self._release_complete_at_s = None
            self._release_sequence_confirmed = False
            self._release_sequence_error = None
            self._release_log_batches = 0
            self._release_log_carry_pending = False
        self._start_jlog_writer()
        self._raise_if_cancelled(cancel_event)
        if not native_engine.available():
            raise MinitouchStartError(
                f"Native 模块不可用：{native_engine.unavailable_reason()}"
            )
        self._abi = self._detect_abi()
        self._raise_if_cancelled(cancel_event)
        binary = self._binary_path(self._abi)

        # 幂等 push：已存在且可执行就不重复 push，缩短启动路径。
        listed = self._run_adb("shell", "ls", _DEVICE_BINARY, check=False)
        self._raise_if_cancelled(cancel_event)
        if not listed.endswith(_DEVICE_BINARY):
            self._run_adb("push", str(binary), _DEVICE_BINARY)
            self._raise_if_cancelled(cancel_event)
        self._run_adb("shell", "chmod", "777", _DEVICE_BINARY)
        self._raise_if_cancelled(cancel_event)

        self._process = subprocess.Popen(
            [
                self._adb,
                "-s",
                self._serial,
                "shell",
                f"{_DEVICE_BINARY} -n {self._socket_name}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self._spawned = True
        self._raise_if_cancelled(cancel_event)
        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            args=(self._process,),
            daemon=True,
        )
        self._stderr_thread.start()

        # 等设备端进程完成触控设备检测（stderr 出现 detected 或失败行），
        # 否则立即 connect 会命中“abstract socket 尚未绑定”的竞态。
        deadline = time.monotonic() + 5.0
        detected = False
        while time.monotonic() < deadline and self._process.poll() is None:
            self._raise_if_cancelled(cancel_event)
            lines = list(self._stderr_lines)
            if any("touch device" in line for line in lines):
                detected = True
                break
            if any("Unable to" in line for line in lines):
                break
            self._cancel_aware_wait(cancel_event, 0.05)
        if not detected:
            self.stop()
            raise MinitouchStartError(
                f"minitouch 设备检测超时；stderr 尾部："
                f"{list(self._stderr_lines)[-3:]}"
            )

        self._port = self._free_port()
        self._raise_if_cancelled(cancel_event)
        self._run_adb(
            "forward",
            f"tcp:{self._port}",
            f"localabstract:{self._socket_name}",
        )
        self._raise_if_cancelled(cancel_event)

        # 握手：v / ^ / $ 三行；adbd 可能在新 abstract socket 就绪前短暂
        # 拒绝连接，超时则断开重连，最多 3 次。
        client: Any | None = None
        handshake: dict[str, str] = {}
        for _ in range(3):
            self._raise_if_cancelled(cancel_event)
            client = native_engine.minitouch_client()
            self._client = client
            if not client.connect("127.0.0.1", self._port):
                client.close()
                client = None
                self._client = None
                self._cancel_aware_wait(cancel_event, 0.2)
                continue
            handshake = {}
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                self._raise_if_cancelled(cancel_event)
                chunk = client.receive(4096, 300)
                self._raise_if_cancelled(cancel_event)
                if not chunk:
                    continue
                for line in chunk.replace("\r\n", "\n").split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    kind = line.split(" ", 1)[0]
                    if kind in ("v", "^", "$"):
                        handshake[kind] = line
                        self._record_log_line(line)
            if handshake.get("^"):
                break
            client.close()
            client = None
            self._client = None
            self._cancel_aware_wait(cancel_event, 0.3)
        if client is None or not handshake.get("^"):
            self.stop()
            raise MinitouchStartError(
                f"minitouch 握手超时；stderr 尾部：{list(self._stderr_lines)[-3:]}"
            )
        self._raise_if_cancelled(cancel_event)
        self._client = client
        parts = handshake["^"].split()
        if len(parts) != 5:
            self.stop()
            raise MinitouchStartError(f"异常握手头：{handshake['^']}")
        self._max_contacts = int(parts[1])
        self._max_x = int(parts[2])
        self._max_y = int(parts[3])
        if not 1 <= self._max_contacts <= 10 or self._max_x <= 0 or self._max_y <= 0:
            self.stop()
            raise MinitouchStartError("minitouch 握手触点数量或坐标范围无效")
        # MuMu 等模拟器的物理触摸面可能是竖屏（如 720x1280），而游戏截图
        # 是横屏；读取当前 SurfaceOrientation，后续发布命令时据此把逻辑
        # 坐标映射回物理坐标，避免所有触点被压到同一列。
        try:
            rotation_text = self._run_adb("shell", "dumpsys", "input")
        except MinitouchStartError:
            rotation_text = ""
        self._surface_rotation = _parse_surface_rotation(rotation_text)
        if handshake.get("$"):
            try:
                self._pid = int(handshake["$"].split()[1])
            except (IndexError, ValueError):
                self._pid = None
        self._raise_if_cancelled(cancel_event)
        # 握手完成后再启动日志排空线程，避免与握手读取抢同一套接字。
        self._log_thread = threading.Thread(
            target=self._read_logs,
            args=(client, self._reset_generation),
            daemon=True,
        )
        self._log_thread.start()

    def publish(self, text: str) -> None:
        """追加一段已定时脚本；时序由设备端 w 保证。"""
        commands = tuple(line.strip() for line in str(text).splitlines() if line.strip())
        if any(commands[index:index + 2] == ("w 0", "w 0") for index in range(len(commands) - 1)):
            raise MinitouchStartError("普通输入禁止使用保留的释放标记")
        may_establish_touch = any(
            line.strip().split(" ", 1)[0] == "d"
            for line in str(text).splitlines()
            if line.strip()
        )
        # 串行化现有发布与释放批次；状态锁不得覆盖 socket 写入或等待。
        with self._publish_lock:
            with self._reset_lock:
                if self._publishing_closed:
                    raise MinitouchStartError("minitouch 输入发布已封闭")
                if self._closed or not self._client or not self._client.connected:
                    raise MinitouchStartError("minitouch 未连接")
                if self._published_tail_command == "w 0" and commands and commands[0] == "w 0":
                    raise MinitouchStartError("普通输入跨批次禁止拼接保留的释放标记")
                if may_establish_touch:
                    # 部分发送也可能已建立触点，不能依赖 publish 返回值走无触点捷径。
                    self._touch_possible = True
                client = self._client
                if commands:
                    self._published_tail_command = commands[-1]
            if not client.publish(text):
                raise MinitouchStartError("publish 失败")

    def _send_reset(self, client: Any, generation: int) -> None:
        sent = False
        try:
            with self._publish_lock:
                # 内存日志游标锁只覆盖内存读写，不与诊断磁盘 IO 共享。
                with self._log_lock:
                    send_cursor = self._log_sequence
                with self._reset_lock:
                    if generation != self._reset_generation or self._closed or client is not self._client:
                        return
                    self._release_send_cursor = send_cursor
                    self._release_last_log_sequence = self._release_send_cursor
                    # 排队末尾单个零等待用 1ms 无触控等待隔开，不能让旧前缀凑齐新标记。
                    prelude = "w 1\n" if self._published_tail_command == "w 0" else ""
                    payload = prelude + "\n".join(self._release_commands) + "\n"
                if client.connected:
                    sent = bool(client.publish(payload))
        except Exception:
            sent = False
        with self._reset_lock:
            if generation != self._reset_generation:
                return
            self._last_reset_sent = sent
            self._reset_sent = sent
            if not sent or self._closed or client is not self._client:
                self._invalidate_release_locked("partial-or-failed-release-send")
            else:
                self._confirm_release_locked()

    def request_reset(self) -> bool:
        """异步封闭新发布并发送本轮 UP/commit/reset；不等待传输或回执。"""
        with self._reset_lock:
            self._publishing_closed = True
            if self._reset_requested:
                return self._reset_executed
            if not self._touch_possible:
                self._release_proof = "no-touch-possible"
                return True
            if type(self._max_contacts) is not int or not 1 <= self._max_contacts <= 10:
                self._release_sequence_error = "invalid-contact-handshake"
                self._release_proof = "release-sequence-unconfirmed"
                return False
            if self._reset_thread is not None and self._reset_thread.is_alive():
                return False
            client = self._client
            if self._closed or client is None or not client.connected:
                return False
            self._reset_request_cursor = self._log_sequence
            self._reset_requested = True
            self._reset_requested_at_s = time.perf_counter()
            self._last_reset_sent = False
            self._reset_sent = False
            self._release_proof = "reset-execution-pending"
            self._release_contacts = tuple(range(self._max_contacts))
            # 普通编译器不产生零等待；probe 的单个 w 0 被 c 隔开，双零等待是保留标记。
            self._release_commands = ("w 0", "w 0", *(f"u {contact}" for contact in self._release_contacts), "c", "r", "c")
            generation = self._reset_generation
            self._reset_thread = threading.Thread(
                target=self._send_reset,
                args=(client, generation),
                name="native-minitouch-reset",
                daemon=True,
            )
            self._reset_thread.start()
        return False

    def _emergency_stop_impl(self, timeout_s: float) -> bool:
        """在后台执行本地句柄清理，外层负责硬截止。"""
        writer = self._jlog_writer
        deadline = time.monotonic() + max(0.0, float(timeout_s))

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        if not self._local_stop_lock.acquire(timeout=remaining()):
            return False
        success = True
        try:
            self._closed = True
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:  # noqa: BLE001 - 保留句柄供后续重试
                    success = False
                else:
                    self._client = None
            reader = self._log_thread
            if reader is not None and reader is not threading.current_thread():
                reader.join(timeout=remaining())
                if reader.is_alive():
                    success = False
            if self._process is not None:
                try:
                    self._process.kill()
                except Exception:  # noqa: BLE001
                    success = False
                else:
                    wait = getattr(self._process, "wait", None)
                    if callable(wait):
                        try:
                            wait(timeout=min(0.05, remaining()))
                        except Exception:  # noqa: BLE001 - 保留句柄供后续重试
                            success = False
                        else:
                            self._process = None
                    else:
                        self._process = None
            if writer is not None:
                writer.stop()
            return success
        finally:
            self._local_stop_lock.release()

    def emergency_stop_with_deadline(self, timeout_s: float) -> bool:
        """有界关闭本地句柄；超时线程继续自清理，但本次返回 False。"""
        budget = max(0.0, float(timeout_s))
        if budget <= 0:
            return False
        result: list[bool] = []

        def run() -> None:
            result.append(self._emergency_stop_impl(budget))

        worker = threading.Thread(
            target=run,
            name="native-minitouch-local-stop",
            daemon=True,
        )
        worker.start()
        worker.join(timeout=budget)
        return bool(not worker.is_alive() and result and result[0])

    def emergency_stop(self) -> bool:
        """PlaybackSession fallback 使用的 80ms 有界本地断开。"""
        return self.emergency_stop_with_deadline(0.08)

    def _stop_with_deadline_impl(self, timeout_s: float) -> bool:
        """在硬预算内清理，并只在本地及设备端均有证据时返回成功。"""
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        errors: list[str] = []
        self._last_release_error = None

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        with self._reset_lock:
            reset_required = self._touch_possible or self._reset_requested
        reset_confirmed = not reset_required
        if reset_required:
            self.request_reset()
            # 750ms 覆盖已发布的最大设备队列，至少保留 250ms 做本地与远端清理。
            cleanup_reserve_s = (
                0.250
                if timeout_s >= 0.250
                else max(0.0, timeout_s) * 0.25
            )
            with self._reset_lock:
                requested_at_s = self._reset_requested_at_s
            queue_wait_remaining_s = max(
                0.0,
                0.750
                - max(
                    0.0,
                    time.perf_counter() - float(requested_at_s or 0.0),
                ),
            )
            reset_wait_s = min(
                queue_wait_remaining_s,
                max(0.0, remaining() - cleanup_reserve_s),
            )
            if reset_wait_s > 0:
                self._reset_executed_event.wait(timeout=reset_wait_s)
            with self._reset_lock:
                reset_confirmed = self._reset_executed
                if not reset_confirmed:
                    self._forced_kill_used = True
                    self._release_proof = "reset-execution-unconfirmed"
            if not reset_confirmed:
                errors.append("本轮 reset 未取得完整 UP/commit/reset 设备 jlog 执行回执")
        remote_ok = True
        pid = self._pid
        if pid is not None:
            # 先核对唯一 socket，避免 PID 已复用时误杀无关进程。
            command = (
                f"if [ -d /proc/{int(pid)} ]; then "
                f"case \"$(tr '\\000' ' ' < /proc/{int(pid)}/cmdline "
                f"2>/dev/null)\" in *{self._socket_name}*) "
                f"kill -9 {int(pid)} 2>/dev/null || exit 1;; "
                "*) exit 1;; esac; fi"
            )
            remote_ok = self._run_adb_cleanup(
                "shell", command, timeout_s=remaining()
            )
            if remote_ok:
                self._pid = None
                self._spawned = False
            else:
                errors.append(f"设备端 minitouch PID {int(pid)} 未确认退出")
        elif self._spawned:
            # 握手前取消时还没有 `$ <pid>`，按唯一 abstract socket 从
            # /proc/cmdline 定位本轮进程；不能退化成按二进制名全局误杀。
            command = (
                "checked=0; for path in /proc/[0-9]*/cmdline; do "
                "[ -r \"$path\" ] || continue; checked=1; "
                "pid=${path#/proc/}; pid=${pid%/cmdline}; "
                "[ \"$pid\" = \"$$\" ] && continue; "
                f"case \"$(tr '\\000' ' ' < \"$path\" 2>/dev/null)\" "
                f"in *{self._socket_name}*) "
                "kill -9 \"$pid\" 2>/dev/null || exit 1;; esac; "
                "done; [ \"$checked\" -eq 1 ]"
            )
            remote_ok = self._run_adb_cleanup(
                "shell", command, timeout_s=remaining()
            )
            if remote_ok:
                self._spawned = False
            else:
                errors.append("缺少 PID，且未确认唯一 socket 对应进程已退出")

        local_ok = self.emergency_stop_with_deadline(remaining())
        if not local_ok:
            errors.append("本地 minitouch 传输或 adb shell 句柄未确认关闭")

        if self._port:
            forward_ok = self._run_adb_cleanup(
                "forward",
                "--remove",
                f"tcp:{self._port}",
                timeout_s=remaining(),
            )
            if forward_ok:
                self._port = 0
            else:
                errors.append("ADB forward 未在释放预算内移除")
        with self._reset_lock:
            # 清理期间仍可能读到尾部日志；晚到 DOWN/MOVE 必须推翻之前的 ACK。
            reset_confirmed = not reset_required or (self._reset_executed and self._release_sequence_confirmed)
            release_ok = bool(local_ok and remote_ok and reset_confirmed)
            if reset_required and not reset_confirmed and not errors:
                errors.append("完整释放序列在清理期间失效：" + str(self._release_sequence_error or "unconfirmed"))
            if release_ok:
                self._release_proof = (
                    "current-release-sequence-jlog-and-cleanup"
                    if reset_required
                    else "no-touch-possible-and-cleanup"
                )
        self._last_release_error = "; ".join(errors) or None
        # forward 清理失败会留下资源，但不会推翻设备端进程已退出这一触点证据。
        return release_ok

    def stop_with_deadline(self, timeout_s: float) -> bool:
        """串行化完整清理；并发调用也只能共享同一个硬截止预算。"""
        deadline = time.monotonic() + max(0.0, float(timeout_s))

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        if not self._full_stop_lock.acquire(timeout=remaining()):
            return False
        try:
            return self._stop_with_deadline_impl(remaining())
        finally:
            self._full_stop_lock.release()

    def stop(self) -> bool:
        """幂等清理；普通调用也不得无限等待 ADB。"""
        return self.stop_with_deadline(1.0)
