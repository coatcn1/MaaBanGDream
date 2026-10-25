"""自动跳车调用接缝：替身不接设备，失败和停止不得推进导航。"""
from types import SimpleNamespace

import pytest

from agent.realtime import cooperative_action as coop, versus_action as versus


@pytest.fixture(params=[coop, versus])
def module(request, monkeypatch, tmp_path):
    monkeypatch.setattr(request.param, "PROJECT_ROOT", tmp_path)
    return request.param


def bare(module):
    cls = module.CooperativeLiveFlow if module is coop else module.VersusLiveFlow
    flow = object.__new__(cls)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow.settings = dict(module.DEFAULT_SETTINGS, auto_disconnect_jump=True, auto_jump_continue=True)
    flow.action_argv = lambda params: params
    return flow


def test_failed_release_request_never_jumps(monkeypatch, module):
    flow = bare(module)
    monkeypatch.setattr(module, "RealtimeProfilePlay", lambda: SimpleNamespace(run_for_flow=lambda *a: False))
    monkeypatch.setattr(module, "current_live_run", lambda: SimpleNamespace(disconnect_jump_requested=True))
    flow.auto_disconnect_jump = lambda *a: pytest.fail("释放门禁失败不得跳车或续跑")
    assert flow.play() is False
    assert flow._input_integrity_failed is True


@pytest.mark.parametrize("key", ["disconnect_jump_enabled", "auto_disconnect_jump", "auto_jump_continue"])
@pytest.mark.parametrize("value", ["false", 1, None])
def test_jump_settings_reject_non_boolean(module, key, value):
    configure = module.configure_cooperative_settings if module is coop else module.configure_versus_settings
    with pytest.raises(ValueError, match="布尔"):
        configure({"reset": True, key: value})


def jump_flow(monkeypatch, module, *, restores=(True, True), dismiss=True, home=True):
    flow = bare(module)
    flow._game_uid = 10001
    flow.templates = {"disconnect_continue_body": True, "disconnect_confirm_body": True}
    flow._adb_shell = lambda *a: (0, "")
    flow._wait_and_click = lambda *a: True
    flow._wait_for_template = lambda *a: True
    flow.dismiss_connect_failed = lambda: dismiss
    flow.capture = lambda: "home"
    flow.visible = lambda *a: False
    flow.pipeline_box = lambda *a: [1, 1, 1, 1] if home else None
    calls = []
    class Gate:
        last_error = "restore unknown"
        def __init__(self, *a, **k):
            self.results = iter(restores)
            self._uid = 10001
            self._owned_families = {False, True}
        def cleanup_stale(self): return True
        def block(self): return True
        def restore(self): return next(self.results)
    monkeypatch.setattr(module, "UidBlackholeGate", Gate)
    def key(_):
        calls.append("home")
        return SimpleNamespace(wait=lambda: None)
    flow.context.tasker.controller = SimpleNamespace(post_click_key=key,
        post_start_app=lambda _: calls.append("start") or SimpleNamespace(wait=lambda: None))
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    return flow, calls


def test_stop_after_home_never_starts(monkeypatch, module):
    flow, calls = jump_flow(monkeypatch, module)
    def home(_):
        calls.append("home")
        flow.context.tasker.stopping = True
        return SimpleNamespace(wait=lambda: None)
    flow.context.tasker.controller.post_click_key = home
    with pytest.raises(InterruptedError): flow.disconnect_jump_out()
    assert calls == ["home"]


def test_retry_failure_never_completes(monkeypatch, module):
    flow, _ = jump_flow(monkeypatch, module, dismiss=False)
    assert flow.disconnect_jump_out() is False


def test_final_restore_unknown_never_completes(monkeypatch, module):
    flow, _ = jump_flow(monkeypatch, module, restores=(True, False))
    with pytest.raises(RuntimeError, match="恢复未确认"):
        flow.disconnect_jump_out()


def test_unknown_page_never_completes(monkeypatch, module):
    flow, _ = jump_flow(monkeypatch, module, home=False)
    assert flow.disconnect_jump_out() is False


@pytest.mark.parametrize("manual_enabled", [False, True])
def test_off_and_on_run_never_query_network(monkeypatch, module, manual_enabled):
    flow = bare(module)
    flow.settings.update(auto_disconnect_jump=False, disconnect_jump_enabled=manual_enabled)
    flow._adb_shell = lambda *a: pytest.fail("手动/关闭档不得查询网络")
    monkeypatch.setattr(module, "BoundNetworkShell", lambda *a: pytest.fail("手动/关闭档不得初始化网络通道"))
    monkeypatch.setattr(module, "prepare_stamina_round", lambda *a: False)
    monkeypatch.setattr(module, "mark_stamina_settled", lambda *a: None)
    def attempt(**kwargs): raise module.JumpOutUnavailable("manual end")
    flow.run_attempt = attempt
    monkeypatch.setattr(module, "record_failure_reason", lambda *a: None)
    assert flow.run() is False


