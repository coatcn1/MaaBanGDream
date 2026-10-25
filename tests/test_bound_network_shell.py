"""设备绑定和本机回退的离线回归；全部通道均为替身。"""
import re
import shlex
from types import SimpleNamespace

import pytest

from agent.realtime import cooperative_network as network


BOOT = "12345678-1234-1234-1234-123456789abc"


def prepared(monkeypatch):
    controller = SimpleNamespace(info={"adb_path": "test-adb.exe", "adb_serial": "127.0.0.1:16448"})
    state = {"maa_failed": False, "local_boot": BOOT, "owner": (123, 1.0, "mumu.exe")}
    calls = []
    def result(args, local):
        args = tuple(args)
        if args == network._IDENTITY_COMMANDS[0]: return 0, state["local_boot"] if local else BOOT
        if args in network._IDENTITY_COMMANDS[1:]: return 0, "identity"
        return 0, ""
    def maa(controller, args):
        calls.append(("maa", tuple(args)))
        return (-1, "missing receipt") if state["maa_failed"] else result(args, False)
    def local(self, args):
        calls.append(("local", tuple(args)))
        return result(args, True)
    monkeypatch.setattr(network, "run_network_shell", maa)
    monkeypatch.setattr(network.BoundNetworkShell, "_local_shell", local)
    monkeypatch.setattr(network.BoundNetworkShell, "_endpoint_owner", lambda self: state["owner"])
    transport = network.BoundNetworkShell(lambda: controller)
    calls.clear()
    return transport, state, calls, controller


@pytest.mark.parametrize("command", [("ip", "rule", "add", "uidrange", "1-1", "blackhole"),
    ("su", "-c", "iptables -N OWNED")])
def test_unknown_write_never_replays_in_local_transport(monkeypatch, command):
    transport, state, calls, _ = prepared(monkeypatch)
    state["maa_failed"] = True
    assert transport(command)[0] == -1
    assert calls == [("maa", command)]
    assert transport._local_selected is False


@pytest.mark.parametrize("command", [("ip", "rule", "show"), ("su", "-c", "ip -6 rule show")])
def test_only_read_failure_selects_verified_local_transport(monkeypatch, command):
    transport, state, calls, _ = prepared(monkeypatch)
    state["maa_failed"] = True
    assert transport(command) == (0, "")
    assert calls[-1] == ("local", command)
    assert transport._local_selected is True
    calls.clear()
    write = ("ip", "rule", "del", "uidrange", "1-1", "blackhole")
    assert transport(write) == (0, "")
    assert ("maa", write) not in calls
    assert calls.count(("local", write)) == 1


@pytest.mark.parametrize("changed", ["endpoint", "owner", "boot"])
def test_changed_binding_denies_local_commands(monkeypatch, changed):
    transport, state, calls, controller = prepared(monkeypatch)
    state["maa_failed"] = True
    if changed == "endpoint": controller.info["adb_serial"] = "127.0.0.1:16416"
    if changed == "owner": state["owner"] = (234, 2.0, "mumu.exe")
    if changed == "boot": state["local_boot"] = "87654321-1234-1234-1234-123456789abc"
    query = ("ip", "rule", "show")
    with pytest.raises(network.DeviceIntegrityFailure):
        transport(query)
    assert ("local", query) not in calls


def test_unverified_local_identity_never_becomes_fallback(monkeypatch):
    transport, state, calls, _ = prepared(monkeypatch)
    transport._owner = None
    state["maa_failed"] = True
    query = ("ip", "rule", "show")
    assert transport(query)[0] == -1
    assert calls == [("maa", query)]


def test_validation_can_select_prevalidated_local_channel_after_engine_stop(monkeypatch):
    transport, state, calls, _ = prepared(monkeypatch)
    state["maa_failed"] = True
    transport.validate_current()
    assert transport._local_selected is True
    assert all(call[1] in network._IDENTITY_COMMANDS for call in calls)


def test_identity_preflight_checks_stop_after_each_query(monkeypatch):
    controller = SimpleNamespace(info={"adb_path": "current-adb", "adb_serial": "127.0.0.1:16448"})
    state = {"stopped": False}
    calls = []
    def query(*args):
        calls.append(args[1])
        state["stopped"] = True
        return 0, BOOT
    monkeypatch.setattr(network, "run_network_shell", query)
    with pytest.raises(InterruptedError):
        network.BoundNetworkShell(lambda: controller, stopped=lambda: state["stopped"])
    assert calls == [network._IDENTITY_COMMANDS[0]]


def test_pending_snapshot_keeps_owned_rules_when_disk_and_stdout_fail(monkeypatch, tmp_path):
    blocked_root = tmp_path / "unwritable-root"
    blocked_root.write_text("file instead of directory", encoding="utf-8")
    gate = SimpleNamespace(_uid=10123, _pref=100, _owned_families={False}, _uncertain_families={True},
                           last_error="cleanup query unknown")
    transport = SimpleNamespace(_binding=("cached-adb", "cached-endpoint"), _identity=(BOOT, "fingerprint", "id"))
    def broken_print(*a, **k): raise OSError("stdout closed")
    monkeypatch.setattr("builtins.print", broken_print)
    network.record_pending_network_recovery(gate, transport, blocked_root, "用户停止时恢复未确认")
    record = network._pending_recovery_evidence[-1]
    assert record["owned_families"] == [False]
    assert record["uncertain_families"] == [True]
    assert record["binding"] == ("cached-adb", "cached-endpoint")


