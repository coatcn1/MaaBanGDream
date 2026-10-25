"""匹配恢复的离线页面回放；不连接设备或实时引擎。"""
from types import SimpleNamespace

import pytest

from agent.realtime import cooperative_action as coop
from agent.realtime import versus_action as versus


@pytest.fixture(params=[coop, versus], ids=["cooperative", "versus"])
def replay(request, monkeypatch):
    module = request.param
    cls = module.CooperativeLiveFlow if module is coop else module.VersusLiveFlow
    flow = object.__new__(cls)
    clock = [0.0]
    state = {"page": "popup", "tier": "free", "stop_at": None}
    events = []
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow.settings = dict(module.DEFAULT_SETTINGS, room_tier="free", room_type="free")
    flow.room_recovery_trial = False
    flow.capture = lambda: state["page"]
    lobby = "room_wait" if module is coop else "room_together"
    flow.visible = lambda image, name, *a: (
        (image == "popup" and name == "room_match_wait")
        or (image == "selection" and name == "room_search")
        or (image == "lobby" and name == lobby)
        or (image == "member_exit" and name == "member_exit_title")
        or (image == "connection_failed" and name == "connect_failed_body")
    )
    classifier = "classify_room_tier" if module is coop else "classify_room_type"
    monkeypatch.setattr(module, classifier, lambda image: state["tier"])
    monkeypatch.setattr(module, "require_game_foreground", lambda controller: None)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    def sleep(seconds):
        clock[0] += seconds
        if state["stop_at"] is not None and clock[0] >= state["stop_at"]:
            flow.context.tasker.stopping = True
        if state.get("lobby_at") is not None and clock[0] >= state["lobby_at"]:
            state["page"] = "lobby"
    monkeypatch.setattr(module.time, "sleep", sleep)
    def click(point):
        events.append(("click", point))
        if point == module.ROOM_MATCH_CANCEL_POINT:
            state["page"] = state.get("after_cancel", "selection")
        else:
            state["page"] = "lobby"
    def run_task(node, override):
        assert node == "RealtimeSettingsClick"
        click(tuple(override[node]["target"][:2]))
        return SimpleNamespace(status=SimpleNamespace(succeeded=state.get("job_ok", True)))
    flow.context.run_task = run_task
    flow.click = lambda point: pytest.fail("恢复输入不能再进入通用二次前台保护")
    def controller(label):
        def post(key):
            events.append((label, key))
            state["page"] = state.get("after_back", "selection")
            return SimpleNamespace(wait=lambda: None, succeeded=state.get("job_ok", True))
        return SimpleNamespace(post_click_key=post)
    flow.context.tasker.controller = controller("back")
    confirm = module.ROOM_CONFIRM_POINT if module is coop else module.VERSUS_CONFIRM_POINT
    return SimpleNamespace(module=module, flow=flow, state=state, clock=clock,
                           events=events, confirm=confirm, controller=controller)


@pytest.mark.parametrize("page", ["lobby", "unknown", "black", "selection"])
def test_cancel_observes_destination_before_reconfirm(replay, page):
    r = replay
    r.state.update(after_cancel=page, lobby_at=2.0 if page in {"unknown", "black"} else None)
    outcome = r.flow.recover_room_match_stall(r.confirm)
    assert outcome == ("reconfirmed" if page == "selection" else "entered")
    assert r.events == [("click", r.module.ROOM_MATCH_CANCEL_POINT)] + (
        [("click", r.confirm)] if page == "selection" else []
    )


def test_already_entered_before_cancel_sends_nothing(replay):
    replay.state["page"] = "lobby"
    assert replay.flow.recover_room_match_stall(replay.confirm) == "entered"
    assert replay.events == []


def test_back_can_arrive_in_lobby_without_reconfirm(replay):
    r = replay
    r.state.update(after_cancel="popup", after_back="lobby")
    assert r.flow.recover_room_match_stall(r.confirm) == "entered"
    assert r.events == [("click", r.module.ROOM_MATCH_CANCEL_POINT), ("back", 4)]


@pytest.mark.parametrize("tier,confirm", [("wrong", True), ("free", False)])
def test_reconfirm_requires_target_tier_and_original_point(replay, tier, confirm):
    r = replay
    r.state["tier"] = tier
    assert r.flow.recover_room_match_stall(r.confirm if confirm else None) == "pending"
    assert r.events == [("click", r.module.ROOM_MATCH_CANCEL_POINT)]


def test_unknown_after_popup_times_out_with_typed_failure(replay):
    r = replay
    r.state["after_cancel"] = "unknown"
    with pytest.raises(r.module.RoomEntryTransient, match="typed timeout"):
        r.flow.verify_room_entry("typed timeout", confirm_point=r.confirm)
    assert r.clock[0] <= 45
    assert r.events == [("click", r.module.ROOM_MATCH_CANCEL_POINT)]


def test_stop_during_last_cancel_sleep_prevents_back(replay):
    r = replay
    r.state.update(after_cancel="popup", stop_at=6.0)
    with pytest.raises(InterruptedError):
        r.flow.recover_room_match_stall(r.confirm)
    assert r.events == [("click", r.module.ROOM_MATCH_CANCEL_POINT)]