def test_failed_release_preserves_cause_without_retry_or_navigation(monkeypatch, module):
    flow = bare(module)
    flow.settings.update(auto_disconnect_jump=False, play_failure_retry_count=99)
    monkeypatch.setattr(module, "RealtimeProfilePlay", lambda: SimpleNamespace(run_for_flow=lambda *a: False))
    monkeypatch.setattr(module, "current_live_run", lambda: SimpleNamespace(disconnect_jump_requested=True))
    monkeypatch.setattr(module, "prepare_stamina_round", lambda *a: False)
    monkeypatch.setattr(module, "mark_stamina_settled", lambda *a: None)
    monkeypatch.setattr(module, "mark_uncertain", lambda *a: None)
    monkeypatch.setattr(module, "record_failure_reason", lambda *a: pytest.fail("不得覆盖演奏层释放失败首因"))
    attempts = []
    flow.run_attempt = lambda **kwargs: attempts.append(True) or flow.play()
    flow.recover_after_play_failure = lambda *a: pytest.fail("释放失败不得恢复导航")
    assert flow.run() is False
    assert attempts == [True]


def test_real_profile_guard_failed_release_cannot_continue_round(monkeypatch, module):
    from agent.realtime import profile_play_action as profile

    flow = bare(module)
    flow.settings.update(auto_disconnect_jump=False, play_failure_retry_count=99)
    errors = []
    def failed_release(*args, **kwargs):
        raise RuntimeError("Native 演奏未通过完整性门禁：release_confirmed=false")
    monkeypatch.setattr(profile.RealtimeProfilePlay, "_run", failed_release)
    monkeypatch.setattr(profile, "record_failure_reason", errors.append)
    monkeypatch.setattr(module, "record_failure_reason", lambda *a: pytest.fail("不得覆盖首因"))
    monkeypatch.setattr(module, "current_live_run", lambda: SimpleNamespace(disconnect_jump_requested=True))
    monkeypatch.setattr(module, "prepare_stamina_round", lambda *a: False)
    monkeypatch.setattr(module, "mark_stamina_settled", lambda *a: None)
    monkeypatch.setattr(module, "mark_uncertain", lambda *a: None)
    flow.run_attempt = lambda **kwargs: flow.play()
    flow.auto_disconnect_jump = lambda *a: pytest.fail("失败门禁不得跳车")
    flow.recover_after_play_failure = lambda *a: pytest.fail("失败门禁不得重试导航")
    assert flow.run() is False
    assert errors == ["RuntimeError: Native 演奏未通过完整性门禁：release_confirmed=false"]


def test_stop_after_retry_click_never_captures(monkeypatch, module):
    flow = bare(module)
    captures = []
    flow.capture = lambda: captures.append(True) or "popup"
    flow.visible = lambda _, name, *a: name == "connect_failed_body"
    flow.click = lambda *a: setattr(flow.context.tasker, "stopping", True)
    monkeypatch.setattr(module.time, "sleep", lambda *a: None)
    with pytest.raises(InterruptedError): flow.dismiss_connect_failed()
    assert captures == [True]


def test_network_pending_terminates_run_without_retry_or_navigation(monkeypatch, module):
    flow = bare(module)
    flow.settings.update(auto_disconnect_jump=False, play_failure_retry_count=99)
    monkeypatch.setattr(module, "prepare_stamina_round", lambda *a: False)
    monkeypatch.setattr(module, "mark_stamina_settled", lambda *a: None)
    errors, attempts = [], []
    monkeypatch.setattr(module, "record_failure_reason", errors.append)
    def attempt(**kwargs):
        attempts.append(True)
        raise module.NetworkRecoveryPending("网络恢复未确认：lost receipt")
    flow.run_attempt = attempt
    flow.recover_after_play_failure = lambda *a: pytest.fail("网络未决不得恢复导航")
    assert flow.run() is False
    assert attempts == [True]
    assert errors == ["生命归零；网络恢复未确认：lost receipt"]


def test_device_revalidation_failure_never_falls_back_to_manual(monkeypatch, module):
    from tests.test_bound_network_shell import prepared

    flow, calls = jump_flow(monkeypatch, module)
    transport, state, _, controller = prepared(monkeypatch)
    controller.info["adb_serial"] = "127.0.0.1:16416"
    flow._network_shell = transport
    monkeypatch.setattr(module, "RealtimeProfilePlay", lambda: SimpleNamespace(run_for_flow=lambda *a: True))
    monkeypatch.setattr(module, "current_live_run", lambda: SimpleNamespace(disconnect_jump_requested=True))
    with pytest.raises(module.NetworkRecoveryPending, match="绑定"):
        flow.play()
    assert calls == []


