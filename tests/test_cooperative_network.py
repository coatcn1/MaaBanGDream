from __future__ import annotations

import re
import shlex
from types import SimpleNamespace

import pytest

from agent.realtime.cooperative_network import (
    GameNetworkGate,
    GAME_PACKAGE,
    RulePresence,
    UidBlackholeGate,
    resolve_game_uid,
    run_network_shell,
)


def test_iptables_gate_uses_preflight_uid_without_package_query():
    shell, calls, _ = _fake_shell(None)
    gate = GameNetworkGate(shell, uid=10123)
    assert gate.block() is True
    assert not any(command[0] in {"dumpsys", "pidof"} for command in calls)
    assert gate.restore() is True


@pytest.mark.parametrize("gate_type", [GameNetworkGate, UidBlackholeGate])
@pytest.mark.parametrize("stop_at", ["root-query", "first-create"])
def test_stop_blocks_new_rules_but_allows_owned_cleanup(gate_type, stop_at):
    if gate_type is UidBlackholeGate:
        shell = RuleShell()
        calls = shell.calls
    else:
        shell, calls, _ = _fake_shell(10123)
    state = {"stopped": False}
    def stopped_shell(args):
        result = shell(args)
        is_create = (args[0] == "iptables" and args[1] == "-N"
                     or args[:3] == ("ip", "rule", "add"))
        if (stop_at == "root-query" and args == ("id", "-u")) or (stop_at == "first-create" and is_create):
            state["stopped"] = True
        return result
    gate = gate_type(stopped_shell, uid=10123, stopped=lambda: state["stopped"])
    with pytest.raises(InterruptedError): gate.block()
    creates = [command for command in calls if (command[0] in {"iptables", "ip6tables"} and command[1] in {"-N", "-A", "-I"})
               or command[:3] == ("ip", "rule", "add") or command[:4] == ("ip", "-6", "rule", "add")]
    assert len(creates) == (0 if stop_at == "root-query" else 1)
    assert gate.restore() is True


def test_uid_preflight_stop_prevents_proc_fallback_queries():
    state = {"stopped": False}
    calls = []
    def shell(args):
        calls.append(args)
        state["stopped"] = True
        return -1, "dumpsys failed during stop"
    with pytest.raises(InterruptedError):
        resolve_game_uid(shell, stopped=lambda: state["stopped"])
    assert calls == [("dumpsys", "package", GAME_PACKAGE)]