@pytest.mark.parametrize("channel", ["maa", "local"])
@pytest.mark.parametrize("root", [False, True])
@pytest.mark.parametrize("create", [
    ("ip", "rule", "add", "uidrange", "10123-10123", "blackhole", "pref", "100"),
    ("ip", "-6", "rule", "add", "uidrange", "10123-10123", "blackhole", "pref", "100"),
    *((table, "-N", "OUTPUT_mbdr_game") for table in ("iptables", "ip6tables")),
    *((table, "-A", "OUTPUT_mbdr_game", "-m", "owner", "--uid-owner", "10123", "-j", "REJECT")
      for table in ("iptables", "ip6tables")),
    *((table, "-I", "OUTPUT", "1", "-j", "OUTPUT_mbdr_game") for table in ("iptables", "ip6tables")),
])
def test_stop_in_last_transport_validation_prevents_create_but_allows_restore(monkeypatch, channel, root, create):
    state = {"stopped": False, "getter_calls": 0}
    dispatched = []
    identity = {network._IDENTITY_COMMANDS[0]: BOOT,
                network._IDENTITY_COMMANDS[1]: "fingerprint",
                network._IDENTITY_COMMANDS[2]: "android-id"}
    def receipt(command):
        outer = shlex.split(command)
        args = tuple(shlex.split(outer[2].split("; mbd_shell_status", 1)[0]))
        dispatched.append(args)
        if args == network._IDENTITY_COMMANDS[2]:
            state["stopped"] = True
        marker = re.search(r"__MBD_SHELL_[0-9a-f]+__", command)[0]
        return f"{identity.get(args, '')}\n{marker}:0\n"
    class Controller:
        info = {"adb_path": "cached-adb.exe", "adb_serial": "127.0.0.1:16448"}
        def post_shell(self, command, timeout):
            output = receipt(command)
            return SimpleNamespace(succeeded=True, failed=False, wait=lambda: None, get=lambda: output)
    controller = Controller()
    def getter():
        state["getter_calls"] += 1
        # Maa 最终取代理发生在 Gate 的 creating 检查之后。
        if channel == "maa" and state["getter_calls"] == 2:
            state["stopped"] = True
        return controller
    transport = object.__new__(network.BoundNetworkShell)
    transport._controller_getter = getter
    transport._binding = ("cached-adb.exe", "127.0.0.1:16448")
    transport._identity = tuple(identity.values())
    transport._owner = (123, 1.0, "mumu.exe")
    transport._local_selected = channel == "local"
    transport._stopped = lambda: state["stopped"]
    monkeypatch.setattr(network.BoundNetworkShell, "_endpoint_owner", lambda self: transport._owner)
    monkeypatch.setattr(network.subprocess, "run", lambda argv, **kwargs:
                        SimpleNamespace(returncode=0, stdout=receipt(argv[4])))
    gate_type = network.UidBlackholeGate if create[0] == "ip" else network.GameNetworkGate
    gate = gate_type(transport, uid=10123, stopped=lambda: state["stopped"])
    gate._root_shell_available = root
    with pytest.raises(InterruptedError):
        gate._root_shell_ok(create, creating=True)
    expected_create = create if root else ("su", "-c", shlex.join(create))
    assert expected_create not in dispatched
    if create[0] == "ip":
        family = ("ip", "-6") if create[1] == "-6" else ("ip",)
        cleanup = [(*family, "rule", "del", "uidrange", "10123-10123", "blackhole", "pref", "100")]
    else:
        cleanup = [(create[0], "-F", "OUTPUT_mbdr_game"), (create[0], "-D", "OUTPUT", "-j", "OUTPUT_mbdr_game"),
                   (create[0], "-X", "OUTPUT_mbdr_game")]
    for command in cleanup:
        assert gate._root_shell_ok(command) is True
        expected = command if root else ("su", "-c", shlex.join(command))
        assert dispatched.count(expected) == 1


@pytest.mark.parametrize("remote_status", [0, 7])
def test_local_adb_uses_shared_receipt_and_single_su_command(monkeypatch, remote_status):
    transport = object.__new__(network.BoundNetworkShell)
    transport._binding = ("current-adb.exe", "127.0.0.1:16448")
    def run(argv, **kwargs):
        assert argv[:4] == ["current-adb.exe", "-s", "127.0.0.1:16448", "shell"]
        outer = shlex.split(argv[4])
        assert outer[:2] == ["sh", "-c"]
        assert shlex.split(outer[2].split("; mbd_shell_status", 1)[0]) == ["su", "-c", "printf 'quoted value'"]
        marker = re.search(r"__MBD_SHELL_[0-9a-f]+__", argv[4])[0]
        return SimpleNamespace(returncode=0, stdout=f"body\n{marker}:{remote_status}\n")
    monkeypatch.setattr(network.subprocess, "run", run)
    assert transport._local_shell(("su", "-c", "printf 'quoted value'")) == (remote_status, "body")
