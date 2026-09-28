from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent.realtime.fes_action as fes_action
from agent.realtime.fes_action import (
    DEFAULT_SETTINGS,
    FES_DIFFICULTY_TARGETS,
    FES_GEAR_POINT,
    FES_MATCH_OK_POINT,
    FES_READY_POINT,
    FesLiveFlow,
    configure_fes_settings,
    current_fes_settings,
    fes_play_params,
)
from agent.realtime.profile_play_action import _recording_kind


ROOT = Path(__file__).parents[1]


def _bare_flow():
    flow = object.__new__(FesLiveFlow)
    # run_recognition 默认未命中：FesHomeLive 模板判定由各测试自行 stub。
    flow.context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False),
        run_recognition=lambda _node, _image: None,
    )
    return flow


@pytest.mark.parametrize("count", [0, 1, 100, 999])
def test_fes_accepts_infinite_and_count_limit(count):
    assert configure_fes_settings({"reset": True, "count": count})["count"] == count


@pytest.mark.parametrize("count", [-1, 1000, "abc"])
def test_fes_rejects_invalid_count(count):
    with pytest.raises(ValueError):
        configure_fes_settings({"reset": True, "count": count})


def test_fes_configure_merges_without_reset():
    configure_fes_settings({"reset": True, "count": 2, "difficulty": "Hard"})
    settings = configure_fes_settings({"difficulty": "Expert"})
    assert settings["count"] == 2
    assert settings["difficulty"] == "Expert"
    # 未提供的键保留默认值
    assert settings["diagnostic_trace"] == DEFAULT_SETTINGS["diagnostic_trace"]


def test_fes_rejects_special_difficulty():
    # Fes 活动只有四档难度，Special 必须在配置阶段被拒绝。
    with pytest.raises(ValueError, match="Special"):
        configure_fes_settings({"reset": True, "difficulty": "Special"})


def test_fes_play_params_declare_fes_run_mode():
    params = fes_play_params({"difficulty": "Expert", "debug_recording": False, "diagnostic_trace": True})
    assert params["run_mode"] == "fes"
    assert params["difficulty"] == "Expert"
    assert params["require_profile"] is True
    assert params["require_completion"] is True
    assert params["wait_for_completion"] is True
    # 生命归零跳车请求已启用（引擎归零帧停手置位，切桌面由外层执行）；
    # 其余协力专属机制（结算拖沓等）仍不启用。
    assert params["life_depleted_jump_request"] is True
    # 并发最终封面：photogate 等待期观察、锚点帧裁决，谱面预加载不再
    # 被封面阻塞等待卡住。
    assert params["final_cover_concurrent"] is True
    assert "defer_result_collection" not in params


def test_fes_recording_kind_registered():
    assert _recording_kind("fes") == "fes"


def test_fes_difficulty_targets_cover_all_difficulties():
    # 四档难度；Fes 没有 Special（UI 第五槽永不点击）。
    assert set(FES_DIFFICULTY_TARGETS) == {"Easy", "Normal", "Hard", "Expert"}


def test_fes_calibrated_points_match_real_device_recording():
    # 雷电模拟器 1280x720 录像实测：难度行霍夫圆 + 红按钮 HSV 中心。
    assert FES_DIFFICULTY_TARGETS == {
        "Easy": (602, 572),
        "Normal": (684, 574),
        "Hard": (770, 572),
        "Expert": (856, 574),
    }
    assert FES_READY_POINT == (1123, 630)
    assert FES_MATCH_OK_POINT == (1048, 646)
    assert FES_GEAR_POINT == (946, 650)


def test_fes_unlimited_continues_until_stop():
    flow = _bare_flow()
    flow.settings = {"count": 0}
    completed = []
    flow.run_attempt = lambda: True

    def progress(current, total):
        assert total == 0
        completed.append(current)
        if current == 5:
            flow.context.tasker.stopping = True

    flow.progress_callback = progress
    flow.recover_after_play_failure = lambda _reason: None
    assert flow.run() is True
    assert completed == [1, 2, 3, 4, 5]


def test_fes_count_stops_after_reaching_total():
    flow = _bare_flow()
    flow.settings = {"count": 3}
    completed = []
    flow.run_attempt = lambda: True
    flow.recover_after_play_failure = lambda _reason: None

    def progress(current, total):
        completed.append((current, total))

    flow.progress_callback = progress
    assert flow.run() is True
    assert [current for current, _ in completed] == [1, 2, 3]


