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
    """从 dumpsys 解析游戏进程的 Linux uid；失败返回 None。"""
    code, output = shell(("dumpsys", "package", GAME_PACKAGE))
    if code != 0:
        return None
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("userId="):
            try:
                return int(stripped.split("=", 1)[1].split()[0])
            except ValueError:
                return None
    return None


class GameNetworkGate:
    """按 UID 屏蔽/恢复游戏出口流量；退出时必须恢复网络。"""

    def __init__(self, shell: AdbShell, *, chain_suffix: str = "mbdr_game") -> None:
        self._shell = shell
        self._chain = f"OUTPUT_{chain_suffix}"
        self._blocked = False

    def _shell_ok(self, args: Sequence[str]) -> bool:
        code, _ = self._shell(args)
        return code == 0

    def block(self) -> bool:
        """把游戏 UID 的出口流量 REJECT；重复调用是幂等的。"""
        if self._blocked:
            return True
        uid = resolve_game_uid(self._shell)
        if uid is None:
            return False
        # 自建链便于精确恢复，不污染用户既有 OUTPUT 规则。
        if not self._shell_ok(
            ("iptables", "-N", self._chain)
        ):
            # 链已存在视为幂等成功。
            code, _ = self._shell(("iptables", "-L", self._chain))
            if code != 0:
                return False
        created = self._shell_ok(
            (
                "iptables", "-I", "OUTPUT", "1",
                "-j", self._chain,
            )
        )
        rejected = self._shell_ok(
            (
                "iptables", "-A", self._chain,
                "-m", "owner", "--uid-owner", str(uid),
                "-j", "REJECT",
            )
        )
        self._blocked = created and rejected
        return self._blocked

    def restore(self) -> bool:
        """恢复网络：删除规则并清空自建链，容忍部分命令失败。"""
        if not self._blocked:
            return True
        self._shell(("iptables", "-F", self._chain))
        self._shell(("iptables", "-D", "OUTPUT", "-j", self._chain))
        self._shell(("iptables", "-X", self._chain))
        self._blocked = False
        code, _ = self._shell(("iptables", "-L", self._chain))
        return code != 0  # 链已删除才算恢复成功

    def __enter__(self) -> "GameNetworkGate":
        self.block()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.restore()
