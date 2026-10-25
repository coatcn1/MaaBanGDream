"""竞演整合的失败边界；所有控制器、前台及截图均为替身。"""
from types import SimpleNamespace
import json

import pytest

from agent.realtime import cooperative_action as coop
from agent.realtime import versus_action as versus
from agent.realtime import profile_play_action as profile
from agent.realtime.final_cover import FinalCoverStartupFailure, FinalCoverIdentityConflict


MODULES = [coop, versus]


def bare_flow(module):
    cls = module.CooperativeLiveFlow if module is coop else module.VersusLiveFlow
    flow = object.__new__(cls)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow.settings = dict(module.DEFAULT_SETTINGS, play_failure_retry_count=1)
    flow.room_recovery_trial = False
    flow.progress_callback = None
    return flow


def test_versus_room_entry_recovers_stalled_match_popup(monkeypatch):
    """「匹配中」弹窗卡住时取消并重新确定，而不是等到超时失败。"""
    clock = [0.0]
    flow = bare_flow(versus)
    flow.capture = lambda: "frame"
    flow.context.tasker.controller = object()
    monkeypatch.setattr(versus, "require_game_foreground", lambda _c: None)
    monkeypatch.setattr(versus, "classify_room_type", lambda _image: flow.settings["room_type"])

    def visible(_image, name, *args):
        if name == "room_match_wait":
            return clock[0] < 13.0
        if name == "room_search":
            return 13.0 <= clock[0] < 13.5
        if name == "room_together":
            return clock[0] >= 13.5
        return False

    flow.visible = visible
    clicks = []
    flow.click = lambda point: clicks.append(point)
    flow.context.run_task = lambda node, override: (
        clicks.append(tuple(override[node]["target"][:2]))
        or SimpleNamespace(status=SimpleNamespace(succeeded=True))
    )
    monkeypatch.setattr(versus.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        versus.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    flow.verify_room_entry(
        "entry timeout", confirm_point=versus.VERSUS_CONFIRM_POINT
    )

    assert clicks == [
        versus.ROOM_MATCH_CANCEL_POINT,
        versus.VERSUS_CONFIRM_POINT,
    ]


@pytest.mark.parametrize("error", [FinalCoverStartupFailure, FinalCoverIdentityConflict])
def test_public_callback_contains_startup_failure(monkeypatch, error):
    action = profile.RealtimeProfilePlay()
    failures = []
    monkeypatch.setattr(profile, "record_failure_reason", failures.append)
    def fail(*args, **kwargs):
        raise error("original startup evidence")
    monkeypatch.setattr(action, "_run", fail)
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    assert action.run(context, SimpleNamespace()) is False
    assert failures == [f"{error.__name__}: original startup evidence"]
    with pytest.raises(error, match="original startup evidence"):
        action.run_for_flow(context, SimpleNamespace())


@pytest.mark.parametrize("module", MODULES)
@pytest.mark.parametrize("budget", [0, 1, 99])
def test_identity_conflict_never_retries_or_recovers(monkeypatch, module, budget):
    flow = bare_flow(module)
    flow.settings["play_failure_retry_count"] = budget
    attempts, recoveries, failures, jumps = [], [], [], []
    def fail(**kwargs):
        attempts.append(kwargs)
        raise FinalCoverIdentityConflict("wrong song evidence")
    flow.run_attempt = fail
    flow.recover_after_play_failure = recoveries.append
    flow.jump_after_startup_failure = jumps.append
    monkeypatch.setattr(module, "record_failure_reason", failures.append)
    monkeypatch.setattr(module, "append_current_run_event", lambda *a, **k: None)
    assert flow.run() is False
    assert len(attempts) == 1
    assert recoveries == []
    assert jumps == ["FinalCoverIdentityConflict: wrong song evidence"]
    assert failures == ["FinalCoverIdentityConflict: wrong song evidence"]


@pytest.mark.parametrize("module", MODULES)
def test_identity_conflict_cannot_turn_into_member_exit(monkeypatch, module):
    flow = bare_flow(module)
    def fail():
        raise FinalCoverIdentityConflict("wrong song evidence")
    flow.enter_room = fail
    flow.capture = lambda: pytest.fail("身份硬冲突后不再截图或重连")
    flow.visible = lambda *a, **k: True
    with pytest.raises(FinalCoverIdentityConflict):
        flow.run_attempt()


@pytest.mark.parametrize("module", MODULES)
@pytest.mark.parametrize("stop_after_home", [False, True])
def test_startup_return_respects_stop_between_system_inputs(monkeypatch, module, stop_after_home):
    flow = bare_flow(module)
    calls, failures = [], []
    class Job:
        def wait(self):
            return self
    class Controller:
        def post_click_key(self, key):
            calls.append(("key", key))
            flow.context.tasker.stopping = True
            return Job()
        def post_start_app(self, package):
            calls.append(("start", package))
            return Job()
    flow.context.tasker.controller = Controller()
    flow.context.tasker.stopping = not stop_after_home
    monkeypatch.setattr(module, "require_game_foreground", lambda *a: None)
    monkeypatch.setattr(module, "foreground_package", lambda *a: module.GAME_PACKAGE)
    monkeypatch.setattr(module.time, "sleep", lambda *a: None)
    monkeypatch.setattr(module, "record_failure_reason", failures.append)
    with pytest.raises(InterruptedError):
        flow.jump_after_startup_failure("original timeout evidence")
    assert calls == ([("key", 3)] if stop_after_home else [])
    assert failures == []


@pytest.mark.parametrize("module", MODULES)
def test_stopped_play_never_consumes_life_failure_signal(monkeypatch, module):
    flow = bare_flow(module)
    flow.action_argv = lambda params: params
    class Play:
        def run(self, *args):
            flow.context.tasker.stopping = True
            return False
        run_for_flow = run
    monkeypatch.setattr(module, "RealtimeProfilePlay", Play)
    monkeypatch.setattr(module, "current_live_run", lambda: SimpleNamespace(disconnect_jump_requested=True))
    with pytest.raises(InterruptedError):
        flow.play()


@pytest.mark.parametrize("module", MODULES)
@pytest.mark.parametrize("stop_after", [None, "home", "start"])
def test_life_failure_uses_same_cooperative_jump_and_stop_order(monkeypatch, module, stop_after):
    flow = bare_flow(module)
    flow.action_argv = lambda params: params
    calls = []
    class Job:
        def wait(self):
            return self
    class Controller:
        def post_click_key(self, key):
            calls.append(("key", key))
            if stop_after == "home":
                flow.context.tasker.stopping = True
            return Job()
        def post_start_app(self, package):
            calls.append(("start", package))
            if stop_after == "start":
                flow.context.tasker.stopping = True
            return Job()
    class Play:
        def run_for_flow(self, *args):
            return True
    flow.context.tasker.controller = Controller()
    monkeypatch.setattr(module, "RealtimeProfilePlay", Play)
    monkeypatch.setattr(module, "current_live_run", lambda: SimpleNamespace(disconnect_jump_requested=True))
    monkeypatch.setattr(module.time, "sleep", lambda *a: None)
    error = InterruptedError if stop_after else module.JumpOutUnavailable
    with pytest.raises(error) as caught:
        flow.play()
    if stop_after is None:
        assert str(caught.value) == "生命归零，请手动跳车后重试"
    expected = [("key", 3)]
    if stop_after != "home":
        expected.append(("start", module.GAME_PACKAGE))
    assert calls == expected


@pytest.mark.parametrize("module", MODULES)
def test_disconnect_jump_templates_are_present(module):
    """自动跳车模板必须同时有 PNG 和位置，否则 disconnect_jump_out 直接失败。"""
    image_dir = module.PROJECT_ROOT / "resource" / "image" / (
        "cooperative" if module is coop else "versus"
    )
    for name in ("disconnect_continue_body", "disconnect_confirm_body"):
        assert (image_dir / f"{name}.png").is_file(), f"缺少模板：{name}"
        assert name in module.TEMPLATE_POSITIONS, f"缺少模板位置：{name}"


@pytest.mark.parametrize("module", MODULES)
@pytest.mark.parametrize(
    "auto_result, expected_error, expected_message",
    [
        ("continue", "RoundContinued", "继续下一局"),
        ("stop", "JumpOutUnavailable", "任务结束"),
    ],
)
def test_life_depletion_uses_auto_disconnect_jump_when_enabled(
    monkeypatch, module, auto_result, expected_error, expected_message
):
    """生命归零必须先按跳车选项尝试自动断网跳车，而不是直接结束任务。"""
    flow = bare_flow(module)
    flow.action_argv = lambda params: params
    flow.settings.update(
        auto_disconnect_jump=True,
        auto_jump_continue=auto_result == "continue",
    )
    calls = []
    flow.auto_disconnect_jump = lambda reason: calls.append(reason) or auto_result

    class Play:
        def run_for_flow(self, *args):
            return True

    monkeypatch.setattr(module, "RealtimeProfilePlay", Play)
    monkeypatch.setattr(
        module,
        "current_live_run",
        lambda: SimpleNamespace(disconnect_jump_requested=True),
    )

    with pytest.raises(getattr(module, expected_error)) as caught:
        flow.play()
    assert calls == ["生命归零"]
    assert expected_message in str(caught.value)


@pytest.mark.parametrize("module", MODULES)
def test_life_depletion_falls_back_to_manual_when_auto_jump_fails(
    monkeypatch, module
):
    """自动跳车失败返回 None 时仍执行原有的 HOME → 切回 → 手动提示。"""
    flow = bare_flow(module)
    flow.action_argv = lambda params: params
    flow.settings["auto_disconnect_jump"] = True
    flow.auto_disconnect_jump = lambda reason: None
    calls = []

    class Job:
        def wait(self):
            return self

    class Controller:
        def post_click_key(self, key):
            calls.append(("key", key))
            return Job()

        def post_start_app(self, package):
            calls.append(("start", package))
            return Job()

    class Play:
        def run_for_flow(self, *args):
            return True

    flow.context.tasker.controller = Controller()
    monkeypatch.setattr(module, "RealtimeProfilePlay", Play)
    monkeypatch.setattr(module.time, "sleep", lambda *a: None)
    monkeypatch.setattr(
        module,
        "current_live_run",
        lambda: SimpleNamespace(disconnect_jump_requested=True),
    )

    with pytest.raises(module.JumpOutUnavailable) as caught:
        flow.play()
    assert str(caught.value) == "生命归零，请手动跳车后重试"
    assert calls == [("key", 3), ("start", module.GAME_PACKAGE)]


@pytest.mark.parametrize("module", MODULES)
def test_auto_jump_continue_returns_to_room_and_continues(monkeypatch, module):
    """自动跳车并继续：失败局不计数，回到房间选择页后重试下一局。"""
    flow = bare_flow(module)
    flow.settings.update(count=1)
    attempts = []

    def run_attempt(reuse_room=False):
        attempts.append(reuse_room)
        if len(attempts) == 1:
            raise module.RoundContinued(
                "生命归零；自动断网跳车完成，继续下一局"
            )
        return True

    flow.run_attempt = run_attempt
    nav = []
    nav_name = (
        "navigate_to_cooperative_room_selection"
        if module is coop
        else "navigate_to_versus_room_selection"
    )
    setattr(flow, nav_name, lambda origin: nav.append(origin))
    # 成功局之后的结算导航由各自既有测试覆盖，这里只验证跳车后的续跑。
    flow.return_to_room_selection = lambda: None
    monkeypatch.setattr(module, "record_failure_reason", lambda *a, **k: None)

    assert flow.run() is True
    assert attempts == [False, False]
    assert nav == ["auto-jump"]


@pytest.mark.parametrize("module", MODULES)
def test_life_jump_ends_entire_task_without_retry_or_round_credit(monkeypatch, module):
    flow = bare_flow(module)
    flow.settings.update(count=3, play_failure_retry_count=99)
    attempts, failures, progress = [], [], []
    def jump(**kwargs):
        attempts.append(kwargs)
        raise module.JumpOutUnavailable("生命归零，请手动跳车后重试")
    flow.run_attempt = jump
    flow.progress_callback = lambda *args: progress.append(args)
    flow.recover_after_play_failure = lambda *args: pytest.fail("归零跳车后不能重演")
    monkeypatch.setattr(module, "record_failure_reason", failures.append)
    assert flow.run() is False
    assert len(attempts) == 1
    assert failures == ["生命归零，请手动跳车后重试"]
    assert progress == []


def test_multiplayer_jump_options_share_manual_wording_and_compatible_keys():
    interface = json.loads((versus.PROJECT_ROOT / "interface.json").read_text(encoding="utf-8"))
    expected = json.loads(json.dumps(interface["option"]["CooperativeDisconnectJump"]).replace(
        "CooperativeDisconnectJumpConfigure", "VersusDisconnectJumpConfigure"
    ))
    assert interface["option"]["VersusDisconnectJump"] == expected
    assert expected["label"] == "跳车"
    assert expected["default_case"] == "Off"
    assert expected["cases"][1]["label"] == "生命归零后手动跳车"
    assert "请手动跳车" in expected["description"]
    manual = expected["cases"][1]["pipeline_override"][
        "VersusDisconnectJumpConfigure"
    ]["custom_action_param"]
    assert manual == {
        "disconnect_jump_enabled": True,
        "auto_disconnect_jump": False,
        "auto_jump_continue": False,
    }
    auto_end = expected["cases"][2]["pipeline_override"][
        "VersusDisconnectJumpConfigure"
    ]["custom_action_param"]
    auto_continue = expected["cases"][3]["pipeline_override"][
        "VersusDisconnectJumpConfigure"
    ]["custom_action_param"]
    assert auto_end == {
        "disconnect_jump_enabled": True,
        "auto_disconnect_jump": True,
        "auto_jump_continue": False,
    }
    assert auto_continue == {
        "disconnect_jump_enabled": True,
        "auto_disconnect_jump": True,
        "auto_jump_continue": True,
    }
    task_options = {
        name for task in interface["task"] for name in task.get("option", [])
        if name.endswith("DisconnectJump")
    }
    assert task_options == {"CooperativeDisconnectJump", "VersusDisconnectJump"}



def test_versus_unconfirmed_mode_blocks_ready(monkeypatch):
    flow = bare_flow(versus)
    flow.capture = lambda: object()
    flow.click = lambda *args: None
    flow._save_performance_mode_evidence = lambda *a: None
    monkeypatch.setattr(versus, "live_performance_mode_is_off", lambda image: False)
    monkeypatch.setattr(versus.time, "sleep", lambda *a: None)
    with pytest.raises(RuntimeError, match="OFF"):
        flow.ensure_performance_mode_off()


@pytest.mark.parametrize("obsolete_flag", [None, "0", "1"])
def test_versus_normal_configuration_and_start_do_not_require_trial_env(monkeypatch, obsolete_flag):
    # 竞演按普通协力任务配置和启动，旧开关无论缺省还是遗留为 0 都不能阻断。
    if obsolete_flag is None:
        monkeypatch.delenv("MAABANGDREAM_VERSUS_LIVE_TRIAL", raising=False)
    else:
        monkeypatch.setenv("MAABANGDREAM_VERSUS_LIVE_TRIAL", obsolete_flag)
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    argv = SimpleNamespace(custom_action_param='{"reset":true}', task_detail=None)
    assert versus.VersusLiveConfigure().run(context, argv) is True
    started = []
    class Store:
        def __init__(self, *args):
            pass
        def runtime_options(self):
            return {"cooperative_member_loading_guard_enabled": True, "play_failure_retry_count": 1}
    class Progress:
        def run(self, *args):
            return True
    class Flow:
        def __init__(self, context, settings, **kwargs):
            self.settings = settings
        def run(self):
            started.append(self.settings)
            return True
    monkeypatch.setattr(versus, "RealtimeProfileStore", Store)
    monkeypatch.setattr(versus, "TaskProgress", Progress)
    monkeypatch.setattr(versus, "VersusLiveFlow", Flow)
    monkeypatch.setattr(versus, "versus_profile_preflight", lambda *args: None)
    assert versus.VersusLiveAction().run(context, argv) is True
    assert len(started) == 1
    assert started[0]["room_type"] == "free"
    nodes = json.loads((versus.PROJECT_ROOT / "resource/pipeline/versus_live.json").read_text(encoding="utf-8"))
    assert nodes["VersusLive"]["next"] == ["VersusProcessConflictGuard"]
    assert nodes["VersusProcessConflictGuard"]["custom_action"] == "ProcessConflictGuard"
    assert nodes["VersusProcessConflictGuard"]["next"] == ["VersusLiveStaminaRefillConfigure"]
    configure = nodes["VersusLiveStaminaRefillConfigure"]
    assert configure["custom_action"] == "StaminaRefillConfigure"
    assert configure["custom_action_param"]["stamina_refill_enabled"] is False
    assert configure["next"] == ["VersusRecover"]
    assert "VersusTrialGate" not in nodes
    assert not hasattr(versus, "VersusLiveTrialGate")


@pytest.mark.parametrize("module", MODULES)
def test_all_startup_jump_exits_preserve_cause_and_request_manual_jump(monkeypatch, module):
    flow = bare_flow(module)
    calls, failures = [], []
    class Job:
        def wait(self):
            return self
    class Controller:
        def post_click_key(self, key):
            calls.append(("key", key))
            return Job()
        def post_start_app(self, package):
            calls.append(("start", package))
            return Job()
    flow.context.tasker.controller = Controller()
    monkeypatch.setattr(module, "require_game_foreground", lambda *a: None)
    monkeypatch.setattr(module, "foreground_package", lambda *a: module.GAME_PACKAGE)
    monkeypatch.setattr(module.time, "sleep", lambda *a: None)
    monkeypatch.setattr(module, "record_failure_reason", failures.append)
    with pytest.raises(module.JumpOutUnavailable, match="original timeout；请手动跳车"):
        flow.jump_after_startup_failure("original timeout")
    assert failures == ["original timeout；请手动跳车"]
    assert calls == [("key", 3), ("start", module.GAME_PACKAGE)]


@pytest.mark.parametrize("module", MODULES)
@pytest.mark.parametrize("switch_error", [False, True])
def test_identity_failure_manual_jump_preserves_cause_and_never_retries(monkeypatch, module, switch_error):
    flow = bare_flow(module)
    flow.settings["play_failure_retry_count"] = 10
    attempts, jumps, failures = [], [], []
    def fail(**kwargs):
        attempts.append(kwargs)
        raise FinalCoverIdentityConflict("original final cover evidence")
    def jump(reason):
        jumps.append(reason)
        if switch_error:
            raise RuntimeError("foreground unavailable")
        raise module.JumpOutUnavailable(reason + "；请手动跳车")
    flow.run_attempt = fail
    flow.jump_after_startup_failure = jump
    flow.recover_after_play_failure = lambda _: pytest.fail("身份冲突不得恢复或重试")
    monkeypatch.setattr(module, "record_failure_reason", failures.append)
    assert flow.run() is False
    assert len(attempts) == len(jumps) == 1
    assert "original final cover evidence" in failures[-1]
    assert "请手动跳车" in failures[-1]
    if switch_error:
        assert "foreground unavailable" in failures[-1]


@pytest.mark.parametrize("module", MODULES)
def test_stop_during_identity_failure_does_not_jump_or_record_failure(monkeypatch, module):
    flow = bare_flow(module)
    def fail(**kwargs):
        flow.context.tasker.stopping = True
        raise FinalCoverIdentityConflict("late identity failure")
    flow.run_attempt = fail
    flow.jump_after_startup_failure = lambda _: pytest.fail("停止后不得切换应用")
    monkeypatch.setattr(module, "record_failure_reason", lambda _: pytest.fail("停止不记业务失败"))
    assert flow.run() is True


@pytest.mark.parametrize("module", MODULES)
def test_stop_inside_identity_jump_preserves_neutral_stop(monkeypatch, module):
    flow = bare_flow(module)
    def fail(**kwargs):
        raise FinalCoverIdentityConflict("identity evidence before stop")
    def jump(reason):
        flow.context.tasker.stopping = True
        raise InterruptedError("用户已停止任务")
    flow.run_attempt = fail
    flow.jump_after_startup_failure = jump
    monkeypatch.setattr(module, "record_failure_reason", lambda _: pytest.fail("停止不记业务失败"))
    with pytest.raises(InterruptedError):
        flow.run()