@pytest.mark.parametrize("action", ["cancel", "back", "reconfirm"])
def test_stop_during_foreground_guard_prevents_input(replay, monkeypatch, action):
    r = replay
    r.state["after_cancel"] = "popup" if action == "back" else "selection"
    guards = []
    def guard(_controller):
        guards.append(True)
        if len(guards) == (1 if action == "cancel" else 3):
            r.flow.context.tasker.stopping = True
    monkeypatch.setattr(r.module, "require_game_foreground", guard)
    with pytest.raises(InterruptedError):
        r.flow.recover_room_match_stall(r.confirm)
    assert r.events == ([] if action == "cancel" else [("click", r.module.ROOM_MATCH_CANCEL_POINT)])


def test_back_reacquires_controller_after_guard(replay, monkeypatch):
    r = replay
    r.state.update(after_cancel="popup", after_back="lobby")
    def guard(_controller):
        r.flow.context.tasker.controller = r.controller("fresh-back")
    monkeypatch.setattr(r.module, "require_game_foreground", guard)
    assert r.flow.recover_room_match_stall(r.confirm) == "entered"
    assert r.events[-1] == ("fresh-back", 4)


def test_back_job_failure_is_not_swallowed(replay):
    r = replay
    r.state.update(after_cancel="popup", job_ok=False)
    def run_task(node, override):
        r.events.append(("click", tuple(override[node]["target"][:2])))
        return SimpleNamespace(status=SimpleNamespace(succeeded=True))
    r.flow.context.run_task = run_task
    with pytest.raises(RuntimeError, match="BACK"):
        r.flow.recover_room_match_stall(r.confirm)


def test_popup_disappearance_does_not_accept_unknown_frames(replay, monkeypatch):
    r = replay
    def sleep(seconds):
        r.clock[0] += seconds
        if r.clock[0] >= 1:
            r.state["page"] = "unknown"
    monkeypatch.setattr(r.module.time, "sleep", sleep)
    with pytest.raises(r.module.RoomEntryTransient):
        r.flow.verify_room_entry("timeout", confirm_point=r.confirm)
    assert r.events == []


@pytest.mark.parametrize("action", ["cancel", "back", "reconfirm"])
def test_page_changes_during_guard_do_not_use_old_coordinates(replay, monkeypatch, action):
    r = replay
    r.state["after_cancel"] = "popup" if action == "back" else "selection"
    guards = []
    def guard(_controller):
        guards.append(True)
        if len(guards) == (1 if action == "cancel" else 3):
            r.state["page"] = "lobby"
    monkeypatch.setattr(r.module, "require_game_foreground", guard)
    assert r.flow.recover_room_match_stall(r.confirm) == "entered"
    assert r.events == ([] if action == "cancel" else [("click", r.module.ROOM_MATCH_CANCEL_POINT)])


def test_cancel_job_failure_is_not_swallowed(replay):
    replay.state["job_ok"] = False
    with pytest.raises(RuntimeError, match="点击"):
        replay.flow.recover_room_match_stall(replay.confirm)


@pytest.mark.parametrize("page", ["member_exit", "connection_failed"])
def test_typed_preplay_failures_remain_owned_by_existing_paths(replay, page):
    r = replay
    r.state["after_cancel"] = page
    error = r.module.MemberExited if page == "member_exit" else r.module.RoomEntryTransient
    with pytest.raises(error):
        r.flow.recover_room_match_stall(r.confirm)
    assert r.events == [("click", r.module.ROOM_MATCH_CANCEL_POINT)]


def test_cancel_timeout_starts_after_input_not_before_guard(replay, monkeypatch):
    r = replay
    r.state["after_cancel"] = "popup"
    def guard(_controller):
        if not r.events:
            r.clock[0] += 3.5
    monkeypatch.setattr(r.module, "require_game_foreground", guard)
    assert r.flow.recover_room_match_stall(r.confirm) == "pending"
    assert r.events == [("click", r.module.ROOM_MATCH_CANCEL_POINT)]


@pytest.mark.parametrize("action", ["cancel", "back", "reconfirm"])
def test_stop_during_input_prevents_post_input_work(replay, monkeypatch, action):
    r = replay
    r.state["after_cancel"] = "popup" if action == "back" else "selection"
    original = r.flow.context.run_task
    def run_task(node, override):
        result = original(node, override)
        if action == "cancel" or (action == "reconfirm" and len(r.events) == 2):
            r.flow.context.tasker.stopping = True
        return result
    r.flow.context.run_task = run_task
    if action == "back":
        def back(key):
            r.events.append(("back", key))
            return SimpleNamespace(wait=lambda: setattr(r.flow.context.tasker, "stopping", True), succeeded=True)
        r.flow.context.tasker.controller = SimpleNamespace(post_click_key=back)
    monkeypatch.setattr(r.module, "require_game_foreground", lambda _c: (
        pytest.fail("停止后不能再做前台查询") if r.flow.stopped() else None
    ))
    with pytest.raises(InterruptedError):
        r.flow.recover_room_match_stall(r.confirm)
    assert len(r.events) == (1 if action == "cancel" else 2)
