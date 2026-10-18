from types import SimpleNamespace
import json
from pathlib import Path

import pytest
import agent.realtime.cooperative_action as action


def bare_flow(method="host_invite_all"):
    flow = object.__new__(action.CooperativeLiveFlow)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow.settings = dict(action.DEFAULT_SETTINGS, entry_method=method)
    flow.task_id = "task-42"
    flow.friend_machine = None
    return flow


def test_host_invite_precedes_start_and_existing_preparation(monkeypatch):
    events = []
    class Machine:
        def __init__(self, adapter, *, task_id):
            assert task_id == "task-42"
        def host_invite_all(self, **kwargs):
            assert kwargs == dict(timeout_s=90.0, expected_members=2)
            events.append("invite")
        def start_host_room(self, **kwargs):
            assert kwargs == dict(timeout_s=30.0, expected_members=2)
            events.append("start")
    monkeypatch.setattr(action, "CooperativeFriendFlow", Machine)
    monkeypatch.setattr(action, "CooperativeFriendUIAdapter", lambda flow, role: role)
    flow = bare_flow()
    flow.wait_for_preparation = lambda: events.append("preparation")
    flow.prepare = lambda: events.append("prepare")
    flow.play = lambda: True
    assert flow.run_attempt()
    assert events == ["invite", "start", "preparation", "prepare"]
    events.clear()
    assert flow.run_attempt(reuse_room=True)
    assert events == ["start", "preparation", "prepare"]


def test_guest_delegates_unique_invitation_validation(monkeypatch):
    calls = []
    machine = SimpleNamespace(join_friend_invitation=lambda **kw: calls.append(kw))
    flow = bare_flow("friend")
    flow.new_friend_machine = lambda role: machine if role == "guest" else pytest.fail(role)
    flow.open_room_search = lambda: pytest.fail("legacy blind row flow")
    flow.enter_room()
    assert calls == [dict(timeout_s=90.0)]


@pytest.mark.parametrize("task_id", [None, "", "  ", 42])
def test_friend_operation_missing_stable_task_id_fails_before_ui(monkeypatch, task_id):
    flow = bare_flow()
    flow.task_id = task_id
    monkeypatch.setattr(action, "CooperativeFriendUIAdapter", lambda *a, **kw: pytest.fail("must not inspect UI"))
    with pytest.raises(ValueError, match="任务 ID"):
        flow.new_friend_machine("host")


@pytest.mark.parametrize("value,expected", [(42, "42"), ("abc", "abc"), (None, None), (0, None), (False, None)])
def test_task_id_only_uses_callback_task_detail(value, expected):
    assert action.cooperative_task_id(SimpleNamespace(task_detail=SimpleNamespace(task_id=value))) == expected
    assert action.cooperative_task_id(SimpleNamespace(tasker=SimpleNamespace(_handle=42))) is None


def test_host_stay_advances_only_completed_rounds_without_reinviting():
    flow = bare_flow()
    flow.settings.update(count=3, post_live_action="stay")
    events = []
    flow.friend_machine = SimpleNamespace(
        begin_next_round=lambda **kw: events.append(kw["completed_round_token"])
    )
    flow.run_attempt = lambda reuse_room=False: events.append(reuse_room) or True
    flow.stay_in_room = lambda: events.append("stay")
    flow.return_to_room_selection = lambda: pytest.fail("host must preserve room")
    assert flow.run()
    assert events == [False, "stay", "task-42:completed:1", True, "stay", "task-42:completed:2", True, "stay"]


def test_host_option_preserves_complete_entry_override_and_stay():
    root = Path(__file__).parents[1]
    interface = json.loads((root / "interface.json").read_text(encoding="utf-8"))
    cases = interface["option"]["CooperativeEntryMethod"]["cases"]
    host = next(case for case in cases if case["name"] == "HostInviteAll")
    assert host["option"] == ["CooperativePostLiveAction"]
    assert host["pipeline_override"]["CooperativeEntryConfigure"]["custom_action_param"] == {
        "reset": True, "entry_method": "host_invite_all"
    }
    assert action.should_stay_in_room(dict(entry_method="host_invite_all", post_live_action="stay"))
