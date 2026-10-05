from __future__ import annotations

import pytest

from agent.realtime import chart_repository
from agent.realtime.chart_repository import LocalChartRepository
from agent.realtime.runtime_flags import (
    DISABLE_REGIONAL_LEVEL_DRIFT_ARG,
    REGIONAL_LEVEL_DRIFT_TRIAL_ARG,
)

from agent.realtime import runtime_flags
from agent.realtime.profile_store import RealtimeProfileStore
from agent.realtime.runtime_flags import (
    COOPERATIVE_MEMBER_LOADING_GUARD_TRIAL_ARG,
    COOPERATIVE_MEMBER_LOADING_GUARD_TRIAL_ENV,
    DISABLE_NATIVE_TIMING_COMPENSATION_ARG,
    NATIVE_TIMING_TRIAL_ARG,
    configure_agent_runtime_flags,
    cooperative_member_loading_guard_enabled,
    native_timing_compensation_enabled,
)


@pytest.fixture(autouse=True)
def isolated_runtime_store(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_flags, "_native_timing_trial_enabled", runtime_flags._native_timing_trial_enabled)
    monkeypatch.setattr(chart_repository, "_regional_level_drift_trial_configured", None)
    monkeypatch.delenv(chart_repository.REGIONAL_LEVEL_DRIFT_TRIAL_ENV, raising=False)
    store = RealtimeProfileStore(tmp_path)
    monkeypatch.setattr(runtime_flags, "RealtimeProfileStore", lambda root: store)
    return store


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


def test_cooperative_loading_guard_defaults_enabled_without_trial(monkeypatch):
    monkeypatch.delenv(COOPERATIVE_MEMBER_LOADING_GUARD_TRIAL_ENV, raising=False)
    report = configure_agent_runtime_flags([])
    assert report["cooperative_member_loading_guard_enabled"] is True
    assert report["deprecated_cooperative_member_loading_guard_trial_requested"] is False
    assert cooperative_member_loading_guard_enabled()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("argument", [False, True])
@pytest.mark.parametrize("environment", [False, True])
def test_deprecated_loading_guard_trial_never_overrides_persisted_value(
    monkeypatch, isolated_runtime_store, enabled, argument, environment,
):
    isolated_runtime_store.update_runtime_options({"cooperative_member_loading_guard_enabled": enabled})
    monkeypatch.setenv(COOPERATIVE_MEMBER_LOADING_GUARD_TRIAL_ENV, "1" if environment else "0")
    report = configure_agent_runtime_flags([COOPERATIVE_MEMBER_LOADING_GUARD_TRIAL_ARG] if argument else [])
    assert report["cooperative_member_loading_guard_enabled"] is enabled
    assert report["deprecated_cooperative_member_loading_guard_trial_requested"] is (argument or environment)
    assert "cooperative_member_loading_guard_trial" not in report
    assert cooperative_member_loading_guard_enabled() is enabled


def test_regional_level_drift_defaults_enabled_without_trial(tmp_path):
    report = configure_agent_runtime_flags([])
    assert report["regional_level_drift_trial"] is True
    assert LocalChartRepository(tmp_path).regional_level_drift_enabled is True
    assert LocalChartRepository(tmp_path, regional_level_drift_enabled=False).regional_level_drift_enabled is False


@pytest.mark.parametrize("environment,arguments,enabled", [
    ("0", [], False),
    ("1", [], True),
    ("0", [REGIONAL_LEVEL_DRIFT_TRIAL_ARG], True),
    ("1", [DISABLE_REGIONAL_LEVEL_DRIFT_ARG], False),
    ("1", [REGIONAL_LEVEL_DRIFT_TRIAL_ARG, DISABLE_REGIONAL_LEVEL_DRIFT_ARG], False),
])
def test_regional_level_drift_explicit_disable_and_legacy_enable(
    monkeypatch, tmp_path, environment, arguments, enabled,
):
    monkeypatch.setenv(chart_repository.REGIONAL_LEVEL_DRIFT_TRIAL_ENV, environment)
    report = configure_agent_runtime_flags(arguments)
    assert report["regional_level_drift_trial"] is enabled
    repository = LocalChartRepository(tmp_path)
    assert repository.regional_level_drift_enabled is enabled
    monkeypatch.setenv(chart_repository.REGIONAL_LEVEL_DRIFT_TRIAL_ENV, "0" if enabled else "1")
    assert LocalChartRepository(tmp_path).regional_level_drift_enabled is enabled
    assert repository.regional_level_drift_enabled is enabled
