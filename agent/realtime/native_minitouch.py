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
import socket
import string
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, TextIO

from . import native_engine


_DEVICE_BINARY = "/data/local/tmp/minitouch_maabangdream"
_VENDOR_ROOT = Path(__file__).resolve().parent / "native" / "vendor" / "minitouch"


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
        self._jlog_file: TextIO | None = None
        self._log_lock = threading.Lock()
        self._closed = True
        self._spawned = False
        self._last_reset_sent = False
        self._last_release_error: str | None = None
        self._reset_lock = threading.Lock()
        self._reset_thread: threading.Thread | None = None
        self._local_stop_lock = threading.Lock()
        self._full_stop_lock = threading.Lock()

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

    def _read_logs(self, client: Any) -> None:
        # 持续排空 minitouch 的 jlog 输出；不读会导致设备端输出缓冲写满、
        # 命令执行被阻塞，进而拖慢整条时间线。
        carry = ""
        while not self._closed and client.connected:
            try:
                chunk = client.receive(65536, 500)
            except Exception:  # noqa: BLE001 - 停止阶段套接字可能已关闭
                break
            if not chunk:
                continue
            parts = (carry + chunk.replace("\r\n", "\n")).split("\n")
            carry = parts.pop()
            for line in parts:
                line = line.strip()
                if line:
                    self._record_log_line(line)

    def _record_log_line(
        self, line: str, received_s: float | None = None
    ) -> None:
        """内存保留诊断尾部，同时把原始 jlog 逐行刷到运行证据。"""
        if received_s is None:
            received_s = time.perf_counter()
        with self._log_lock:
            self._log_sequence += 1
            self._log_lines.append(line)
            self._log_records.append(
                (self._log_sequence, line, float(received_s))
            )
            if self._jlog_file is not None:
                self._jlog_file.write(line + "\n")
                self._jlog_file.flush()

    def start(
        self,
        *,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """push 二进制、启动 minitouch、forward 并完成握手。"""
        self._raise_if_cancelled(cancel_event)
        self._closed = False
        self._last_reset_sent = False
        if self._jlog_path is not None:
            self._jlog_path.parent.mkdir(parents=True, exist_ok=True)
            self._jlog_file = self._jlog_path.open(
                "a", encoding="utf-8", newline="\n"
            )
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
            args=(client,),
            daemon=True,
        )
        self._log_thread.start()

    def publish(self, text: str) -> None:
        """追加一段已定时脚本；时序由设备端 w 保证。"""
        if self._closed or not self._client or not self._client.connected:
            raise MinitouchStartError("minitouch 未连接")
        if not self._client.publish(text):
            raise MinitouchStartError("publish 失败")

    def _send_reset(self, client: Any) -> None:
        sent = False
        try:
            if client.connected:
                sent = bool(client.publish("r\n"))
        except Exception:  # noqa: BLE001 - 设备端 kill 是独立释放证据
            sent = False
        self._last_reset_sent = sent

    def request_reset(self) -> bool:
        """异步尝试 panic reset；协议无 ACK，调用本身绝不等待 send。"""
        self._last_reset_sent = False
        with self._reset_lock:
            if self._reset_thread is not None and self._reset_thread.is_alive():
                return False
            client = self._client
            if self._closed or client is None or not client.connected:
                return False
            self._reset_thread = threading.Thread(
                target=self._send_reset,
                args=(client,),
                name="native-minitouch-reset",
                daemon=True,
            )
            self._reset_thread.start()
        return False

    def _emergency_stop_impl(self, timeout_s: float) -> bool:
        """在后台执行本地句柄清理，外层负责硬截止。"""
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
            if self._log_lock.acquire(timeout=remaining()):
                try:
                    if self._jlog_file is not None:
                        try:
                            self._jlog_file.flush()
                            self._jlog_file.close()
                        except Exception:  # noqa: BLE001
                            success = False
                        else:
                            self._jlog_file = None
                finally:
                    self._log_lock.release()
            else:
                success = False
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

        self.request_reset()
        reset_thread = self._reset_thread
        if reset_thread is not None and reset_thread.is_alive():
            reset_thread.join(timeout=min(0.02, remaining()))
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
        self._last_release_error = "; ".join(errors) or None
        # forward 清理失败会留下资源，但不会推翻设备端进程已退出这一触点证据。
        return bool(local_ok and remote_ok)

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
        return self.stop_with_deadline(0.5)
