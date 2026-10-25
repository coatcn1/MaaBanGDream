import json
from types import SimpleNamespace

import pytest

from agent.realtime import performance_state


def events(capsys):
    return [json.loads(line.removeprefix(performance_state.PREFIX))
            for line in capsys.readouterr().out.splitlines()
            if line.startswith(performance_state.PREFIX)]


@pytest.mark.parametrize("failure", [None, RuntimeError("engine failure"), InterruptedError("stop")])
def test_engine_window_always_exits_with_same_task_and_unique_round(capsys, failure):
    argv = SimpleNamespace(task_detail=SimpleNamespace(task_id=42))

    def run():
        with performance_state.performance_window(argv):
            assert events(capsys)[0]["active"] is True
            if failure:
                raise failure

    if failure:
        with pytest.raises(type(failure), match=str(failure)):
            run()
    else:
        run()
    exited = events(capsys)
    assert len(exited) == 1 and exited[0]["active"] is False
    assert exited[0]["task_id"] == 42
    with performance_state.performance_window(argv):
        pass
    second = events(capsys)
    assert [event["active"] for event in second] == [True, False]
    assert second[0]["run_token"] == second[1]["run_token"]
    assert second[0]["run_token"] != exited[0]["run_token"]


@pytest.mark.parametrize("task_id", [None, True, 0, -1, "42"])
def test_missing_trusted_framework_identity_does_not_invent_state(capsys, task_id):
    with performance_state.performance_window(SimpleNamespace(task_detail=SimpleNamespace(task_id=task_id))):
        pass
    assert events(capsys) == []


@pytest.mark.parametrize("failed_write", [1, 2])
def test_bad_stdout_does_not_skip_engine_or_replace_its_exception(monkeypatch, failed_write):
    writes = []

    def write(*args, **kwargs):
        writes.append(args)
        if len(writes) == failed_write:
            raise BrokenPipeError("closed pipe")

    monkeypatch.setattr(performance_state, "print", write, raising=False)
    entered = []
    original = RuntimeError("original engine exception")
    with pytest.raises(RuntimeError) as caught:
        with performance_state.performance_window(SimpleNamespace(task_detail=SimpleNamespace(task_id=42))):
            entered.append(True)
            raise original
    assert caught.value is original
    assert entered == [True] and len(writes) == 2


def test_notification_setup_failure_preserves_engine(monkeypatch):
    monkeypatch.setattr(performance_state.uuid, "uuid4", lambda: (_ for _ in ()).throw(OSError("entropy")))
    with performance_state.performance_window(SimpleNamespace(task_detail=SimpleNamespace(task_id=42))):
        reached_engine = True
    assert reached_engine


@pytest.mark.parametrize("mode", ["cooperative", "versus"])
def test_room_flow_passes_original_task_identity_to_actual_profile_call(monkeypatch, capsys, mode):
    if mode == "cooperative":
        from agent.realtime import cooperative_action as module
        cls = module.CooperativeLiveFlow
    else:
        from agent.realtime import versus_action as module
        cls = module.VersusLiveFlow
    detail = SimpleNamespace(task_id=51)
    flow = object.__new__(cls)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow.stamina_argv = SimpleNamespace(task_detail=detail)
    flow.settings = dict(module.DEFAULT_SETTINGS)
    captured = []

    class ProfilePlay:
        def run_for_flow(self, _context, argv):
            captured.append(argv.task_detail)
            with performance_state.performance_window(argv):
                pass
            return True

    monkeypatch.setattr(module, "RealtimeProfilePlay", ProfilePlay)
    monkeypatch.setattr(module, "current_live_run", lambda: None)
    assert flow.play() is True
    assert captured == [detail]
    assert [event["task_id"] for event in events(capsys)] == [51, 51]
