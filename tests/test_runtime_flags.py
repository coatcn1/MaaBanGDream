from __future__ import annotations

from agent.realtime.runtime_flags import (
    COOPERATIVE_MEMBER_LOADING_GUARD_TRIAL_ARG,
    COOPERATIVE_MEMBER_LOADING_GUARD_TRIAL_ENV,
    DISABLE_NATIVE_TIMING_COMPENSATION_ARG,
    NATIVE_TIMING_TRIAL_ARG,
    configure_agent_runtime_flags,
    cooperative_member_loading_guard_enabled,
    native_timing_compensation_enabled,
)


def test_native_timing_trial_can_be_enabled_by_agent_argument(monkeypatch):
    monkeypatch.delenv("MAABANGDREAM_NATIVE_TIMING_TRIAL", raising=False)

    report = configure_agent_runtime_flags([NATIVE_TIMING_TRIAL_ARG])

    assert report["native_timing_compensation"] is True
    assert native_timing_compensation_enabled() is True


def test_native_timing_compensation_defaults_to_enabled(monkeypatch):
    monkeypatch.delenv("MAABANGDREAM_NATIVE_TIMING_TRIAL", raising=False)

    report = configure_agent_runtime_flags([])

    assert report["native_timing_compensation"] is True
    assert native_timing_compensation_enabled() is True


def test_native_timing_compensation_can_be_disabled_for_diagnosis(monkeypatch):
    monkeypatch.delenv("MAABANGDREAM_NATIVE_TIMING_TRIAL", raising=False)

    report = configure_agent_runtime_flags(
        [DISABLE_NATIVE_TIMING_COMPENSATION_ARG]
    )

    assert report["native_timing_compensation"] is False
    assert native_timing_compensation_enabled() is False


def test_cooperative_loading_guard_argument_survives_missing_inherited_environment(monkeypatch):
    from agent.realtime import runtime_flags

    monkeypatch.setattr(runtime_flags, "_cooperative_member_loading_guard_trial_enabled", False)
    monkeypatch.delenv(COOPERATIVE_MEMBER_LOADING_GUARD_TRIAL_ENV, raising=False)
    report = configure_agent_runtime_flags([COOPERATIVE_MEMBER_LOADING_GUARD_TRIAL_ARG])
    assert report["cooperative_member_loading_guard_trial"] is True
    assert cooperative_member_loading_guard_enabled()

    report = configure_agent_runtime_flags([])
    assert report["cooperative_member_loading_guard_trial"] is False
    assert not cooperative_member_loading_guard_enabled()


def test_cooperative_loading_guard_remains_available_as_process_environment_trial(monkeypatch):
    from agent.realtime import runtime_flags

    monkeypatch.setattr(runtime_flags, "_cooperative_member_loading_guard_trial_enabled", False)
    monkeypatch.setenv(COOPERATIVE_MEMBER_LOADING_GUARD_TRIAL_ENV, "1")
    report = configure_agent_runtime_flags([])
    assert report["cooperative_member_loading_guard_trial"] is True
    assert cooperative_member_loading_guard_enabled()
