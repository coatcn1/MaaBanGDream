"""协力“断网跳车”的网络控制：按游戏 UID 屏蔽/恢复出口流量。

约束（见 AGENTS 第 22 条）：
- 禁止 `svc wifi` / 飞行模式——它们会顺带打断 MuMu 的 adb 通道，导致
  引擎截图与触控全部中断；
- 必须用 iptables 按游戏 UID 屏蔽出口流量（需要 root），并且 finally
  恢复网络、有界重试，绝不把模拟器留在断网状态。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence


GAME_PACKAGE = "com.bilibili.star.bili"

# adb_shell 的调用协议：输入 shell 参数列表，返回 (returncode, output)。
AdbShell = Callable[[Sequence[str]], tuple[int, str]]


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


class GameNetworkGate:
    """按 UID 屏蔽/恢复游戏出口流量；退出时必须恢复网络。"""

    def __init__(self, shell: AdbShell, *, chain_suffix: str = "mbdr_game") -> None:
        self._shell = shell
        self._chain = f"OUTPUT_{chain_suffix}"
        self._blocked = False
        # 设备 shell 是否为 root 的探测结果缓存；None 表示尚未探测。
        self._root_shell_available: bool | None = None
        # 最近一次失败的可读原因，供上层把 fail-closed 的细节交给用户。
        self.last_error: str | None = None

    def _shell_ok(self, args: Sequence[str]) -> bool:
        code, _ = self._shell(args)
        return code == 0

    def _root_shell(self, args: Sequence[str]) -> tuple[int, str]:
        """执行需要 root 的命令；非 root shell 下用 ``su -c`` 提权。

        雷电等模拟器的 adb shell 默认是 uid 2000，iptables 会直接报
        "Permission denied"。这里先探测一次 ``id -u``，不是 root 就
        把整条命令交给 ``su -c``；设备没有 su 或拒绝提权时返回失败，
        保持 fail-closed。探测结果按实例缓存，避免每条命令多一次往返。
        """
        if self._root_shell_available is None:
            code, output = self._shell(("id", "-u"))
            self._root_shell_available = (
                code == 0 and output.strip() == "0"
            )
        if self._root_shell_available:
            return self._shell(args)
        # adb shell 会按空白把参数拆开，su -c 只收一个命令参数；这里把
        # 整条命令用引号包成一个 token，避免 `su -c iptables -L ...`
        # 被拆成多个选项。
        joined = " ".join(str(part) for part in args)
        return self._shell(("su", "-c", f'"{joined}"'))

    def _root_shell_ok(self, args: Sequence[str]) -> bool:
        code, _ = self._root_shell(args)
        return code == 0

    def block(self) -> bool:
        """把游戏 UID 的出口流量 REJECT；重复调用是幂等的。"""
        if self._blocked:
            return True
        self.last_error = None
        uid = resolve_game_uid(self._shell)
        if uid is None:
            self.last_error = "无法解析游戏 UID"
            return False
        # 自建链便于精确恢复，不污染用户既有 OUTPUT 规则。
        if not self._root_shell_ok(
            ("iptables", "-N", self._chain)
        ):
            # 链已存在视为幂等成功。
            code, output = self._root_shell(("iptables", "-L", self._chain))
            if code != 0:
                self.last_error = (
                    f"缺少 root 权限或 iptables 不可用：{output.strip()}"
                )
                return False
        created = self._root_shell_ok(
            (
                "iptables", "-I", "OUTPUT", "1",
                "-j", self._chain,
            )
        )
        rejected = self._root_shell_ok(
            (
                "iptables", "-A", self._chain,
                "-m", "owner", "--uid-owner", str(uid),
                "-j", "REJECT",
            )
        )
        self._blocked = created and rejected
        if not self._blocked:
            self.last_error = "iptables 屏蔽规则写入失败"
        return self._blocked

    def restore(self) -> bool:
        """恢复网络：删除规则并清空自建链，容忍部分命令失败。"""
        if not self._blocked:
            return True
        self._root_shell(("iptables", "-F", self._chain))
        self._root_shell(("iptables", "-D", "OUTPUT", "-j", self._chain))
        self._root_shell(("iptables", "-X", self._chain))
        self._blocked = False
        code, _ = self._root_shell(("iptables", "-L", self._chain))
        return code != 0  # 链已删除才算恢复成功

    def __enter__(self) -> "GameNetworkGate":
        self.block()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.restore()
