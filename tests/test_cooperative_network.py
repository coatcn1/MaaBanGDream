from __future__ import annotations

import pytest

from agent.realtime.cooperative_network import (
    GameNetworkGate,
    GAME_PACKAGE,
    resolve_game_uid,
)


def _fake_shell(uid: int | None, deny_iptables: bool = False):
    calls: list[tuple[str, ...]] = []
    rules: set[str] = set()
    chains: set[str] = set()

    def shell(args):
        calls.append(tuple(args))
        if args[0] == "dumpsys":
            body = f"userId={uid} appId=10123" if uid is not None else ""
            return (0, body)
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
    no_root = GameNetworkGate(_fake_shell(uid=10123, deny_iptables=True)[0])
    assert no_root.block() is False
    assert no_root.restore() is True

    no_uid = GameNetworkGate(_fake_shell(uid=None)[0])
    assert no_uid.block() is False
    assert no_uid.restore() is True
