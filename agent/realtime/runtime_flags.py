from __future__ import annotations

import os
from collections.abc import Sequence


NATIVE_TIMING_TRIAL_ARG = "--native-timing-trial"
DISABLE_NATIVE_TIMING_COMPENSATION_ARG = "--disable-native-timing-compensation"
NATIVE_TIMING_TRIAL_ENV = "MAABANGDREAM_NATIVE_TIMING_TRIAL"

_native_timing_trial_enabled = False


def configure_agent_runtime_flags(arguments: Sequence[str]) -> dict[str, bool]:
    """启用已验收的 Native 补偿，并保留显式关闭的诊断入口。"""

    global _native_timing_trial_enabled
    _native_timing_trial_enabled = not (
        DISABLE_NATIVE_TIMING_COMPENSATION_ARG in arguments
        or os.environ.get(NATIVE_TIMING_TRIAL_ENV) == "0"
    )
    return {"native_timing_compensation": _native_timing_trial_enabled}


def native_timing_compensation_enabled() -> bool:
    """返回当前 Agent 进程实际启用的 Native timing 补偿状态。"""

    return _native_timing_trial_enabled
