from __future__ import annotations

from agent.realtime.runtime_flags import (
    DISABLE_NATIVE_TIMING_COMPENSATION_ARG,
    NATIVE_TIMING_TRIAL_ARG,
    configure_agent_runtime_flags,
    native_timing_compensation_enabled,
)


def test_native_timing_trial_can_be_enabled_by_agent_argument(monkeypatch):
    monkeypatch.delenv("MAABANGDREAM_NATIVE_TIMING_TRIAL", raising=False)

    report = configure_agent_runtime_flags([NATIVE_TIMING_TRIAL_ARG])

    assert report == {"native_timing_compensation": True}
    assert native_timing_compensation_enabled() is True


def test_native_timing_compensation_defaults_to_enabled(monkeypatch):
    monkeypatch.delenv("MAABANGDREAM_NATIVE_TIMING_TRIAL", raising=False)

    report = configure_agent_runtime_flags([])

    assert report == {"native_timing_compensation": True}
    assert native_timing_compensation_enabled() is True


def test_native_timing_compensation_can_be_disabled_for_diagnosis(monkeypatch):
    monkeypatch.delenv("MAABANGDREAM_NATIVE_TIMING_TRIAL", raising=False)

    report = configure_agent_runtime_flags(
        [DISABLE_NATIVE_TIMING_COMPENSATION_ARG]
    )

    assert report == {"native_timing_compensation": False}
    assert native_timing_compensation_enabled() is False
