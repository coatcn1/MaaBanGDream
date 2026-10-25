"""协力和竞演自动跳车的网络控制：按游戏 UID 屏蔽/恢复出口流量。

网络隔离约束：
- 禁止 `svc wifi` / 飞行模式——它们会顺带打断 MuMu 的 adb 通道，导致
  引擎截图与触控全部中断；
- 按游戏 UID 屏蔽出口流量（需要 root）：MuMu 12 的内核没有 iptables
  ``owner`` 模块，使用 ``ip rule ... blackhole``；其他环境回退到 iptables
  ``owner``。finally 有界尝试恢复；无法确认时保留失败，不冒充已恢复。
"""

from __future__ import annotations

import re
import shlex
import time
import uuid
import subprocess
import json
from collections import deque
from pathlib import Path
from collections.abc import Callable, Sequence
from enum import Enum

from .optional_diagnostics import safe_print


GAME_PACKAGE = "com.bilibili.star.bili"

# adb_shell 的调用协议：输入 shell 参数列表，返回 (returncode, output)。
AdbShell = Callable[[Sequence[str]], tuple[int, str]]


class RulePresence(Enum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class NetworkRecoveryPending(RuntimeError):
    """网络状态未确认，外层必须停止导航和自动续跑。"""


class DeviceIntegrityFailure(NetworkRecoveryPending):
    """设备身份或绑定失效，禁止自动或手动导航回退。"""


class _IsolationCreationStopped(InterruptedError):
    """仅在最终派发前中止新隔离创建，不阻拦必要恢复命令。"""


def _is_isolation_creation(args):
    """精确识别本模块两后端的新增命令，包含单参数 su -c 包装。"""
    probe = tuple(args)
    if not probe or any(not isinstance(part, str) for part in probe):
        return False
    if len(probe) == 3 and probe[:2] == ("su", "-c"):
        try:
            probe = tuple(shlex.split(probe[2]))
        except ValueError:
            return False
    if not probe:
        return False
    if probe[0] == "ip":
        rest = probe[2:] if probe[1:2] == ("-6",) else probe[1:]
        return (
            len(rest) == 7 and rest[:3] == ("rule", "add", "uidrange")
            and re.fullmatch(r"([0-9]+)-\1", rest[3]) is not None
            and rest[4:6] == ("blackhole", "pref") and rest[6].isdigit()
        )
    if probe[0] not in {"iptables", "ip6tables"}:
        return False
    def own_chain(name):
        return re.fullmatch(r"OUTPUT_[A-Za-z0-9_]+", name) is not None
    return (
        len(probe) == 3 and probe[1] == "-N" and own_chain(probe[2])
        or len(probe) == 9 and probe[1] == "-A" and own_chain(probe[2])
        and probe[3:6] == ("-m", "owner", "--uid-owner") and probe[6].isdigit()
        and probe[7:] == ("-j", "REJECT")
        or len(probe) == 6 and probe[1:5] == ("-I", "OUTPUT", "1", "-j") and own_chain(probe[5])
    )


def validate_network_device(transport, stopped):
    if stopped():
        raise InterruptedError("用户已停止任务")
    try:
        transport.validate_current()
    except Exception as exc:
        if stopped():
            raise InterruptedError("用户已停止任务") from exc
        if isinstance(exc, DeviceIntegrityFailure):
            raise
        raise DeviceIntegrityFailure(f"设备完整性核验失败：{exc}") from exc
    if stopped():
        raise InterruptedError("用户已停止任务")


_pending_recovery_evidence = deque(maxlen=16)


def record_pending_network_recovery(gate, transport, root, reason):
    """仅保存已有内存证据，不再查询设备，也不自动接管遗留规则。"""
    safe_print("AutoDisconnectJump network_restore_unconfirmed=true manual_review_required=true", flush=True)
    try:
        record = {
            "schema_version": 1, "status": "unresolved", "reason": str(reason)[:2048],
            "gate_type": type(gate).__name__,
            "uid": getattr(gate, "_uid", None), "pref": getattr(gate, "_pref", None),
            "chain": getattr(gate, "_chain", None),
            "owned_families": sorted(getattr(gate, "_owned_families", ())),
            "uncertain_families": sorted(getattr(gate, "_uncertain_families", ())),
            "owned_tables": sorted(getattr(gate, "_owned_tables", ())),
            "uncertain_tables": sorted(getattr(gate, "_uncertain_tables", ())),
            "last_error": str(getattr(gate, "last_error", ""))[:2048],
            "binding": getattr(transport, "_binding", None),
            "device_identity": getattr(transport, "_identity", None),
            "endpoint_owner": getattr(transport, "_owner", None),
            "local_selected": getattr(transport, "_local_selected", False),
        }
        # 落盘失败时仍短期保留有界内存证据；它只供人工核验，不成为清理授权。
        _pending_recovery_evidence.append(record)
        destination = Path(root) / "debug" / "network-recovery-pending"
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / f"{uuid.uuid4().hex}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        safe_print(f"AutoDisconnectJump pending_network_evidence={path}", flush=True)
    except Exception as exc:
        safe_print(f"AutoDisconnectJump pending_network_evidence_warning={type(exc).__name__}", flush=True)


def run_network_shell(controller, args: Sequence[str], *, before_dispatch=None) -> tuple[int, str]:
    """核验本次 Maa 作业及远端退出码，拒绝共享缓存中的旧 shell 输出。"""
    try:
        if not args or any(not isinstance(part, str) for part in args):
            return -1, "网络 shell 参数无效"
        marker = f"__MBD_SHELL_{uuid.uuid4().hex}__"
        # Maa 的 shell 输出缓存不绑定 job ID，必须由本次命令生成唯一尾标。
        script = (
            f"{shlex.join(args)}; mbd_shell_status=$?; "
            f"printf '\\n{marker}:%s\\n' \"$mbd_shell_status\""
        )
        command = shlex.join(("sh", "-c", script))
        if before_dispatch is not None:
            before_dispatch()
        job = controller.post_shell(command, 8000)
        job.wait()
        if job.succeeded is not True or job.failed is not False:
            return -1, "网络 shell 作业未成功"
        output = job.get()
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="strict")
        if not isinstance(output, str) or output.count(marker) != 1:
            return -1, "网络 shell 缺少本次唯一回执"
        receipt = re.fullmatch(
            rf"(.*?)\r?\n{re.escape(marker)}:([0-9]{{1,3}})\r?\n", output, re.DOTALL
        )
        if receipt is None or int(receipt[2]) > 255:
            return -1, "网络 shell 回执截断或退出码无效"
        return int(receipt[2]), receipt[1]
    except _IsolationCreationStopped:
        raise
    except Exception as exc:  # noqa: BLE001 - 通道/回执异常属于未知，不能猜测成功
        return -1, f"网络 shell 回执异常：{exc}"