def _fake_shell(
    uid: int | None,
    *,
    deny_iptables: bool = False,
    root: bool = True,
    su_available: bool = True,
):
    calls: list[tuple[str, ...]] = []
    rules: set[str] = set()
    chains = {"iptables": set(), "ip6tables": set()}
    entries = {"iptables": [], "ip6tables": []}

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
        if args[0] in {"iptables", "ip6tables"}:
            if deny_iptables:
                return (127, "Permission denied")
            owned = chains[args[0]]
            store = entries[args[0]]
            if args[1] == "-S":
                return 0, "\n".join([
                    "-P OUTPUT ACCEPT",
                    *(f"-N {chain}" for chain in sorted(owned)),
                    *(" ".join(rule) for rule in store),
                ])
            if args[1] == "-N":
                if args[2] in owned:
                    return 1, "Chain already exists"
                owned.add(args[2])
                return (0, "")
            if args[1] == "-L":
                exists = args[2] in owned
                return (0 if exists else 1, "")
            if args[1] in {"-I", "-A", "-F", "-D", "-X"}:
                if args[1] in {"-I", "-A"}:
                    rules.add(" ".join(args[2:]))
                    entry = ("-A", args[2], *args[4:]) if args[1] == "-I" else tuple(args[1:])
                    store.append(entry)
                elif args[1] == "-F":
                    store[:] = [rule for rule in store if rule[1] != args[2]]
                elif args[1] == "-D":
                    entry = ("-A", *args[2:])
                    if entry not in store:
                        return 1, "No matching rule"
                    store.remove(entry)
                elif args[1] == "-X":
                    if any(args[2] in rule for rule in store):
                        return 1, "Chain still referenced"
                    owned.discard(args[2])
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
                    "0: from all lookup local\n" + "\n".join(
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


def test_uid_blackhole_gate_cleanup_stale_preserves_other_uid():
    shell, _, rules_v4, rules_v6 = _fake_uid_rule_shell()
    rules_v4.add("99999-99999")
    rules_v6.add("99999-99999")
    gate = UidBlackholeGate(shell)

    assert gate.cleanup_stale() is True
    assert rules_v4 == {"99999-99999"}
    assert rules_v6 == {"99999-99999"}


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


class RuleShell:
    """保留同优先级多条规则及删除首条的真实语义。"""

    def __init__(self):
        self.rules = {False: [], True: []}
        self.calls = []
        self.fail_show = set()
        self.fail_add = set()
        self.fail_delete = set()

    def __call__(self, args):
        args = tuple(args)
        self.calls.append(args)
        if args == ("id", "-u"):
            return 0, "0"
        if args[0] != "ip":
            return 127, "unexpected command"
        ipv6 = args[1] == "-6"
        rest = args[2:] if ipv6 else args[1:]
        store = self.rules[ipv6]
        if rest == ("rule", "show"):
            if ipv6 in self.fail_show:
                return 1, "Permission denied"
            return 0, "0: from all lookup local\n" + "\n".join(store)
        if rest[:2] == ("rule", "add"):
            if ipv6 in self.fail_add:
                return 1, "Operation not supported"
            store.append(f"{rest[-1]}: from all uidrange {rest[3]} blackhole")
            return 0, ""
        if rest[:2] == ("rule", "del"):
            if ipv6 in self.fail_delete:
                return 1, "Permission denied"
            for index, line in enumerate(store):
                if rest[2] == "pref":
                    matches = line.startswith(f"{rest[3]}:")
                else:
                    matches = line == f"{rest[-1]}: from all uidrange {rest[3]} blackhole"
                if matches:
                    store.pop(index)
                    return 0, ""
            return 2, "No such file or directory"
        return 127, "unexpected command"


def test_uid_restore_query_failure_keeps_recovery_pending():
    shell = RuleShell()
    gate = UidBlackholeGate(shell, uid=10046)
    assert gate.block()
    shell.fail_delete.add(False)
    shell.fail_show.add(False)
    assert gate.restore() is False
    assert gate._blocked is True
    shell.fail_delete.clear()
    shell.fail_show.clear()
    assert gate.restore() is True
    assert shell.rules == {False: [], True: []}


def test_uid_cleanup_never_deletes_unowned_same_pref_rules():
    shell = RuleShell()
    shell.rules[False] = [
        "100: from all lookup 123",
        "100: from all uidrange 99999-99999 blackhole",
        "100: from all uidrange 10046-10046 blackhole",
    ]
    original = list(shell.rules[False])
    gate = UidBlackholeGate(shell, uid=10046)
    assert gate.cleanup_stale() is False
    assert gate.restore() is False
    assert shell.rules[False] == original
    assert not any("del" in call for call in shell.calls)


def test_uid_block_does_not_adopt_preexisting_rule():
    shell = RuleShell()
    shell.rules[True] = ["100: from all uidrange 10046-10046 blackhole"]
    gate = UidBlackholeGate(shell, uid=10046)
    assert gate.block() is False
    assert not any("add" in call for call in shell.calls)
    assert gate.restore() is False
    assert len(shell.rules[True]) == 1


def test_uid_ipv6_failure_rolls_back_ipv4():
    shell = RuleShell()
    shell.fail_add.add(True)
    gate = UidBlackholeGate(shell, uid=10046)
    assert gate.block() is False
    assert gate.restore() is True
    assert shell.rules == {False: [], True: []}


def test_uid_rule_at_other_pref_is_not_our_rule():
    shell = RuleShell()
    shell.rules[False] = ["101: from all uidrange 10046-10046 blackhole"]
    gate = UidBlackholeGate(shell, uid=10046)
    # 无关规则不能冒充本实例成功写入，也不能在恢复时被删除。
    assert gate.block() is True
    assert gate.restore() is True
    assert shell.rules[False] == ["101: from all uidrange 10046-10046 blackhole"]


def test_uid_block_query_failure_does_not_write_rules():
    shell = RuleShell()
    shell.fail_show.add(True)
    gate = UidBlackholeGate(shell, uid=10046)
    assert gate.block() is False
    assert not any("add" in call for call in shell.calls)


@pytest.mark.parametrize("ipv6", [False, True])
@pytest.mark.parametrize("rule", [
    "99: from all lookup 1000",
    "100: from all lookup 1000",
    "100: from all uidrange 42-42 blackhole",
])
def test_uid_block_refuses_existing_rule_at_higher_or_equal_priority(ipv6, rule):
    shell = RuleShell()
    shell.rules[ipv6] = [rule]
    gate = UidBlackholeGate(shell, uid=10046)
    assert gate.block() is False
    assert shell.rules[ipv6] == [rule]
    assert shell.rules[not ipv6] == []
    assert not any("add" in call or "del" in call for call in shell.calls)


def test_uid_restore_preserves_lookup_and_other_uid_at_other_pref():
    shell = RuleShell()
    shell.rules[False] = ["101: from all lookup 1000", "101: from all uidrange 42-42 blackhole"]
    original = list(shell.rules[False])
    gate = UidBlackholeGate(shell, uid=10046)
    assert gate.block()
    assert gate.restore()
    assert shell.rules[False] == original
    assert not any(call[-3:] == ("del", "pref", "100") for call in shell.calls)


@pytest.mark.parametrize("ipv6", [False, True])
def test_uid_standard_local_rule_is_the_only_high_priority_exception(ipv6):
    shell = RuleShell()
    shell.rules[ipv6] = ["0: from all lookup 1000"]
    gate = UidBlackholeGate(shell, uid=10046)
    assert gate.block() is False
    assert shell.rules[ipv6] == ["0: from all lookup 1000"]
    assert not any("add" in call or "del" in call for call in shell.calls)


@pytest.mark.parametrize("ipv6", [False, True])
def test_uid_standard_local_and_lower_priority_rules_allow_creation(ipv6):
    shell = RuleShell()
    shell.rules[ipv6] = ["101: from all lookup 1000"]
    gate = UidBlackholeGate(shell, uid=10046)
    # 替身始终包含标准 0 local；低优先级 lookup 不阻挡创建，也不归本实例清理。
    assert gate.block()
    assert gate.restore()
    assert shell.rules[ipv6] == ["101: from all lookup 1000"]


@pytest.mark.parametrize("failure", ["-I", "-A"])
def test_iptables_partial_write_is_rolled_back(failure):
    shell, calls, _ = _fake_shell(uid=10123)

    def failing(args):
        if args[0] == "iptables" and args[1] == failure:
            return 1, "write failed"
        return shell(args)

    gate = GameNetworkGate(failing)
    assert gate.block() is False
    assert gate.restore() is True
    assert any(call[:2] == ("iptables", "-X") for call in calls)


def test_iptables_ipv6_failure_rolls_back_ipv4():
    shell, calls, _ = _fake_shell(uid=10123)

    def failing(args):
        if args[0] == "ip6tables":
            return 127, "not available"
        return shell(args)

    gate = GameNetworkGate(failing)
    assert gate.block() is False
    assert not gate._blocked


def test_iptables_delete_and_query_failure_do_not_confirm_restore():
    shell, _, _ = _fake_shell(uid=10123)
    failing = False

    def flaky(args):
        if failing and args[0] in {"iptables", "ip6tables"}:
            return 1, "Permission denied"
        return shell(args)

    gate = GameNetworkGate(flaky)
    assert gate.block()
    failing = True
    assert gate.restore() is False
    assert gate._blocked is True


@pytest.mark.parametrize("output", ["", "Permission denied", "RTNETLINK answers: Operation not permitted"])
def test_zero_exit_with_invalid_query_output_is_unknown(output):
    shell = RuleShell()

    def malformed(args):
        if tuple(args)[-2:] == ("rule", "show"):
            return 0, output
        return shell(args)

    gate = UidBlackholeGate(malformed, uid=10046)
    assert gate._rule_present(ipv6=False) is RulePresence.UNKNOWN
    assert gate.block() is False
    assert gate.restore() is False
    assert not any("add" in call or "del" in call for call in shell.calls)


@pytest.mark.parametrize("ipv6", [False, True])
def test_failed_delete_keeps_owned_family_pending(ipv6):
    shell = RuleShell()
    gate = UidBlackholeGate(shell, uid=10046)
    assert gate.block()
    shell.fail_delete.add(ipv6)
    assert gate.restore() is False
    assert ipv6 in gate._owned_families
    assert len(shell.rules[ipv6]) == 1
    shell.fail_delete.clear()
    assert gate.restore()
    assert not gate._owned_families


@pytest.mark.parametrize("ipv6", [False, True])
def test_duplicate_exact_rule_never_triggers_unowned_delete(ipv6):
    shell = RuleShell()
    gate = UidBlackholeGate(shell, uid=10046)
    assert gate.block()
    shell.rules[ipv6].append(shell.rules[ipv6][0])
    assert gate.restore() is False
    assert len(shell.rules[ipv6]) == 2
    assert ipv6 in gate._owned_families


def test_constrained_preexisting_rule_is_not_adopted_or_deleted():
    shell = RuleShell()
    shell.rules[False] = ["100: from 192.0.2.0/24 uidrange 10046-10046 blackhole"]
    gate = UidBlackholeGate(shell, uid=10046)
    assert gate.block() is False
    assert gate.restore() is False
    assert len(shell.rules[False]) == 1
    assert not any("add" in call or "del" in call for call in shell.calls)


def test_rule_parser_distinguishes_family_and_priority():
    shell = RuleShell()
    shell.rules[False] = [
        "100: from all lookup 1000",
        "101: from all uidrange 10046-10046 blackhole",
        "100: from all uidrange 10046-10046 blackhole",
    ]
    gate = UidBlackholeGate(shell, uid=10046)
    assert gate._rule_present(ipv6=False) is RulePresence.PRESENT
    assert gate._rule_present(ipv6=True) is RulePresence.ABSENT


def test_uid_partial_write_rollback_failure_remains_pending():
    shell = RuleShell()
    shell.fail_add.add(True)
    shell.fail_delete.add(False)
    gate = UidBlackholeGate(shell, uid=10046)
    assert gate.block() is False
    assert gate._blocked
    assert gate._owned_families == {False}
    assert "回滚未确认" in gate.last_error
    assert gate.block() is False
    shell.fail_delete.clear()
    assert gate.restore()


def test_exception_in_shell_is_not_successful_recovery():
    shell = RuleShell()
    broken = False

    def failing(args):
        if broken:
            raise OSError("controller unavailable")
        return shell(args)

    gate = UidBlackholeGate(failing, uid=10046)
    assert gate.block()
    broken = True
    assert gate.restore() is False
    assert gate._owned_families == {False, True}
    assert gate._blocked


def test_iptables_preexisting_chain_is_not_adopted_or_flushed():
    shell, calls, _ = _fake_shell(uid=10123)
    assert shell(("iptables", "-N", "OUTPUT_mbdr_game"))[0] == 0
    gate = GameNetworkGate(shell)
    assert gate.block() is False
    assert gate.restore() is False
    assert not any(call[1] in {"-F", "-D", "-X"} for call in calls if call[0] in {"iptables", "ip6tables"})


@pytest.mark.parametrize("table", ["iptables", "ip6tables"])
def test_iptables_partial_rollback_failure_keeps_owner(table):
    shell, _, _ = _fake_shell(uid=10123)

    def partial(args):
        if args[0] == "ip6tables" and args[1] == "-I":
            return 1, "write failed"
        if args[0] == table and args[1] == "-F":
            return 1, "flush failed"
        return shell(args)

    gate = GameNetworkGate(partial)
    assert gate.block() is False
    assert gate.restore() is False
    assert table in gate._owned_tables
    assert gate._blocked


@pytest.mark.parametrize("ipv6", [False, True])
def test_failed_add_with_side_effect_is_not_assumed_owned(ipv6):
    shell = RuleShell()

    def uncertain(args):
        code, output = shell(args)
        if "add" in args and ("-6" in args) == ipv6:
            return 1, "transport lost after write"
        return code, output

    gate = UidBlackholeGate(uncertain, uid=10046)
    assert gate.block() is False
    assert gate.restore() is False
    assert ipv6 in gate._uncertain_families
    assert len(shell.rules[ipv6]) == 1
    assert not any("del" in call and ("-6" in call) == ipv6 for call in shell.calls)


@pytest.mark.parametrize("gate_type", [GameNetworkGate, UidBlackholeGate])
def test_missing_returncode_does_not_become_success(gate_type):
    gate = gate_type(lambda args: (None, ""))
    assert gate.block() is False
    assert gate.last_error


def test_iptables_zero_exit_error_text_never_confirms_restore():
    shell, _, _ = _fake_shell(uid=10123)
    broken = False

    def invalid_query(args):
        if broken and args[0] in {"iptables", "ip6tables"} and args[1] == "-S":
            return 0, "Permission denied"
        return shell(args)

    gate = GameNetworkGate(invalid_query)
    assert gate.block()
    broken = True
    assert gate.restore() is False
    assert gate._owned_tables == {"iptables", "ip6tables"}


@pytest.mark.parametrize("args", [
    ("su", "-c", "ip -6 rule show"),
    ("printf", "%s", "a' b;$()"),
])
def test_network_shell_quotes_argv_once_and_rejects_previous_nonce(args):
    previous = []

    class Controller:
        def post_shell(self, command, timeout):
            outer = shlex.split(command)
            assert outer[:2] == ["sh", "-c"]
            script = outer[2]
            assert shlex.split(script.split("; mbd_shell_status=", 1)[0]) == list(args)
            marker = re.search(r"__MBD_SHELL_[0-9a-f]+__", script)[0]
            output = f"\n{marker}:0\n"
            result = previous[0] if previous else output
            previous.append(output)
            return SimpleNamespace(
                wait=lambda: None, succeeded=True, failed=False, get=lambda: result,
            )

    controller = Controller()
    assert run_network_shell(controller, args) == (0, "")
    assert run_network_shell(controller, args)[0] == -1
