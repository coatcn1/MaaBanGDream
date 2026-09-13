"""协力演出生命归零恢复路径与首拍冻结阈值的回归测试。

对应改动：
- `CooperativeLiveFlow.play()` 在生命归零后的三路恢复；
- `CooperativeLiveFlow.run()` 单局重试预算用尽后的继续策略；
- `NativeStartPhotogate._required_stable_s()` 在“本局从未确认到准备弹窗”时的冻结阈值。

测试使用轻量桩对象，不依赖真机、MaaFramework 控制器或模板图片。
"""
import numpy as np

from agent.realtime import cooperative_action as ca
from agent.realtime import native_play as npmod

FRAME = (720, 1280, 3)
FLOW = ca.CooperativeLiveFlow


class _Waiter:
    def wait(self, timeout=None):
        return self


class _Controller:
    """记录 stop/start 调用，用来断言是否真的重启了客户端。"""

    def __init__(self):
        self.calls = []

    def post_click_key(self, key):
        self.calls.append("key:" + str(key))
        return _Waiter()

    def post_stop_app(self, package):
        self.calls.append("stop:" + str(package))
        return _Waiter()

    def post_start_app(self, package):
        self.calls.append("start:" + str(package))
        return _Waiter()


class _Run:
    disconnect_jump_requested = True


class _PlayStub:
    performance_aborted_by_disconnect = FLOW.performance_aborted_by_disconnect
    dismiss_disconnect_dialogs = FLOW.dismiss_disconnect_dialogs
    play = FLOW.play

    def __init__(self, dialog, jump_enabled=False, jump_result=True):
        self.dialog = dialog
        self.dismissed = 0
        self.jump_calls = 0
        self.jump_result = jump_result
        self.settings = dict(ca.DEFAULT_SETTINGS)
        self.settings["disconnect_jump_enabled"] = jump_enabled
        self.context = None
        self.controller = _Controller()
        self.templates = {
            name: np.zeros((4, 4, 3), np.uint8)
            for name in ca.TEMPLATE_POSITIONS
        }

    def capture(self):
        return np.full(FRAME, 128, np.uint8)

    def stopped(self):
        return False

    def visible(self, image, name, threshold=0.90):
        if not self.dialog:
            return False
        return name in (
            "disconnect_continue_body",
            "disconnect_confirm_body",
        )

    def action_argv(self, params):
        return []

    def _wait_and_click(self, name, point, timeout):
        self.dismissed += 1
        return True

    def restart_game_and_wait(self, settle_seconds: float = 0.1):
        return FLOW.restart_game_and_wait(self, settle_seconds=settle_seconds)

    def disconnect_jump_out(self, popup_timeout_s: float = 25.0):
        self.jump_calls += 1
        return self.jump_result


class _FakePlayAction:
    def run(self, context, argv):
        return True


def _run_play(monkeypatch, dialog, jump_enabled=False, jump_result=True):
    stub = _PlayStub(dialog, jump_enabled, jump_result)
    monkeypatch.setattr(ca, "RealtimeProfilePlay", _FakePlayAction)
    monkeypatch.setattr(ca, "current_live_run", lambda: _Run())
    result = stub.play()
    return stub, result


class _GateStub:
    _required_stable_s = npmod.NativeStartPhotogate._required_stable_s

    def __init__(self, popup_frames, playfield_seen_at_s):
        self._popup_gate_enabled = True
        self.prepare_popup_frames = popup_frames
        self._no_popup_stable_s = 1.2
        self._stable_duration_s = 0.12
        self.playfield_seen_at_s = playfield_seen_at_s
        self._no_popup_stable_window_s = 60.0


def _image():
    return np.full(FRAME, 128, np.uint8)


def test_performance_aborted_by_disconnect_detects_dialog():
    assert _PlayStub(True).performance_aborted_by_disconnect(_image()) is True


def test_performance_aborted_by_disconnect_ignores_clean_screen():
    assert _PlayStub(False).performance_aborted_by_disconnect(_image()) is False


def test_network_dialog_dismisses_and_retries_round(monkeypatch):
    stub, result = _run_play(monkeypatch, dialog=True)

    assert result is False
    assert stub.dismissed == 2
    assert stub.jump_calls == 0
    assert stub.controller.calls == []


def test_missing_dialog_restarts_client_and_continues(monkeypatch):
    stub, result = _run_play(monkeypatch, dialog=False)

    assert result is False
    assert stub.dismissed == 0
    assert stub.controller.calls == [
        "stop:" + ca.GAME_PACKAGE,
        "start:" + ca.GAME_PACKAGE,
    ]


def test_disconnect_jump_is_executed_when_enabled(monkeypatch):
    stub, result = _run_play(
        monkeypatch, dialog=False, jump_enabled=True, jump_result=True
    )

    assert result is False
    assert stub.jump_calls == 1
    assert stub.controller.calls == []


def test_failed_disconnect_jump_falls_back_to_client_restart(monkeypatch):
    stub, result = _run_play(
        monkeypatch, dialog=False, jump_enabled=True, jump_result=False
    )

    assert result is False
    assert stub.jump_calls == 1
    assert stub.controller.calls == [
        "stop:" + ca.GAME_PACKAGE,
        "start:" + ca.GAME_PACKAGE,
    ]


class _MemberExitStub:
    handle_member_exit = FLOW.handle_member_exit

    def __init__(self, streak):
        self.member_exit_streak = streak
        self.settings = {
            "member_exit_policy": "reconnect",
            "max_reconnects": 3,
        }
        self.recoveries = []
        self.dismissed = 0

    def dismiss_member_exit(self):
        self.dismissed += 1
        return True

    def recover_after_play_failure(self, reason):
        self.recoveries.append(reason)
        return True


def test_member_exit_reconnect_limit_recovers_and_continues(monkeypatch):
    monkeypatch.setattr(ca, "record_failure_reason", lambda *a, **k: None)
    stub = _MemberExitStub(streak=0)

    result = stub.handle_member_exit(3)

    assert result == 0
    assert stub.member_exit_streak == 1
    assert stub.recoveries == ["成员退出重连达到上限"]


def test_member_exit_reconnect_limit_ends_task_after_three_streak(monkeypatch):
    monkeypatch.setattr(ca, "record_failure_reason", lambda *a, **k: None)
    stub = _MemberExitStub(streak=ca.MEMBER_EXIT_STREAK_LIMIT - 1)

    result = stub.handle_member_exit(3)

    assert result is None
    assert stub.recoveries == []


def test_late_trigger_without_popup_still_requires_full_quiet_window():
    # 2026-09-12 20:22：演奏场 9.657s 才出现，触发点在 9.09s 之后。
    # 旧实现用 8 秒窗口，阈值退回 120ms，于是冻结在 133ms 的假安静窗口上。
    gate = _GateStub(popup_frames=0, playfield_seen_at_s=9.657)

    assert gate._required_stable_s(9.657 + 9.09) == 1.2


def test_detected_popup_keeps_fast_freeze_threshold():
    gate = _GateStub(popup_frames=94, playfield_seen_at_s=5.127)

    assert gate._required_stable_s(5.127 + 6.1) == 0.12