def test_fes_retry_budget_not_clamped_to_one():
    flow = _bare_flow()
    flow.settings = {"count": 1, "play_failure_retry_count": 99}
    attempts = []
    flow.recover_after_play_failure = lambda _reason: None

    def attempt():
        attempts.append(True)
        return len(attempts) == 100

    flow.run_attempt = attempt
    assert flow.run() is True
    assert len(attempts) == 100


def test_fes_retry_exhaustion_propagates_failure():
    flow = _bare_flow()
    flow.settings = {"count": 1, "play_failure_retry_count": 0}
    flow.recover_after_play_failure = lambda _reason: None

    def attempt():
        raise RuntimeError("boom")

    flow.run_attempt = attempt
    with pytest.raises(RuntimeError, match="boom"):
        flow.run()


def test_fes_play_failure_returns_false_when_budget_exhausted():
    flow = _bare_flow()
    flow.settings = {"count": 1, "play_failure_retry_count": 0}
    flow.run_attempt = lambda: False
    assert flow.run() is False


def test_fes_user_stop_returns_true_mid_round():
    flow = _bare_flow()
    flow.settings = {"count": 5}

    def attempt():
        flow.context.tasker.stopping = True
        return True

    flow.run_attempt = attempt
    assert flow.run() is True


def _make_jump_flow(monkeypatch, *, jump_requested):
    """跳车测试流：伪 RealtimeProfilePlay + 信号化 live run + 记录型控制器。"""

    class Play:
        def run(self, _context, _params):
            return True

    monkeypatch.setattr(fes_action, "RealtimeProfilePlay", Play)
    monkeypatch.setattr(
        fes_action,
        "current_live_run",
        lambda: SimpleNamespace(
            disconnect_jump_requested=jump_requested,
            recording_path=None,
        ),
    )
    flow = _bare_flow()
    flow.settings = dict(DEFAULT_SETTINGS)
    flow.action_argv = lambda params: params
    keys = []
    started = []

    class Controller:
        def post_click_key(self, key):
            keys.append(key)
            return SimpleNamespace(wait=lambda: None)

        def post_start_app(self, package):
            started.append(package)
            return SimpleNamespace(wait=lambda: None)

    flow.context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=Controller()),
        run_recognition=lambda _node, _image: None,
    )
    return flow, keys, started


def test_fes_play_jump_homes_without_reopening_game(monkeypatch):
    flow, keys, started = _make_jump_flow(monkeypatch, jump_requested=True)

    with pytest.raises(fes_action.FesLifeJumpHome, match="手动断网跳车"):
        flow.play()
    # 只发 KEYCODE_HOME 切桌面；绝不 post_start_app——游戏必须留在后台，
    # 由玩家手动断网跳车（协力版会再切回游戏，Fes 版明确不切回）。
    assert keys == [3]
    assert started == []
    assert fes_action._JUMP_HOME_DONE is True
    # 复位避免污染其它用例（生产侧每轮 run() 入口也复位）。
    fes_action._JUMP_HOME_DONE = False


def test_fes_play_skips_jump_without_live_run_signal(monkeypatch):
    flow, keys, started = _make_jump_flow(monkeypatch, jump_requested=False)

    assert flow.play() is True
    assert keys == []
    assert started == []


def test_fes_run_ends_on_jump_without_retry_or_recovery(monkeypatch):
    flow = _bare_flow()
    flow.settings = {"count": 5, "play_failure_retry_count": 9}
    failures = []
    monkeypatch.setattr(fes_action, "record_failure_reason", failures.append)
    attempts = []

    def attempt():
        attempts.append(True)
        raise fes_action.FesLifeJumpHome(
            "生命归零，已切至模拟器桌面，请手动断网跳车"
        )

    flow.run_attempt = attempt
    flow.recover_after_play_failure = lambda _reason: pytest.fail(
        "跳车后禁止 recover（会把游戏从桌面拽回主页）"
    )
    assert flow.run() is False
    assert len(attempts) == 1
    assert failures and "手动断网跳车" in failures[0]