_IDENTITY_COMMANDS = (
    ("cat", "/proc/sys/kernel/random/boot_id"),
    ("getprop", "ro.build.fingerprint"),
    ("settings", "get", "secure", "android_id"),
)


class BoundNetworkShell:
    """仅向本任务已核验设备发送命令；未知写回执绝不跨通道重放。"""

    def __init__(self, controller_getter, *, stopped=None):
        self._stopped = stopped or (lambda: False)
        self._check_stopping()
        self._controller_getter = controller_getter
        self._binding = self._controller_binding()
        self._identity = self._read_identity(lambda args: run_network_shell(controller_getter(), args), self._check_stopping)
        self._owner = None
        self._local_selected = False
        # 本机回退是可选能力，核验失败仍可使用当前 Maa 通道。
        try:
            self._check_stopping()
            self._owner = self._endpoint_owner()
            local_identity = self._read_identity(self._local_shell, self._check_stopping)
        except InterruptedError:
            raise
        except Exception:
            self._owner = None
        else:
            if local_identity != self._identity:
                raise DeviceIntegrityFailure("本机 ADB 与当前控制器设备身份不一致")
            try:
                owner = self._endpoint_owner()
            except Exception as exc:
                raise DeviceIntegrityFailure("本机 ADB 模拟器进程归属未确认") from exc
            if owner != self._owner:
                raise DeviceIntegrityFailure("本机 ADB 模拟器进程归属已改变")
        self._check_stopping()

    def _check_stopping(self):
        if self._stopped():
            raise InterruptedError("用户已停止任务")

    def _check_isolation_creation(self):
        if self._stopped():
            raise _IsolationCreationStopped("用户已停止任务，取消尚未派发的新隔离规则")

    def _controller_binding(self):
        try:
            info = self._controller_getter().info
            path, serial = info["adb_path"], info["adb_serial"]
            if not isinstance(path, str) or not isinstance(serial, str) or not path or not serial:
                raise ValueError("绑定缺失")
        except Exception as exc:
            raise DeviceIntegrityFailure("当前控制器缺少可信 ADB 绑定") from exc
        return path, serial

    @staticmethod
    def _read_identity(shell, check_stopping=lambda: None):
        values = []
        for command in _IDENTITY_COMMANDS:
            check_stopping()
            try:
                code, output = shell(command)
            except (InterruptedError, DeviceIntegrityFailure):
                raise
            except Exception as exc:
                raise DeviceIntegrityFailure(f"无法核验当前 Android 设备身份：{exc}") from exc
            check_stopping()
            if not isinstance(output, str):
                raise DeviceIntegrityFailure("Android 设备身份回执格式无效")
            value = output.strip()
            if code != 0 or not value or value == "null":
                raise DeviceIntegrityFailure("无法核验当前 Android 设备身份")
            values.append(value)
        if re.fullmatch(r"[0-9a-fA-F-]{36}", values[0]) is None:
            raise DeviceIntegrityFailure("Android 启动身份无效")
        return tuple(values)

    def _endpoint_owner(self):
        import psutil
        try:
            from ..device_runtime_guard import tcp_listeners, verify_endpoint_owner
        except ImportError:
            from device_runtime_guard import tcp_listeners, verify_endpoint_owner
        endpoint = self._binding[1]
        address, port_text = endpoint.rsplit(":", 1)
        port = int(port_text)
        listeners = tcp_listeners()
        exact = [entry for entry in listeners if entry.port == port and entry.address == address]
        routed = exact or [entry for entry in listeners if entry.port == port and entry.address == "0.0.0.0"]
        if len(routed) != 1:
            raise RuntimeError("当前 ADB 端口归属不唯一")
        verify_endpoint_owner(endpoint, routed[0].pid, listeners)
        process = psutil.Process(routed[0].pid)
        if process.name().lower() not in {"ld9boxheadless.exe", "mumuvmmheadless.exe"}:
            raise RuntimeError("当前 ADB 端口不属于受支持的模拟器进程")
        if not Path(self._binding[0]).is_file():
            raise RuntimeError("当前控制器的 ADB 路径无效")
        return process.pid, process.create_time(), process.exe()

    def _local_shell(self, args):
        binding = self._binding
        before_dispatch = self._check_isolation_creation if _is_isolation_creation(args) else None
        class LocalJob:
            def __init__(self, command, timeout):
                if before_dispatch is not None:
                    before_dispatch()
                self.result = subprocess.run(
                    [binding[0], "-s", binding[1], "shell", command],
                    capture_output=True, text=True, encoding="utf-8", errors="strict",
                    timeout=timeout / 1000, check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self.succeeded = self.result.returncode == 0
                self.failed = not self.succeeded
            def wait(self):
                return self
            def get(self):
                return self.result.stdout
        class LocalController:
            def post_shell(self, command, timeout):
                return LocalJob(command, timeout)
        return run_network_shell(LocalController(), args, before_dispatch=before_dispatch)

    def validate_current(self, *, observe_stop=True):
        check_stopping = self._check_stopping if observe_stop else (lambda: None)
        check_stopping()
        if self._controller_binding() != self._binding:
            raise DeviceIntegrityFailure("当前控制器的 ADB 绑定已改变")
        if self._local_selected:
            try:
                owner = self._endpoint_owner()
            except Exception as exc:
                raise DeviceIntegrityFailure("本机 ADB 模拟器进程归属未确认") from exc
            if self._owner is None or owner != self._owner:
                raise DeviceIntegrityFailure("本机 ADB 模拟器进程归属已改变")
            shell = self._local_shell
        else:
            # 开始跳车时 Maa 代理可能已换代；身份查询本身允许进入已核验回退。
            shell = self
        if self._read_identity(shell, check_stopping) != self._identity:
            raise DeviceIntegrityFailure("Android 设备身份已改变")
        if self._controller_binding() != self._binding:
            raise DeviceIntegrityFailure("设备核验期间控制器绑定已改变")
        if self._local_selected:
            try:
                owner = self._endpoint_owner()
            except Exception as exc:
                raise DeviceIntegrityFailure("设备核验期间模拟器进程归属未确认") from exc
            if owner != self._owner:
                raise DeviceIntegrityFailure("设备核验期间模拟器进程归属已改变")
        check_stopping()

    def __call__(self, args):
        try:
            if self._controller_binding() != self._binding:
                raise DeviceIntegrityFailure("当前控制器的 ADB 绑定已改变")
            if self._local_selected:
                # 必要恢复也会走此处；停止只禁止新隔离创建，不阻拦自有规则清理。
                self.validate_current(observe_stop=False)
                return self._local_shell(args)
            controller = self._controller_getter()
            if _is_isolation_creation(args):
                result = run_network_shell(controller, args, before_dispatch=self._check_isolation_creation)
            else:
                result = run_network_shell(controller, args)
            # 仅无副作用查询可以切换并重试。规则写入即使没有回执也可能已生效。
            probe = tuple(args)
            if len(probe) == 3 and probe[:2] == ("su", "-c"):
                probe = tuple(shlex.split(probe[2]))
            readonly = (
                probe in _IDENTITY_COMMANDS or probe == ("id", "-u")
                or probe in {("dumpsys", "package", GAME_PACKAGE), ("pidof", GAME_PACKAGE)}
                or (len(probe) == 2 and probe[0] == "cat" and re.fullmatch(r"/proc/[0-9]+/status", probe[1]))
                or probe in {("ip", "rule", "show"), ("ip", "-6", "rule", "show"),
                                   ("iptables", "-S"), ("ip6tables", "-S")}
            )
            if result[0] == -1 and readonly and self._owner is not None:
                self._local_selected = True
                self.validate_current(observe_stop=False)
                safe_print("AutoDisconnectJump network_transport=local-adb device_verified=true readonly_switch=true", flush=True)
                return self._local_shell(args)
            return result
        except (DeviceIntegrityFailure, _IsolationCreationStopped):
            raise
        except Exception as exc:
            return -1, f"已绑定网络通道核验失败：{exc}"


def resolve_game_uid(shell: AdbShell, *, stopped=None) -> int | None:
    """解析游戏进程的 Linux uid；``dumpsys`` 失败/超时时回退到 /proc。"""
    def query(args):
        if stopped is not None and stopped():
            raise InterruptedError("用户已停止任务")
        result = shell(args)
        if stopped is not None and stopped():
            raise InterruptedError("用户已停止任务")
        return result

    code, output = query(("dumpsys", "package", GAME_PACKAGE))
    if code == 0:
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("userId="):
                try:
                    return int(stripped.split("=", 1)[1].split()[0])
                except ValueError:
                    return None
    # 演奏中 dumpsys package 可能超时或输出被截断，导致“无法解析游戏
    # UID”进而门禁 fail-closed。回退到更轻量的 pidof + /proc/<pid>/status，
    # 它不需要包管理器、输出也小得多。
    code, output = query(("pidof", GAME_PACKAGE))
    if code != 0 or not output.strip():
        return None
    pid = output.strip().split()[0]
    code, status = query(("cat", f"/proc/{pid}/status"))
    if code != 0:
        return None
    for line in status.splitlines():
        if line.startswith("Uid:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


class _RootShellAdapter:
    """把需要 root 的 shell 命令统一走 ``su -c``。"""

    _shell: AdbShell
    # 设备 shell 是否为 root 的探测结果缓存；None 表示尚未探测。
    _root_shell_available: bool | None

    def _check_stopping(self):
        if self._stopped():
            raise InterruptedError("用户已停止任务")

    def _run_shell(self, args: Sequence[str]) -> tuple[int, str]:
        try:
            code, output = self._shell(args)
            if type(code) is not int or not isinstance(output, str):
                return -1, "shell 返回格式无效"
            return code, output
        except (DeviceIntegrityFailure, InterruptedError):
            raise
        except Exception as exc:  # noqa: BLE001 - 通道异常不能冒充规则不存在
            return -1, str(exc)

    def _shell_ok(self, args: Sequence[str]) -> bool:
        code, output = self._run_shell(args)
        return code == 0 and not output.strip()

    def _root_shell(self, args: Sequence[str], *, creating=False) -> tuple[int, str]:
        """执行需要 root 的命令；非 root shell 下用 ``su -c`` 提权。

        雷电等模拟器的 adb shell 默认是 uid 2000，iptables 会直接报
        "Permission denied"。这里先探测一次 ``id -u``，不是 root 就
        把整条命令交给 ``su -c``；设备没有 su 或拒绝提权时返回失败，
        保持 fail-closed。探测结果按实例缓存，避免每条命令多一次往返。
        """
        if self._root_shell_available is None:
            code, output = self._run_shell(("id", "-u"))
            self._root_shell_available = (
                code == 0 and output.strip() == "0"
            )
        # 提权探测可能耗时，返回后、真正写入新隔离规则前必须再次检查停止。
        # restore 不传 creating，保留停止期间必要清理自有规则的能力。
        if creating:
            self._check_stopping()
        if self._root_shell_available:
            return self._run_shell(args)
        # su -c 只收一个命令参数；具体传输层负责引用 argv，避免重复手工加引号。
        return self._run_shell(("su", "-c", shlex.join(args)))

    def _root_shell_ok(self, args: Sequence[str], *, creating=False) -> bool:
        code, output = self._root_shell(args, creating=creating)
        return code == 0 and not output.strip()


class GameNetworkGate(_RootShellAdapter):
    """按 UID 屏蔽/恢复游戏出口流量；退出时必须恢复网络。

    依赖 iptables 的 ``owner`` match。MuMu 12 的内核没有该模块，
    这类设备请使用 :class:`UidBlackholeGate`。
    """

    def __init__(self, shell: AdbShell, *, chain_suffix: str = "mbdr_game", uid: int | None = None, stopped=None) -> None:
        self._shell = shell
        self._stopped = stopped or (lambda: False)
        if re.fullmatch(r"[A-Za-z0-9_]+", chain_suffix) is None:
            raise ValueError("无效的网络规则链名")
        self._chain = f"OUTPUT_{chain_suffix}"
        self._blocked = False
        if uid is not None and (type(uid) is not int or uid < 0):
            raise ValueError("无效的网络规则 UID")
        self._uid: int | None = uid
        self._owned_tables: set[str] = set()
        self._uncertain_tables: set[str] = set()
        self._root_shell_available: bool | None = None
        # 最近一次失败的可读原因，供上层把 fail-closed 的细节交给用户。
        self.last_error: str | None = None

    def _table_rules(self, table: str) -> list[str] | None:
        code, output = self._root_shell((table, "-S"))
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if code != 0 or not lines or any(
            not line.startswith(("-P ", "-N ", "-A ")) for line in lines
        ):
            return None
        return lines

    def _chain_present(self, rules: list[str]) -> bool:
        return f"-N {self._chain}" in rules or any(
            self._chain in line.split()[1:] for line in rules
        )

    def _block_verified(self, table: str) -> bool:
        rules = self._table_rules(table)
        reject = f"-A {self._chain} -m owner --uid-owner {self._uid} -j REJECT"
        return rules is not None and (
            f"-N {self._chain}" in rules
            and rules.count(f"-A OUTPUT -j {self._chain}") == 1
            and any(line == reject or line.startswith(reject + " --reject-with ") for line in rules)
        )

    def block(self) -> bool:
        """双协议栈全部写入才成功；部分写入仍保留所有权并回滚。"""
        self._check_stopping()
        tables = ("iptables", "ip6tables")
        if self._blocked:
            verified = (
                self._owned_tables == set(tables) and not self._uncertain_tables
                and all(self._block_verified(table) for table in tables)
            )
            if not verified:
                self.last_error = "iptables 已有屏蔽状态未通过双栈读回复核"
            return verified
        self.last_error = None
        uid = self._uid if self._uid is not None else resolve_game_uid(self._run_shell, stopped=self._stopped)
        if uid is None:
            self.last_error = "无法解析游戏 UID"
            return False
        self._uid = uid
        for table in tables:
            rules = self._table_rules(table)
            if rules is None or self._chain_present(rules):
                if rules is not None:
                    self._uncertain_tables.add(table)
                self.last_error = f"{table} 查询失败或存在无所有权的链；需人工核验"
                return False
        for table in tables:
            created = self._root_shell_ok((table, "-N", self._chain), creating=True)
            if created:
                self._owned_tables.add(table)
            else:
                self._uncertain_tables.add(table)
            rejected = created and self._root_shell_ok(
                (table, "-A", self._chain, "-m", "owner", "--uid-owner", str(uid), "-j", "REJECT"), creating=True
            )
            linked = rejected and self._root_shell_ok(
                (table, "-I", "OUTPUT", "1", "-j", self._chain), creating=True
            )
            if not linked or not self._block_verified(table):
                error = f"{table} 屏蔽规则写入失败"
                if not self.restore():
                    self.last_error = f"{error}；回滚未确认：{self.last_error}"
                else:
                    self.last_error = error
                return False
        self._blocked = True
        return True

    def restore(self) -> bool:
        """仅清理本实例创建的链，命令和读回均成功才解除未决状态。"""
        restored = True
        for table in tuple(self._owned_tables | self._uncertain_tables):
            rules = self._table_rules(table)
            if rules is not None and not self._chain_present(rules):
                self._owned_tables.discard(table)
                self._uncertain_tables.discard(table)
                continue
            if table not in self._owned_tables:
                restored = False
                continue
            flushed = self._root_shell_ok((table, "-F", self._chain))
            rules = self._table_rules(table)
            link = f"-A OUTPUT -j {self._chain}"
            unlinked = rules is not None
            if rules is not None and rules.count(link) == 1:
                unlinked = self._root_shell_ok((table, "-D", "OUTPUT", "-j", self._chain))
            elif rules is not None and rules.count(link) > 1:
                unlinked = False
            deleted = flushed and unlinked and self._root_shell_ok((table, "-X", self._chain))
            rules = self._table_rules(table)
            if deleted and rules is not None and not self._chain_present(rules):
                self._owned_tables.discard(table)
            else:
                restored = False
        self._blocked = not restored
        if not restored:
            self.last_error = "iptables 恢复未确认：清理失败、查询失败或缺少所有权；需人工核验"
        return restored

    def __enter__(self) -> "GameNetworkGate":
        self.block()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.restore()


class UidBlackholeGate(_RootShellAdapter):
    """用 uidrange 策略路由屏蔽游戏出口流量。

    MuMu 12 的内核没有 iptables ``owner`` match，``GameNetworkGate`` 在
    这类设备上写不了规则；改用 ``ip rule ... blackhole`` 按 UID 黑洞，
    ADB（adbd 的 UID）不受影响。pref 必须小于 Android 自带的 fwmark
    规则（16000），否则游戏带 fwmark 的包会先命中那里。
    """

    def __init__(
        self, shell: AdbShell, *, pref: int = 100, uid: int | None = None, stopped=None
    ) -> None:
        self._shell = shell
        self._stopped = stopped or (lambda: False)
        self._pref = int(pref)
        if not 0 <= self._pref < 16000 or (uid is not None and int(uid) < 0):
            raise ValueError("无效的网络规则 UID 或优先级")
        # 任务入口可以在控制器空闲时预先解析 UID；跳车发生在引擎刚停止之后，
        # 那时反向控制器可能已被替换，post_shell 会立即失败（2026-10-23 实测）。
        self._uid: int | None = int(uid) if uid is not None else None
        self._blocked = False
        self._owned_families: set[bool] = set()
        self._uncertain_families: set[bool] = set()
        self._root_shell_available: bool | None = None
        # 最近一次失败的可读原因，供上层把 fail-closed 的细节交给用户。
        self.last_error: str | None = None

    def _resolve_uid(self) -> int | None:
        if self._uid is not None:
            return self._uid
        # 跳车时控制器状态可能瞬时不可用；有界重试后再 fail-closed。
        for _ in range(3):
            self._check_stopping()
            uid = resolve_game_uid(self._run_shell, stopped=self._stopped)
            if uid is not None:
                return uid
            time.sleep(0.3)
        return None

    def _family(self, *, ipv6: bool) -> tuple[str, ...]:
        return ("ip", "-6") if ipv6 else ("ip",)

    def _rule_args(self, *, ipv6: bool) -> tuple[str, ...]:
        return (
            *self._family(ipv6=ipv6),
            "rule", "add", "uidrange", f"{self._uid}-{self._uid}",
            "blackhole", "pref", str(self._pref),
        )

    def _delete_args(self, *, ipv6: bool) -> tuple[str, ...]:
        return (
            *self._family(ipv6=ipv6),
            "rule", "del", "uidrange", f"{self._uid}-{self._uid}",
            "blackhole", "pref", str(self._pref),
        )

    def _rule_count(self, *, ipv6: bool, pref_must_be_empty: bool = False) -> int | None:
        code, output = self._root_shell(
            (*self._family(ipv6=ipv6), "rule", "show")
        )
        if code != 0 or not output.strip():
            return None
        count = 0
        for line in output.splitlines():
            parsed = re.fullmatch(r"\s*(\d+):\s+(.+?)\s*", line)
            if parsed is None:
                return None
            tokens = parsed[2].split()
            if (
                pref_must_be_empty and int(parsed[1]) <= self._pref
                and not (int(parsed[1]) == 0 and tokens == ["from", "all", "lookup", "local"])
            ):
                return None
            # 只接受完整选择器；含额外 from/to/fwmark 的规则不属于本实例。
            expected = ["from", "all", "uidrange", f"{self._uid}-{self._uid}", "blackhole"]
            rule_uid = None
            if "uidrange" in tokens:
                index = tokens.index("uidrange") + 1
                if index >= len(tokens) or re.fullmatch(r"\d+-\d+", tokens[index]) is None:
                    return None
                rule_uid = tokens[index]
            if (
                int(parsed[1]) == self._pref and "blackhole" in tokens
                and rule_uid == f"{self._uid}-{self._uid}"
                and tokens != expected
            ):
                # 完整删除选择器不能区分的同类规则先拒绝，不借清理机会接管。
                return None
            if int(parsed[1]) == self._pref and tokens == expected:
                count += 1
        return count

    def _rule_present(self, *, ipv6: bool) -> RulePresence:
        count = self._rule_count(ipv6=ipv6)
        if count is None:
            return RulePresence.UNKNOWN
        return RulePresence.PRESENT if count else RulePresence.ABSENT

    def block(self) -> bool:
        """把游戏 UID 的出口流量黑洞掉；重复调用是幂等的。"""
        self._check_stopping()
        if self._owned_families or self._uncertain_families:
            verified = (
                self._owned_families == {False, True}
                and not self._uncertain_families
                and all(self._rule_count(ipv6=ipv6) == 1 for ipv6 in (False, True))
            )
            if not verified:
                self.last_error = "uidrange 已有屏蔽状态未通过双栈读回复核"
            return verified
        self.last_error = None
        uid = self._resolve_uid()
        if uid is None:
            self.last_error = "无法解析游戏 UID"
            return False
        self._uid = uid
        if not self.cleanup_stale():
            return False
        # 更高或同优先级 lookup 可能先接收流量；只豁免系统标准 local 表。
        for ipv6 in (False, True):
            if self._rule_count(ipv6=ipv6, pref_must_be_empty=True) != 0:
                family = "IPv6" if ipv6 else "IPv4"
                self.last_error = f"{family} 优先级 {self._pref} 之前存在冲突或查询失败；保留现有规则"
                return False
        for ipv6 in (False, True):
            family = "IPv6" if ipv6 else "IPv4"
            written = self._root_shell_ok(self._rule_args(ipv6=ipv6), creating=True)
            if written:
                self._owned_families.add(ipv6)
            count = self._rule_count(ipv6=ipv6)
            if not written or count != 1:
                # 命令失败却出现规则时缺少可靠所有权，不擅自删除。
                if not written and count != 0:
                    self._uncertain_families.add(ipv6)
                self.last_error = f"{family} uidrange 规则写入或读回复核失败"
                error = self.last_error
                if not self.restore():
                    self.last_error = f"{error}；回滚未确认：{self.last_error}"
                else:
                    self.last_error = error
                return False
        self._blocked = True
        return True

    def restore(self) -> bool:
        """删除规则并确认没有残留；重复调用是幂等的。"""
        if self._uid is None:
            return True
        restored = True
        for ipv6 in (False, True):
            count = self._rule_count(ipv6=ipv6)
            deleted = True
            if ipv6 in self._owned_families and count == 1:
                # 删除必须带完整选择器；未知或重复规则时保留所有权并等待核验。
                deleted = self._root_shell_ok(self._delete_args(ipv6=ipv6))
                count = self._rule_count(ipv6=ipv6)
                if not deleted:
                    restored = False
            if count == 0 and deleted:
                self._owned_families.discard(ipv6)
                self._uncertain_families.discard(ipv6)
            else:
                restored = False
        self._blocked = not restored
        if not restored:
            self.last_error = "uidrange 恢复未确认：规则仍在、查询失败或缺少所有权；需人工核验"
        return restored

    def cleanup_stale(self) -> bool:
        """仅清理本实例规则；无持久所有权凭据时拒绝接管崩溃遗留。"""
        if self._uid is None:
            self._uid = self._resolve_uid()
            if self._uid is None:
                self.last_error = "无法解析游戏 UID"
                return False
        return self.restore()

    def __enter__(self) -> "UidBlackholeGate":
        self.block()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.restore()