@pytest.mark.parametrize("validation_number", [1, 2])
def test_stop_during_device_validation_never_sends_home(monkeypatch, module, validation_number):
    flow, calls = jump_flow(monkeypatch, module)
    checks = []
    def validate():
        checks.append(True)
        if len(checks) == validation_number:
            flow.context.tasker.stopping = True
    flow._network_shell = SimpleNamespace(validate_current=validate)
    with pytest.raises(InterruptedError): flow.disconnect_jump_out()
    assert calls == []


def test_stopped_restore_failure_retains_cached_evidence(monkeypatch, module, tmp_path, capsys):
    import json
    flow, calls = jump_flow(monkeypatch, module, restores=(False,))
    flow._network_shell = SimpleNamespace(validate_current=lambda: None,
        _binding=("cached-adb.exe", "cached-endpoint"), _identity=("cached-boot", "fingerprint", "android-id"))
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    def home(_):
        calls.append("home")
        flow.context.tasker.stopping = True
        return SimpleNamespace(wait=lambda: None)
    flow.context.tasker.controller.post_click_key = home
    flow.capture = lambda: pytest.fail("停止后不截图")
    monkeypatch.setattr(module, "record_failure_reason", lambda *a: pytest.fail("人工停止不记业务失败"))
    with pytest.raises(InterruptedError): flow.disconnect_jump_out()
    assert calls == ["home"]
    output = capsys.readouterr().out
    assert "network_restore_unconfirmed=true" in output
    evidence = list((tmp_path / "debug" / "network-recovery-pending").glob("*.json"))
    assert len(evidence) == 1
    record = json.loads(evidence[0].read_text(encoding="utf-8"))
    assert record["reason"]
    assert record["binding"] == ["cached-adb.exe", "cached-endpoint"]
    assert record["device_identity"][0] == "cached-boot"
    assert record["uid"] == 10001
    assert record["owned_families"] == [False, True]


@pytest.mark.parametrize("stage", ["uid", "root", "su"])
def test_preflight_stop_at_query_boundary_never_advances(monkeypatch, module, stage):
    flow = bare(module)
    calls = []
    class Transport:
        def __init__(self, *a, **k): pass
        def __call__(self, args):
            calls.append(tuple(args))
            if (stage == "uid" and args[0] == "dumpsys"
                or stage == "root" and args == ("id", "-u")
                or stage == "su" and args[0] == "su"):
                flow.context.tasker.stopping = True
            return (0, "userId=10001") if args[0] == "dumpsys" else (0, "2000")
        def validate_current(self): pytest.fail("停止后不继续核验")
    monkeypatch.setattr(module, "BoundNetworkShell", Transport)
    flow.run_attempt = lambda **k: pytest.fail("预检停止后不进入演出")
    assert flow.run() is True
    assert len(calls) == {"uid": 1, "root": 2, "su": 3}[stage]


def test_device_integrity_preflight_is_hard_failure(monkeypatch, module):
    flow = bare(module)
    errors = []
    def fail(*a, **k): raise module.DeviceIntegrityFailure("原始设备身份冲突")
    monkeypatch.setattr(module, "BoundNetworkShell", fail)
    monkeypatch.setattr(module, "record_failure_reason", errors.append)
    flow.run_attempt = lambda **k: pytest.fail("设备冲突后不得导航")
    assert flow.run() is False
    assert errors == ["原始设备身份冲突"]


def test_missing_root_remains_capability_fallback_after_identity_validation(monkeypatch, module):
    flow = bare(module)
    calls, errors = [], []
    class Transport:
        def __init__(self, *a, **k): pass
        def __call__(self, args):
            calls.append(tuple(args))
            if args[0] == "dumpsys": return 0, "userId=10001"
            if args[0] == "id": return 0, "2000"
            return 127, "su: not found"
        def validate_current(self): calls.append(("validate",))
    monkeypatch.setattr(module, "BoundNetworkShell", Transport)
    monkeypatch.setattr(module, "UidBlackholeGate", lambda *a, **k: pytest.fail("无 root 不初始化规则门禁"))
    monkeypatch.setattr(module, "prepare_stamina_round", lambda *a: False)
    monkeypatch.setattr(module, "mark_stamina_settled", lambda *a: None)
    monkeypatch.setattr(module, "record_failure_reason", errors.append)
    def manual_attempt(**kwargs):
        assert flow._game_uid is None
        assert isinstance(flow._network_shell, Transport)
        raise module.JumpOutUnavailable("original manual path")
    flow.run_attempt = manual_attempt
    assert flow.run() is False
    assert errors == ["original manual path"]
    assert calls[-1] == ("validate",)


