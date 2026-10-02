from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.realtime import native_engine, native_play
from agent.realtime.runtime_flags import native_wait_jitter_trial_enabled


CHART = Path(__file__).resolve().parents[1] / "resource/charts/bestdori/728/expert.json"


def test_wait_jitter_trial_defaults_off_and_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("MAABANGDREAM_NATIVE_WAIT_JITTER_TRIAL", raising=False)
    assert native_wait_jitter_trial_enabled() is False
    monkeypatch.setenv("MAABANGDREAM_NATIVE_WAIT_JITTER_TRIAL", "0")
    assert native_wait_jitter_trial_enabled() is False
    monkeypatch.setenv("MAABANGDREAM_NATIVE_WAIT_JITTER_TRIAL", "1")
    assert native_wait_jitter_trial_enabled() is True


def test_wait_cost_estimator_rejects_spike_but_tracks_sustained_cost():
    estimator = native_play._WaitCostEstimator()
    estimator.observe({"command": "w 8", "cost_ms": 40.741})
    assert estimator.estimate(0.5) == 0.5
    for _ in range(8):
        estimator.observe({"command": "w 8", "cost_ms": 8.5})
    assert estimator.estimate(0.5) < 0.7
    for _ in range(64):
        estimator.observe({"command": "w 8", "cost_ms": 10.0})
    assert estimator.estimate(0.5) == 2.0
    assert estimator.report()["sample_count"] == 64
    estimator.reset()
    assert estimator.estimate(0.0) == 0.0
    assert estimator.report()["sample_count"] == 0


@pytest.mark.skipif(not native_engine.available(), reason="Native 模块未构建")
@pytest.mark.parametrize("enabled", [False, True])
def test_chunk_calibration_keeps_actual_stall_debt_but_filters_future_waits(enabled):
    logs = []
    device = SimpleNamespace(
        request_reset=lambda: True,
        log_records_since=lambda cursor: (len(logs), logs[cursor:]),
    )
    backend = native_play.NativeMinitouchBackend(
        CHART, adb_path="offline", serial="offline", device=device,
        timing_trial_enabled=True,
        wait_jitter_trial_enabled=enabled,
    )
    backend._playback_observation_started = True
    start_ms = 0.0

    for sequence, costs in ((1, [0.5] * 8), (2, [32.741])):
        used_offsets = backend._compiler.offsets
        for index, overhead in enumerate(costs):
            command = "w 100"
            cost = 100.0 + overhead
            event = {"st": start_ms, "et": start_ms + cost, "c": cost, "cmd": command}
            logs.append(("jlog " + json.dumps(event), event["et"] / 1000.0))
            backend._expected_commands.append(native_play._ExpectedCommand(
                command=command,
                chunk_sequence=sequence,
                last_in_chunk=index == len(costs) - 1,
                used_offsets=used_offsets,
            ))
            start_ms += cost
        backend._observe_new_logs()

    assert backend._calibration_chunks == 2
    assert backend._calibration_correction_ms == pytest.approx(4.0 + 32.741 - 0.5)
    assert backend._compiler.offsets.wait_ms == pytest.approx(
        (8 * 0.5 + 1.5) / 9 if enabled else 32.741,
    )
    assert not backend._expected_commands
    report = backend.report()["timing_trial"]["wait_jitter_guard"]
    assert report["enabled"] is enabled
    if enabled:
        assert report["sample_count"] == 9
        assert report["clipped_sample_count"] == 1
        assert report["raw_chunk_wait_ms"] == pytest.approx(32.741)


@pytest.mark.skipif(not native_engine.available(), reason="Native 模块未构建")
def test_filtered_wait_prediction_prevents_sparse_stall_amplification():
    estimator = native_play._WaitCostEstimator()
    for _ in range(8):
        estimator.observe({"command": "w 8", "cost_ms": 8.5})
    estimator.observe({"command": "w 8", "cost_ms": 40.741})
    predicted = estimator.estimate(0.5)
    actions = [{
        "kind": "tap", "lane": 3, "due_s": 0.05 + index * 0.01,
        "contact": -1, "target_x": -1.0, "flick_direction": None,
    } for index in range(30)]

    def actual_drift(wait_ms):
        compiler = native_engine.touch_script_compiler(offsets={"wait_ms": wait_ms})
        compiler.set_wait_cost_recovery_enabled(True)
        # 原始卡顿欠账只注入一次；不能把它从真实时间轴上删掉。
        compiler.add_residual_ms(32.741 - 0.5)
        script = compiler.compile(actions, {"judgement_y": 590.0}, 0, True, 0.5)
        elapsed_ms = 32.741 - 0.5
        drift = []
        for line in script:
            if line.startswith("w "):
                elapsed_ms += int(line.split()[1]) + 0.5
            elif line.startswith("d "):
                drift.append(elapsed_ms - (50 + len(drift) * 10))
        return drift

    baseline = actual_drift(32.741)
    candidate = actual_drift(predicted)
    assert min(baseline) < -150.0
    assert min(candidate) > -5.0
    assert max(candidate) < 35.0
