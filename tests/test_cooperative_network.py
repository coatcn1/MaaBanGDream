from __future__ import annotations

import pytest

from agent.realtime.cooperative_network import (
    GameNetworkGate,
    GAME_PACKAGE,
    UidBlackholeGate,
    resolve_game_uid,
)


def _fake_shell(
    uid: int | None,
    *,
    deny_iptables: bool = False,
    root: bool = True,
    su_available: bool = True,
):
    calls: list[tuple[str, ...]] = []
    rules: set[str] = set()
    chains: set[str] = set()

    def shell(args):
        calls.append(tuple(args))
        if args[0] == "dumpsys":
            body = f"userId={uid} appId=10123" if uid is not None else ""
            return (0, body)
        if args[0] == "id":
            return (0, "0" if root else "2000")
        if args[0] == "su":
            if not su_available:
                return (127, "su: not found")
            # su -c 把整条命令作为单个字符串参数，这里拆回原始命令执行。
            return shell(tuple(args[2].strip('"').split()))
        if args[0] == "iptables":
            if deny_iptables:
                return (127, "Permission denied")
            if args[1] == "-N":
                chains.add(args[2])
                return (0, "")
            if args[1] == "-L":
                exists = args[2] in chains
                return (0 if exists else 1, "")
            if args[1] in {"-I", "-A", "-F", "-D", "-X"}:
                if args[1] in {"-I", "-A"}:
                    rules.add(" ".join(args[2:]))
                elif args[1] == "-X":
                    rules.clear()
                    chains.discard(args[2])
                return (0, "")
        return (0, "")

    return shell, calls, rules


def test_resolve_game_uid_parses_dumpsys_output():
    shell, calls, _ = _fake_shell(uid=10123)

    assert resolve_game_uid(shell) == 10123
    assert calls[0][1] == "package"
    assert calls[0][2] == GAME_PACKAGE


def test_resolve_game_uid_returns_none_when_missing():
    shell, _, _ = _fake_shell(uid=None)

    assert resolve_game_uid(shell) is None


def test_resolve_game_uid_falls_back_to_proc_status_when_dumpsys_fails():
    def shell(args):
        if args[0] == "dumpsys":
            return (127, "dumpsys failed")
        if args[0] == "pidof":
            return (0, "6757 6931")
        if args[0] == "cat":
            return (0, "Uid:\t10052\t10052\t10052\t10052\nGid:\t10052\n")
        return (0, "")

    assert resolve_game_uid(shell) == 10052


def test_gate_blocks_and_restores_game_traffic():
    shell, calls, rules = _fake_shell(uid=10123)
    gate = GameNetworkGate(shell)

    assert gate.block() is True
    assert any("REJECT" in rule for rule in rules)
    # 幂等
    assert gate.block() is True

    assert gate.restore() is True
    assert gate._blocked is False


def test_gate_restores_network_via_context_manager_on_exception():
    shell, calls, _ = _fake_shell(uid=10123)
    gate = GameNetworkGate(shell)

    with pytest.raises(RuntimeError):
        with gate:
            assert gate._blocked is True
            raise RuntimeError("boom")

    assert gate._blocked is False
    # restore 阶段确实执行了 iptables 清理
    assert any(call[:2] == ("iptables", "-F") for call in calls)
    assert any(call[:2] == ("iptables", "-X") for call in calls)


def test_gate_fails_closed_without_root_or_uid():
    no_iptables = GameNetworkGate(_fake_shell(uid=10123, deny_iptables=True)[0])
    assert no_iptables.block() is False
    assert no_iptables.last_error is not None
    assert no_iptables.restore() is True

    no_uid = GameNetworkGate(_fake_shell(uid=None)[0])
    assert no_uid.block() is False
    assert no_uid.last_error == "无法解析游戏 UID"
    assert no_uid.restore() is True


def test_gate_escalates_via_su_when_shell_is_not_root():
    shell, calls, rules = _fake_shell(uid=10123, root=False)
    gate = GameNetworkGate(shell)

    assert gate.block() is True
    assert any("REJECT" in rule for rule in rules)
    # 提权路径必须走 su -c，而不是直接执行 iptables。
    assert any(call[0] == "su" for call in calls)

    assert gate.restore() is True