def test_pending_diagnostic_failure_cannot_block_neutral_stop(monkeypatch, module):
    flow, calls = jump_flow(monkeypatch, module, restores=(False,))
    def home(_):
        flow.context.tasker.stopping = True
        calls.append("home")
        return SimpleNamespace(wait=lambda: None)
    def evidence(*a): raise OSError("disk unavailable")
    flow.context.tasker.controller.post_click_key = home
    monkeypatch.setattr(module, "record_pending_network_recovery", evidence)
    monkeypatch.setattr(module, "record_failure_reason", lambda *a: pytest.fail("停止不记业务失败"))
    with pytest.raises(InterruptedError): flow.disconnect_jump_out()
    assert calls == ["home"]


def test_device_failure_reaches_run_without_manual_fallback_or_retry(monkeypatch, module):
    flow, calls = jump_flow(monkeypatch, module)
    flow.settings.update(auto_disconnect_jump=False, play_failure_retry_count=99)
    errors, attempts = [], []
    monkeypatch.setattr(module, "prepare_stamina_round", lambda *a: False)
    monkeypatch.setattr(module, "mark_stamina_settled", lambda *a: None)
    monkeypatch.setattr(module, "record_failure_reason", errors.append)
    monkeypatch.setattr(module, "RealtimeProfilePlay", lambda: SimpleNamespace(run_for_flow=lambda *a: True))
    monkeypatch.setattr(module, "current_live_run", lambda: SimpleNamespace(disconnect_jump_requested=True))
    def changed_device(): raise RuntimeError("原始设备绑定失效")
    def attempt(**kwargs):
        attempts.append(True)
        flow.settings["auto_disconnect_jump"] = True
        flow._game_uid = 10001
        flow._network_shell = SimpleNamespace(validate_current=changed_device)
        return flow.play()
    flow.run_attempt = attempt
    flow.recover_after_play_failure = lambda *a: pytest.fail("设备失效后不得重试导航")
    assert flow.run() is False
    assert attempts == [True]
    assert calls == []
    assert errors == ["生命归零；设备完整性核验失败：原始设备绑定失效"]


@pytest.mark.parametrize("changed", ["binding", "boot"])
def test_rootless_auto_retains_baseline_before_manual_fallback(monkeypatch, module, changed):
    from agent.realtime import cooperative_network as network
    from tests.test_bound_network_shell import BOOT

    flow = bare(module)
    calls, errors = [], []
    state = {"boot": BOOT}
    controller = SimpleNamespace(info={"adb_path": "cached-adb", "adb_serial": "127.0.0.1:16448"},
        post_click_key=lambda key: calls.append("home") or SimpleNamespace(wait=lambda: None),
        post_start_app=lambda package: calls.append("start") or SimpleNamespace(wait=lambda: None))
    flow.context.tasker.controller = controller
    def shell(controller, args):
        if args == network._IDENTITY_COMMANDS[0]: return 0, state["boot"]
        if args in network._IDENTITY_COMMANDS[1:]: return 0, "identity"
        if args[0] == "dumpsys": return 0, "userId=10123"
        if args == ("id", "-u"): return 0, "2000"
        if args[0] == "su": return 127, "su: not found"
        pytest.fail("未知预检查询")
    def no_local(self): raise RuntimeError("不启用本机回退")
    monkeypatch.setattr(network, "run_network_shell", shell)
    monkeypatch.setattr(network.BoundNetworkShell, "_endpoint_owner", no_local)
    monkeypatch.setattr(module, "prepare_stamina_round", lambda *a: False)
    monkeypatch.setattr(module, "mark_stamina_settled", lambda *a: None)
    monkeypatch.setattr(module, "record_failure_reason", errors.append)
    monkeypatch.setattr(module, "current_live_run", lambda: SimpleNamespace(disconnect_jump_requested=True))
    def engine(*a):
        if changed == "binding": controller.info["adb_serial"] = "127.0.0.1:16416"
        else: state["boot"] = "87654321-1234-1234-1234-123456789abc"
        return True
    monkeypatch.setattr(module, "RealtimeProfilePlay", lambda: SimpleNamespace(run_for_flow=engine))
    flow.run_attempt = lambda **kwargs: flow.play()
    assert flow.run() is False
    assert calls == []
    assert len(errors) == 1
    assert ("绑定" if changed == "binding" else "身份") in errors[0]