def test_fes_run_jump_normalizes_post_play_exception(monkeypatch):
    # 归零跳车信号已置位后的后续异常（原生门禁/清理等）也必须按跳车
    # 收尾：切桌面、记录原因、结束任务，绝不进重试/恢复。
    flow = _bare_flow()
    flow.settings = {"count": 5, "play_failure_retry_count": 9}
    failures = []
    monkeypatch.setattr(fes_action, "record_failure_reason", failures.append)
    monkeypatch.setattr(
        fes_action,
        "current_live_run",
        lambda: SimpleNamespace(disconnect_jump_requested=True),
    )
    homes = []
    flow._jump_home_desktop = lambda: homes.append(True)
    recovered = []
    flow.recover_after_play_failure = recovered.append

    def attempt():
        raise RuntimeError("native gate boom")

    flow.run_attempt = attempt
    assert flow.run() is False
    assert homes == [True]
    assert recovered == []
    assert failures and "native gate boom" in failures[0]


def test_fes_run_resets_jump_flag_per_task(monkeypatch):
    monkeypatch.setattr(fes_action, "_JUMP_HOME_DONE", True)
    flow = _bare_flow()
    flow.settings = {"count": 1}
    flow.run_attempt = lambda: True
    flow.recover_after_play_failure = lambda _reason: pytest.fail(
        "单轮成功后不应恢复"
    )
    assert flow.run() is True
    assert fes_action._JUMP_HOME_DONE is False


def test_fes_finalize_skips_recover_after_jump_home(monkeypatch):
    calls = []

    class _NoRecover:
        def run(self, _context, _argv):
            calls.append(True)
            return True

    monkeypatch.setattr(fes_action, "CommonRecover", _NoRecover)
    monkeypatch.setattr(fes_action, "_JUMP_HOME_DONE", True)
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    argv = SimpleNamespace(custom_action_param="{}")
    assert fes_action.FesLiveFinalize().run(context, argv) is True
    assert calls == []


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _box(x=1000, y=600, w=80, h=40):
    return SimpleNamespace(x=x, y=y, w=w, h=h)


def _ocr_stub(responses: dict):
    """按 expected 文本返回预设 box；列表按调用次序弹出，标量重复返回。"""

    def ocr(image, text, **_kwargs):
        value = responses.get(text)
        if isinstance(value, list):
            return value.pop(0) if value else None
        return value

    return ocr


def test_fes_enter_room_confirms_when_prepare_button_visible():
    flow = _bare_flow()
    flow.match_timeout_seconds = 5.0
    flow.capture = lambda: None
    flow._ocr_box = _ocr_stub({"准备完毕": _box()})
    assert flow.enter_room() is None


def test_fes_enter_room_waits_through_matching_state():
    flow = _bare_flow()
    flow.match_timeout_seconds = 10.0
    flow.capture = lambda: None
    # 前两次准备完毕未出现（列表弹出 None），匹配文案一直可见，第三次命中。
    flow._ocr_box = _ocr_stub({
        "准备完毕": [None, None, _box()],
        "的成员匹配": _box(),
    })
    assert flow.enter_room() is None


def test_fes_enter_room_times_out_with_clear_reason():
    flow = _bare_flow()
    flow.match_timeout_seconds = 0.0
    flow.capture = lambda: None
    flow._ocr_box = _ocr_stub({})
    with pytest.raises(RuntimeError, match="最终确认页"):
        flow.enter_room()


def test_fes_enter_room_navigates_from_home_between_rounds():
    flow = _bare_flow()
    flow.match_timeout_seconds = 5.0
    flow.capture = lambda: None
    # 第一次流程锚点全未命中（不在流程中），主页模板命中触发导航；
    # 导航后等待循环第一次就看到准备完毕页。
    flow._ocr_box = _ocr_stub({"准备完毕": [None, _box()]})
    flow.context.run_recognition = (
        lambda node, _image: (
            SimpleNamespace(hit=True, box=_box())
            if node == "FesHomeLive" else None
        )
    )
    navigated = []
    flow._navigate_to_entry = lambda: navigated.append(True)
    assert flow.enter_room() is None
    assert navigated == [True]


def test_fes_enter_room_skips_navigation_when_already_in_flow():
    flow = _bare_flow()
    flow.match_timeout_seconds = 5.0
    flow.capture = lambda: None
    navigated = []
    flow._navigate_to_entry = lambda: navigated.append(True)
    # 匹配文案可见 → 已在流程中，跳过导航；准备完毕随后出现。
    flow._ocr_box = _ocr_stub({
        "准备完毕": [None, _box()],
        "的成员匹配": _box(),
    })
    assert flow.enter_room() is None
    assert navigated == []


