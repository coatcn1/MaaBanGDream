from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.framework_logging import configure_framework_logging, FRAMEWORK_LOGGING_ENV


@pytest.mark.parametrize("raw,enabled,path", [
    (None, False, ""), ("0", False, ""), ("1", True, "./debug"),
    ("true", False, ""), ("", False, ""), ("2", False, ""),
])
def test_agent_file_logging_uses_explicit_startup_snapshot(raw, enabled, path):
    warnings = []
    calls = []
    environment = {} if raw is None else {FRAMEWORK_LOGGING_ENV: raw}
    tasker = SimpleNamespace(set_log_dir=lambda value: calls.append(value) or True)
    assert configure_framework_logging(tasker, environ=environment, warn=warnings.append) is enabled
    assert calls == [path]
    assert bool(warnings) is (raw not in (None, "0", "1"))


@pytest.mark.parametrize("failure", [False, RuntimeError("native log failure")])
def test_logging_failure_never_prevents_agent_startup(failure):
    warnings = []
    def apply(_):
        if isinstance(failure, Exception):
            raise failure
        return failure
    assert configure_framework_logging(SimpleNamespace(set_log_dir=apply),
        environ={FRAMEWORK_LOGGING_ENV: "1"}, warn=warnings.append) is False
    assert warnings


def test_broken_warning_channel_is_optional():
    def warn(_):
        raise BrokenPipeError("closed")
    assert configure_framework_logging(SimpleNamespace(set_log_dir=lambda _: False),
        environ={FRAMEWORK_LOGGING_ENV: "invalid"}, warn=warn) is False


@pytest.mark.parametrize("setting,path", [(None, ""), ("0", ""), ("1", "./debug")])
@pytest.mark.parametrize("failure", [None, False, RuntimeError("native log failure")])
def test_server_applies_logging_policy_before_agent_startup(monkeypatch, setting, path, failure):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "agent"))
    server = importlib.import_module("server")
    if setting is None:
        monkeypatch.delenv("MAABANGDREAM_MAAFW_LOGGING", raising=False)
    else:
        monkeypatch.setenv("MAABANGDREAM_MAAFW_LOGGING", setting)
    calls = []
    def set_log_dir(value):
        calls.append(("log", value))
        if isinstance(failure, Exception):
            raise failure
        return failure is None
    monkeypatch.setattr(server.Tasker, "set_log_dir", set_log_dir)
    monkeypatch.setattr(server, "AgentServer", SimpleNamespace(
        start_up=lambda value: calls.append(("start", value)),
        join=lambda: calls.append(("join",)),
        shut_down=lambda: calls.append(("stop",)),
    ))
    monkeypatch.setattr(server.sys, "argv", ["server.py", "test-socket"])
    monkeypatch.setattr(server, "configure_agent_runtime_flags", lambda _: {
        "native_timing_compensation": True,
        "regional_level_drift_trial": False,
        "cooperative_member_loading_guard_enabled": True,
        "deprecated_cooperative_member_loading_guard_trial_requested": False,
    })
    monkeypatch.setattr(server, "native_wait_jitter_trial_enabled", lambda: False)
    server.main()
    assert calls == [("log", path), ("start", "test-socket"), ("join",), ("stop",)]
