from types import SimpleNamespace

import pytest

from agent.realtime import cooperative_action, versus_action


@pytest.mark.parametrize("module", [cooperative_action, versus_action])
@pytest.mark.parametrize("kind", ["member", "room"])
def test_preplay_recovery_ignores_old_policy_and_cumulative_limit(module, kind, monkeypatch):
    flow_type = getattr(module, "CooperativeLiveFlow", None) or module.VersusLiveFlow
    flow = object.__new__(flow_type)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow.settings = {"count": 2, "entry_method": "normal", "member_exit_policy": "fail",
                     "max_reconnects": 0, "play_failure_retry_count": 0}
    flow.verify_preplay_recovery = lambda: None
    flow.room_recovery_trial = False
    error = module.MemberExited if kind == "member" else module.RoomPreparationTimeout
    outcomes = iter([error("preplay")] * 5 + [True, error("preplay"), True])
    attempts, completed, recovered, failures = [], [], [], []
    def attempt(reuse_room=False):
        attempts.append(reuse_room)
        result = next(outcomes)
        if isinstance(result, Exception):
            raise result
        return result
    flow.run_attempt = attempt
    flow.dismiss_member_exit = lambda: None
    flow.return_to_room_selection = lambda: recovered.append("selection")
    flow.recover_room_preparation_timeout = lambda reason: recovered.append(reason)
    flow.progress_callback = lambda count, total: completed.append(count)
    monkeypatch.setattr(module, "record_failure_reason", failures.append)
    assert flow.run() is True
    assert attempts == [False] * 8
    assert completed == [1, 2]
    assert failures == []


@pytest.mark.parametrize("module", [cooperative_action, versus_action])
def test_member_recovery_preserves_stop_exception(module):
    flow_type = getattr(module, "CooperativeLiveFlow", None) or module.VersusLiveFlow
    flow = object.__new__(flow_type)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow.settings = {"member_exit_policy": "reconnect", "max_reconnects": 3}
    flow.verify_preplay_recovery = lambda: None
    flow.dismiss_member_exit = lambda: None
    flow.return_to_room_selection = lambda: (_ for _ in ()).throw(RuntimeError("not home"))
    flow.recover_room_preparation_timeout = lambda reason: (_ for _ in ()).throw(InterruptedError("stop"))
    with pytest.raises(InterruptedError):
        flow.handle_member_exit(0)


@pytest.mark.parametrize("scene", ["black", "unknown", "generic-error-title", "ready", "home"])
def test_preplay_recovery_requires_a_new_safe_page(monkeypatch, scene):
    from agent.realtime import room_song_entry
    clock = [0.0]
    observations, dismissals = [], []
    flow = SimpleNamespace(stopped=lambda: False,
        capture=lambda: observations.append(clock[0]) or scene,
        visible=lambda image, name, threshold=0.90: (image == "ready" and name == "ready_button") or
            (image == "generic-error-title" and name == "member_exit_title"),
        dismiss_member_exit=lambda: dismissals.append(clock[0]),
        pipeline_box=lambda image, name: object() if image == "home" else None)
    monkeypatch.setattr(room_song_entry.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(room_song_entry.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    if scene in {"ready", "home"}:
        room_song_entry.wait_for_preplay_recovery_page(flow, "room_wait", "HomeMarker")
        assert len(observations) == 1
        assert dismissals == []
    elif scene == "generic-error-title":
        with pytest.raises(RuntimeError, match="反复出现"):
            room_song_entry.wait_for_preplay_recovery_page(flow, "room_wait", "HomeMarker")
        assert len(dismissals) == 3
        assert 0.0 < clock[0] < 60.0
    else:
        with pytest.raises(RuntimeError, match="安全页面"):
            room_song_entry.wait_for_preplay_recovery_page(flow, "room_wait", "HomeMarker")
        assert clock[0] >= 60.0
        assert dismissals == []


def test_preplay_recovery_stop_after_capture_does_no_recognition():
    from agent.realtime.room_song_entry import wait_for_preplay_recovery_page
    stopped = [False]
    def capture():
        stopped[0] = True
        return "ready"
    flow = SimpleNamespace(stopped=lambda: stopped[0], capture=capture,
        visible=lambda *args: pytest.fail("停止后不再识别或输入"))
    with pytest.raises(InterruptedError):
        wait_for_preplay_recovery_page(flow, "room_wait", "HomeMarker")


@pytest.mark.parametrize("module", [cooperative_action, versus_action])
def test_play_exception_never_scans_member_exit_title(module, monkeypatch):
    flow_type = getattr(module, "CooperativeLiveFlow", None) or module.VersusLiveFlow
    flow = object.__new__(flow_type)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow.settings = {}
    flow.enter_room = lambda: None
    flow.wait_for_preparation = lambda: None
    flow.prepare = lambda: None
    flow.play = lambda: (_ for _ in ()).throw(RuntimeError("real-input-failed"))
    flow.capture = lambda: pytest.fail("演奏异常不能扫描通用成员退出标题")
    with pytest.raises(RuntimeError, match="real-input-failed"):
        flow.run_attempt()


@pytest.mark.parametrize("module", [cooperative_action, versus_action])
def test_prepare_integrity_error_is_not_reclassified_by_generic_error_title(module):
    flow_type = getattr(module, "CooperativeLiveFlow", None) or module.VersusLiveFlow
    flow = object.__new__(flow_type)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow.settings = {}
    flow.enter_room = lambda: None
    flow.wait_for_preparation = lambda: None
    flow.prepare = lambda: (_ for _ in ()).throw(ValueError("Profile-speed-conflict"))
    flow.capture = lambda: "generic-error-title"
    flow.visible = lambda *args: True
    with pytest.raises(ValueError, match="Profile-speed-conflict"):
        flow.run_attempt()