def test_fes_enter_room_taps_hub_ok_once():
    # 中继页出现“创建房间”即点底栏红色 OK（自动匹配），且只点一次。
    flow = _bare_flow()
    flow.match_timeout_seconds = 5.0
    flow.capture = lambda: None
    clicks = []
    flow.click = clicks.append
    flow._ocr_box = _ocr_stub({
        # 首次 None 被 _in_fes_flow 探测消费；第二次 None 让出 hub 分支；
        # 第三次命中“准备完毕”确认入房。
        "准备完毕": [None, None, _box()],
        "创建房间": _box(),
    })
    assert flow.enter_room() is None
    assert clicks == [(1048, 646)]


def test_fes_navigate_requires_home_entry():
    flow = _bare_flow()
    flow.entry_home_timeout_seconds = 0.0
    flow.capture = lambda: None
    flow.context.run_recognition = lambda _node, _image: None
    with pytest.raises(RuntimeError, match="演出"):
        flow._navigate_to_entry()


def test_fes_ready_up_taps_prepare_and_waits_for_loading():
    flow = _bare_flow()
    flow.ready_departure_timeout_seconds = 10.0
    flow.capture = lambda: None
    clicks = []
    flow.click = clicks.append
    flow._ocr_box = _ocr_stub({
        "准备完毕": _box(x=1000, y=600, w=80, h=40),
        # 第一次未见加载，第二次 NOW LOADING 出现 → 离开。
        "NOW LOADING": [None, _box()],
    })
    flow._ready_up_and_wait()
    assert clicks == [(1123, 630)]


def test_fes_ready_up_fails_when_button_missing():
    flow = _bare_flow()
    flow.capture = lambda: None
    flow._ocr_box = _ocr_stub({"准备完毕": None})
    with pytest.raises(RuntimeError, match="准备完"):
        flow._ready_up_and_wait()


def test_fes_ready_up_times_out_if_page_never_departs():
    flow = _bare_flow()
    flow.ready_departure_timeout_seconds = 0.0
    flow.capture = lambda: None
    flow.click = lambda _point: None
    flow._ocr_box = _ocr_stub({"准备完毕": _box()})
    with pytest.raises(RuntimeError, match="未离开最终确认页"):
        flow._ready_up_and_wait()


def test_fes_ready_up_departs_on_playfield_when_loading_missed(monkeypatch):
    # NOW LOADING 永不出现（loading 被漏/跳过，真机 2026-09-27 00:10 局根因）
    # 时，必须在演奏场成立后立即交棒，绝不挂死让引擎饿死——引擎不启动 =
    # 不读谱 + 生命归零不跳桌面（两个症状同源）。
    flow = _bare_flow()
    flow.ready_departure_timeout_seconds = 10.0
    flow.capture = lambda: None
    clicks = []
    flow.click = clicks.append
    flow._ocr_box = _ocr_stub({
        "准备完毕": _box(x=1000, y=600, w=80, h=40),
        "NOW LOADING": None,
    })
    monkeypatch.setattr(
        "agent.realtime.fes_action.PlayfieldDetector",
        lambda: (lambda _image: True),
    )
    flow._ready_up_and_wait()
    assert clicks == [(1123, 630)]


