"""Native 滚动路径真实墙钟虚拟设备 smoke。"""

from __future__ import annotations

import pytest

from scripts import stress_native_realtime as stress


def test_virtual_device_wall_clock_smoke() -> None:
    try:
        native = stress.load_native_module()
    except RuntimeError as exc:
        pytest.skip(str(exc))

    config = stress.StressConfig(
        duration_s=1.2,
        mode="idle",
        action_interval_s=0.060,
        startup_lead_s=0.100,
        p95_limit_ms=50.0,
        max_drift_limit_ms=100.0,
        cancel_limit_ms=500.0,
    )
    result = stress.run_scenario(config, native_module=native)

    assert result["evidence_scope"] == "virtual_device_only"
    assert result["android_acceptance"] is False
    assert result["passed"] is True, result
    assert result["metrics"]["executed_actions"] == result["metrics"][
        "planned_actions"
    ]
    assert result["metrics"]["queue_underflows"] == 0
    assert result["metrics"]["active_contacts_at_end"] == 0
    assert result["cancel_probe"]["released_contacts"] >= 1
    for command in ("c", "w", "d", "m", "u"):
        assert result["protocol"]["command_counts"][command] > 0
