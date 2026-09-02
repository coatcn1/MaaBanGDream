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
from typing import Any

from . import native_engine


_DEVICE_BINARY = "/data/local/tmp/minitouch_maabangdream"
_VENDOR_ROOT = Path(__file__).resolve().parent / "native" / "vendor" / "minitouch"


class MinitouchStartError(RuntimeError):
    """minitouch 无法在设备上启动或握手失败。"""


class NativeMinitouchDevice:
    """在模拟器/真机上运行 EvATive7 minitouch 并通过 TCP 发布脚本。"""

    def __init__(self, adb_path: str, serial: str) -> None:
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
        self._stderr_thread: threading.Thread | None = None
        self._stderr_lines: deque[str] = deque(maxlen=40)
        self._log_thread: threading.Thread | None = None
        self._log_lines: deque[str] = deque(maxlen=4096)
        self._closed = True

    # -- 基本属性 --
    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.connected

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
    def recent_stderr(self) -> list[str]:
        return list(self._stderr_lines)

    @property
    def recent_logs(self) -> list[str]:
        return list(self._log_lines)

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

    def _read_stderr(self) -> None:
        assert self._process is not None
        assert self._process.stderr is not None
        for raw in self._process.stderr:
            line = raw.strip()
            if line:
                self._stderr_lines.append(line)

    def _read_logs(self) -> None:
        # 持续排空 minitouch 的 jlog 输出；不读会导致设备端输出缓冲写满、
        # 命令执行被阻塞，进而拖慢整条时间线。
        assert self._client is not None
        carry = ""
        while not self._closed and self._client.connected:
            try:
                chunk = self._client.receive(65536, 500)
            except Exception:  # noqa: BLE001 - 停止阶段套接字可能已关闭
                break
            if not chunk:
                continue
            parts = (carry + chunk.replace("\r\n", "\n")).split("\n")
            carry = parts.pop()
            for line in parts:
                line = line.strip()
                if line:
                    self._log_lines.append(line)

    def start(self) -> None:
        """push 二进制、启动 minitouch、forward 并完成握手。"""
        self._closed = False
        if not native_engine.available():
            raise MinitouchStartError(
                f"Native 模块不可用：{native_engine.unavailable_reason()}"
            )
        self._abi = self._detect_abi()
        binary = self._binary_path(self._abi)

        # 幂等 push：已存在且可执行就不重复 push，缩短启动路径。
        listed = self._run_adb("shell", "ls", _DEVICE_BINARY, check=False)
        if not listed.endswith(_DEVICE_BINARY):
            self._run_adb("push", str(binary), _DEVICE_BINARY)
        self._run_adb("shell", "chmod", "777", _DEVICE_BINARY)

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
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, daemon=True
        )
        self._stderr_thread.start()

        # 等设备端进程完成触控设备检测（stderr 出现 detected 或失败行），
        # 否则立即 connect 会命中“abstract socket 尚未绑定”的竞态。
        deadline = time.monotonic() + 5.0
        detected = False
        while time.monotonic() < deadline and self._process.poll() is None:
            lines = list(self._stderr_lines)
            if any("touch device" in line for line in lines):
                detected = True
                break
            if any("Unable to" in line for line in lines):
                break
            time.sleep(0.05)
        if not detected:
            self.stop()
            raise MinitouchStartError(
                f"minitouch 设备检测超时；stderr 尾部："
                f"{list(self._stderr_lines)[-3:]}"
            )

        self._port = self._free_port()
        self._run_adb(
            "forward",
            f"tcp:{self._port}",
            f"localabstract:{self._socket_name}",
        )

        # 握手：v / ^ / $ 三行；adbd 可能在新 abstract socket 就绪前短暂
        # 拒绝连接，超时则断开重连，最多 3 次。
        client: Any | None = None
        handshake: dict[str, str] = {}
        for _ in range(3):
            client = native_engine.minitouch_client()
            if not client.connect("127.0.0.1", self._port):
                client.close()
                client = None
                time.sleep(0.2)
                continue
            handshake = {}
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                chunk = client.receive(4096, 300)
                if not chunk:
                    continue
                for line in chunk.replace("\r\n", "\n").split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    kind = line.split(" ", 1)[0]
                    if kind in ("v", "^", "$"):
                        handshake[kind] = line
                        self._log_lines.append(line)
            if handshake.get("^"):
                break
            client.close()
            client = None
            time.sleep(0.3)
        if client is None or not handshake.get("^"):
            self.stop()
            raise MinitouchStartError(
                f"minitouch 握手超时；stderr 尾部：{list(self._stderr_lines)[-3:]}"
            )
        self._client = client
        parts = handshake["^"].split()
        if len(parts) != 5:
            self.stop()
            raise MinitouchStartError(f"异常握手头：{handshake['^']}")
        self._max_contacts = int(parts[1])
        self._max_x = int(parts[2])
        self._max_y = int(parts[3])
        if handshake.get("$"):
            try:
                self._pid = int(handshake["$"].split()[1])
            except (IndexError, ValueError):
                self._pid = None
        # 握手完成后再启动日志排空线程，避免与握手读取抢同一套接字。
        self._log_thread = threading.Thread(target=self._read_logs, daemon=True)
        self._log_thread.start()

    def publish(self, text: str) -> None:
        """一次性写入整段脚本；时序由设备端 w 保证。"""
        if not self._client or not self._client.connected:
            raise MinitouchStartError("minitouch 未连接")
        if not self._client.publish(text):
            raise MinitouchStartError("publish 失败")

    def stop(self) -> None:
        """幂等清理：释放触点、断开 TCP、移除 forward、杀掉设备端进程。"""
        if self._closed:
            return
        self._closed = True
        if self._client is not None:
            try:
                self._client.publish("r\n")
            except Exception:  # noqa: BLE001 - 清理阶段尽力而为
                pass
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        if self._process is not None:
            # 先断开本地 adb shell 客户端：设备端 minitouch 随之退出，
            # 不依赖可能被 LDPlayer adb 串行化的远程 kill 命令。
            try:
                self._process.kill()
            except Exception:  # noqa: BLE001
                pass
            self._process = None
        if self._port:
            self._run_adb(
                "forward", "--remove", f"tcp:{self._port}", check=False
            )
            self._port = 0
        if self._pid is not None:
            self._run_adb("shell", "kill", "-9", str(self._pid), check=False)
            self._pid = None
