from types import SimpleNamespace
import json

import pytest

from agent.realtime import runtime_options


@pytest.mark.parametrize("invalid_id", [None, 0, -1, True, "42"])
def test_full_recording_requires_current_integer_task_identity(monkeypatch, invalid_id):
    monkeypatch.setattr(runtime_options, "_DEBUG_BY_TASK", {})
    monkeypatch.setattr(runtime_options, "_DEBUG_ENABLED", False)
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    full = SimpleNamespace(task_detail=SimpleNamespace(task_id=42), custom_action_param=json.dumps({"debug_recording": True}))
    assert runtime_options.RealtimeRuntimeOptions().run(context, full)
    assert runtime_options.debug_recording_enabled(full)
    other = SimpleNamespace(task_detail=SimpleNamespace(task_id=invalid_id))
    assert not runtime_options.debug_recording_enabled(other)
    assert not runtime_options.debug_recording_enabled(SimpleNamespace())


def test_stopping_does_not_mutate_recording_task_gate(monkeypatch):
    monkeypatch.setattr(runtime_options, "_DEBUG_BY_TASK", {})
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=True))
    argv = SimpleNamespace(task_detail=SimpleNamespace(task_id=42), custom_action_param=json.dumps({"debug_recording": True}))
    assert runtime_options.RealtimeRuntimeOptions().run(context, argv)
    assert not runtime_options.debug_recording_enabled(argv)


@pytest.mark.parametrize("invalid_id", [None, 0, -1, True, "42"])
def test_adaptive_diagnostics_require_task_identity_and_reset_with_choice(monkeypatch, invalid_id):
    monkeypatch.setattr(runtime_options, "_ADAPTIVE_BY_TASK", {})
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    argv = SimpleNamespace(task_detail=SimpleNamespace(task_id=42),
        custom_action_param=json.dumps({"adaptive_diagnostics": True}))
    assert runtime_options.RealtimeRuntimeOptions().run(context, argv)
    assert runtime_options.adaptive_diagnostics_enabled(argv)
    other = SimpleNamespace(task_detail=SimpleNamespace(task_id=invalid_id))
    assert not runtime_options.adaptive_diagnostics_enabled(other)
    assert not runtime_options.adaptive_diagnostics_enabled(SimpleNamespace(task_detail=SimpleNamespace(task_id=43)))
    argv.custom_action_param = json.dumps({"opening_recording": True, "adaptive_diagnostics": False})
    assert runtime_options.RealtimeRuntimeOptions().run(context, argv)
    assert not runtime_options.adaptive_diagnostics_enabled(argv)