def test_fes_pipeline_contract():
    pipeline = load(ROOT / "resource" / "pipeline" / "fes_live.json")
    interface = load(ROOT / "interface.json")
    options = interface["option"]

    assert pipeline["FesLive"]["next"] == ["FesProcessConflictGuard"]
    assert pipeline["FesProcessConflictGuard"]["custom_action"] == "ProcessConflictGuard"
    assert pipeline["FesRecover"]["custom_action"] == "CommonRecover"
    assert pipeline["FesRecover"]["next"] == ["FesEntryConfigure"]
    for name in (
        "FesEntryConfigure",
        "FesDifficultyConfigure",
        "FesCountConfigure",
        "FesDebugConfigure",
    ):
        assert pipeline[name]["custom_action"] == "FesLiveConfigure"
    # v1 只支持自动匹配：无房间号配置节点。
    assert "FesRoomCodeConfigure" not in pipeline
    assert pipeline["FesEntryConfigure"]["next"] == ["FesDifficultyConfigure"]
    assert pipeline["FesDifficultyConfigure"]["next"] == ["FesCountConfigure"]
    assert pipeline["FesCountConfigure"]["next"] == ["FesDebugConfigure"]
    assert pipeline["FesDebugConfigure"]["next"] == ["FesSpeedSettingsGate"]
    assert pipeline["FesSpeedSettingsGate"]["custom_action"] == "RealtimeGameSpeedSettingsGate"
    assert pipeline["FesRun"]["custom_action"] == "FesLiveFlow"
    assert pipeline["FesReturnHome"]["custom_action"] == "FesLiveFinalize"
    assert pipeline["FesComplete"]["custom_action"] == "TaskOutcome"
    assert pipeline["FesComplete"]["custom_action_param"]["status"] == "success"
    assert pipeline["FesFailure"]["custom_action"] == "TaskOutcome"
    assert pipeline["FesFailure"]["custom_action_param"]["status"] == "failure"
    assert pipeline["FesFailure"]["custom_action_param"]["reason_source"] == "latest"

    task = next(task for task in interface["task"] if task["name"] == "FesLive")
    assert task["entry"] == "FesLive"
    assert task["option"] == ["FesDifficulty", "FesCount", "FesDebug"]

    count = options["FesCount"]
    assert count["type"] == "input"
    assert count["inputs"][0]["pipeline_type"] == "int"
    assert count["pipeline_override"]["FesCountConfigure"]["custom_action_param"] == {
        "count": "{Count}"
    }

    difficulty_cases = options["FesDifficulty"]["cases"]
    assert [case["name"] for case in difficulty_cases] == [
        "Easy", "Normal", "Hard", "Expert",
    ]
    for case in difficulty_cases:
        override = case["pipeline_override"]["FesDifficultyConfigure"]
        assert override["custom_action_param"] == {"difficulty": case["name"]}
        gate = case["pipeline_override"]["FesSpeedSettingsGate"]["custom_action_param"]
        assert gate["difficulty"] == case["name"]

    debug_cases = options["FesDebug"]["cases"]
    assert [case["name"] for case in debug_cases] == ["Light", "Off", "Full"]

    # 导航入口使用 OCR（无 fes 专属模板），LiveSelectFind 先模板后 OCR，
    # 未提供 template_node 时直接 OCR 识别“团队演出”。
    assert pipeline["FesSelectPage"]["custom_action"] == "LiveSelectFind"
    assert pipeline["FesSelectPage"]["custom_action_param"]["click"] is False
    assert pipeline["FesEnterLive"]["custom_action_param"]["click"] is True
    assert pipeline["FesEnterLive"]["custom_action_param"]["expected"] == "团队演出"


def test_fes_interface_options_resolve_in_real_maafw(tmp_path):
    import os
    import subprocess
    import sys

    interface = load(ROOT / "interface.json")
    pipeline = load(ROOT / "resource/pipeline/fes_live.json")
    nodes = (
        "FesDifficultyConfigure",
        "FesDebugConfigure",
        "FesSpeedSettingsGate",
    )
    difficulty_case = next(
        case for case in interface["option"]["FesDifficulty"]["cases"]
        if case["name"] == "Expert"
    )
    debug_case = next(
        case for case in interface["option"]["FesDebug"]["cases"]
        if case["name"] == "Light"
    )
    code = """
import json, sys
from maa.resource import Resource
from maa.toolkit import Toolkit
data = json.load(sys.stdin)
Toolkit.init_option(data['log_dir'])
resource = Resource()
assert resource.override_pipeline({node: data['pipeline'][node] for node in data['nodes']})
assert resource.override_pipeline(data['difficulty'])
assert resource.override_pipeline(data['debug'])
effective = {}
for node in data['nodes']:
    effective[node] = resource.get_node_data(node)['action']['param']['custom_action_param']
print(json.dumps(effective))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        input=json.dumps({
            "log_dir": str(tmp_path),
            "pipeline": pipeline,
            "nodes": nodes,
            "difficulty": difficulty_case["pipeline_override"],
            "debug": debug_case["pipeline_override"],
        }),
        text=True,
        capture_output=True,
        check=True,
        env={"PYTHONUTF8": "1", "PATH": os.environ["PATH"]},
    )
    effective = json.loads(result.stdout)
    assert effective["FesDifficultyConfigure"] == {"difficulty": "Expert"}
    assert effective["FesSpeedSettingsGate"]["difficulty"] == "Expert"
    assert effective["FesDebugConfigure"] == {
        "debug_recording": False,
        "diagnostic_trace": True,
    }
