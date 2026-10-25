"""历史协力跳车实现的网络控制（当前联机流程不调用）：按游戏 UID 屏蔽/恢复出口流量。

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
from collections.abc import Callable, Sequence
from enum import Enum


GAME_PACKAGE = "com.bilibili.star.bili"

# adb_shell 的调用协议：输入 shell 参数列表，返回 (returncode, output)。
AdbShell = Callable[[Sequence[str]], tuple[int, str]]


class RulePresence(Enum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


def run_network_shell(controller, args: Sequence[str]) -> tuple[int, str]:
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
    except Exception as exc:  # noqa: BLE001 - 通道/回执异常属于未知，不能猜测成功
        return -1, f"网络 shell 回执异常：{exc}"


def resolve_game_uid(shell: AdbShell) -> int | None:
    """解析游戏进程的 Linux uid；``dumpsys`` 失败/超时时回退到 /proc。"""
    code, output = shell(("dumpsys", "package", GAME_PACKAGE))
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
    code, output = shell(("pidof", GAME_PACKAGE))
    if code != 0 or not output.strip():
        return None
    pid = output.strip().split()[0]
    code, status = shell(("cat", f"/proc/{pid}/status"))
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

    def _run_shell(self, args: Sequence[str]) -> tuple[int, str]:
        try:
            code, output = self._shell(args)
            if type(code) is not int or not isinstance(output, str):
                return -1, "shell 返回格式无效"
            return code, output
        except Exception as exc:  # noqa: BLE001 - 通道异常不能冒充规则不存在
            return -1, str(exc)

    def _shell_ok(self, args: Sequence[str]) -> bool:
        code, output = self._run_shell(args)
        return code == 0 and not output.strip()

    def _root_shell(self, args: Sequence[str]) -> tuple[int, str]:
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
        if self._root_shell_available:
            return self._run_shell(args)
        # su -c 只收一个命令参数；具体传输层负责引用 argv，避免重复手工加引号。
        return self._run_shell(("su", "-c", shlex.join(args)))

    def _root_shell_ok(self, args: Sequence[str]) -> bool:
        code, output = self._root_shell(args)
        return code == 0 and not output.strip()


class GameNetworkGate(_RootShellAdapter):
    """按 UID 屏蔽/恢复游戏出口流量；退出时必须恢复网络。

    依赖 iptables 的 ``owner`` match。MuMu 12 的内核没有该模块，
    这类设备请使用 :class:`UidBlackholeGate`。
    """

    def __init__(self, shell: AdbShell, *, chain_suffix: str = "mbdr_game") -> None:
        self._shell = shell
        if re.fullmatch(r"[A-Za-z0-9_]+", chain_suffix) is None:
            raise ValueError("无效的网络规则链名")
        self._chain = f"OUTPUT_{chain_suffix}"
        self._blocked = False
        self._uid: int | None = None
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
        uid = resolve_game_uid(self._run_shell)
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
            created = self._root_shell_ok((table, "-N", self._chain))
            if created:
                self._owned_tables.add(table)
            else:
                self._uncertain_tables.add(table)
            rejected = created and self._root_shell_ok(
                (table, "-A", self._chain, "-m", "owner", "--uid-owner", str(uid), "-j", "REJECT")
            )
            linked = rejected and self._root_shell_ok(
                (table, "-I", "OUTPUT", "1", "-j", self._chain)
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
        self, shell: AdbShell, *, pref: int = 100, uid: int | None = None
    ) -> None:
        self._shell = shell
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
            uid = resolve_game_uid(self._run_shell)
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
            written = self._root_shell_ok(self._rule_args(ipv6=ipv6))
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