def test_gate_fails_closed_without_su_on_unrooted_shell():
    gate = GameNetworkGate(
        _fake_shell(uid=10123, root=False, su_available=False)[0]
    )

    assert gate.block() is False
    assert gate.last_error is not None
    assert gate.restore() is True


def _fake_uid_rule_shell(
    uid: int | None = 10046,
    *,
    root: bool = True,
    su_available: bool = True,
    deny_ip: bool = False,
):
    calls: list[tuple[str, ...]] = []
    rules_v4: set[str] = set()
    rules_v6: set[str] = set()

    def shell(args):
        calls.append(tuple(args))
        if args[0] == "dumpsys":
            return (0, f"userId={uid}" if uid is not None else "")
        if args[0] == "id":
            return (0, "0" if root else "2000")
        if args[0] == "su":
            if not su_available:
                return (127, "su: not found")
            return shell(tuple(args[2].strip('"').split()))
        if args[0] == "ip":
            if deny_ip:
                return (127, "Permission denied")
            ipv6 = args[1] == "-6"
            rest = args[2:] if ipv6 else args[1:]
            store = rules_v6 if ipv6 else rules_v4
            if rest[:2] == ("rule", "add"):
                store.add(rest[3])
                return (0, "")
            if rest[:2] == ("rule", "del"):
                if rest[2] == "uidrange":
                    store.discard(rest[3])
                elif rest[2] == "pref":
                    store.clear()
                return (0, "")
            if rest[:2] == ("rule", "show"):
                return (
                    0,
                    "\n".join(
                        f"100:\tfrom all uidrange {rule} blackhole"
                        for rule in sorted(store)
                    ),
                )
        return (0, "")

    return shell, calls, rules_v4, rules_v6


def test_uid_gate_uses_cached_uid_without_resolving_during_jump():
    """跳车时控制器可能已失效；任务入口缓存 UID 后不再依赖 dumpsys。"""
    shell, calls, rules_v4, _ = _fake_uid_rule_shell(uid=None)
    gate = UidBlackholeGate(shell, uid=10046)

    assert gate.block() is True
    assert any("10046-10046" in rule for rule in rules_v4)
    assert not any(call[0] == "dumpsys" for call in calls)


def test_uid_blackhole_gate_blocks_and_restores():
    shell, _, rules_v4, rules_v6 = _fake_uid_rule_shell()
    gate = UidBlackholeGate(shell)

    assert gate.block() is True
    assert rules_v4 == {"10046-10046"}
    assert rules_v6 == {"10046-10046"}
    # 幂等
    assert gate.block() is True

    assert gate.restore() is True
    assert rules_v4 == set()
    assert rules_v6 == set()
    assert gate._blocked is False


def test_uid_blackhole_gate_cleanup_stale_removes_leftovers():
    shell, _, rules_v4, rules_v6 = _fake_uid_rule_shell()
    rules_v4.add("99999-99999")
    rules_v6.add("99999-99999")
    gate = UidBlackholeGate(shell)

    assert gate.cleanup_stale() is True
    assert rules_v4 == set()
    assert rules_v6 == set()


def test_uid_blackhole_gate_fails_without_uid():
    gate = UidBlackholeGate(_fake_uid_rule_shell(uid=None)[0])

    assert gate.block() is False
    assert gate.last_error == "无法解析游戏 UID"
    assert gate.restore() is True


def test_uid_blackhole_gate_fails_closed_without_root_permission():
    shell, _, rules_v4, _ = _fake_uid_rule_shell(deny_ip=True)
    gate = UidBlackholeGate(shell)

    assert gate.block() is False
    assert gate.last_error is not None
    assert rules_v4 == set()


def test_uid_blackhole_gate_escalates_via_su():
    shell, calls, rules_v4, _ = _fake_uid_rule_shell(root=False)
    gate = UidBlackholeGate(shell)

    assert gate.block() is True
    assert rules_v4 == {"10046-10046"}
    assert any(call[0] == "su" for call in calls)


def test_uid_blackhole_gate_restores_via_context_manager_on_exception():
    shell, _, rules_v4, rules_v6 = _fake_uid_rule_shell()
    gate = UidBlackholeGate(shell)

    with pytest.raises(RuntimeError):
        with gate:
            assert gate._blocked is True
            raise RuntimeError("boom")

    assert gate._blocked is False
    assert rules_v4 == set()
    assert rules_v6 == set()
