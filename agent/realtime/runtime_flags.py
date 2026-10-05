from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from .chart_repository import REGIONAL_LEVEL_DRIFT_TRIAL_ENV, configure_regional_level_drift_trial
from .profile_store import RealtimeProfileStore


NATIVE_TIMING_TRIAL_ARG = "--native-timing-trial"
DISABLE_NATIVE_TIMING_COMPENSATION_ARG = "--disable-native-timing-compensation"
NATIVE_TIMING_TRIAL_ENV = "MAABANGDREAM_NATIVE_TIMING_TRIAL"
NATIVE_WAIT_JITTER_TRIAL_ENV = "MAABANGDREAM_NATIVE_WAIT_JITTER_TRIAL"
REGIONAL_LEVEL_DRIFT_TRIAL_ARG = "--regional-level-drift-trial"
DISABLE_REGIONAL_LEVEL_DRIFT_ARG = "--disable-regional-level-drift"
COOPERATIVE_MEMBER_LOADING_GUARD_TRIAL_ARG = "--cooperative-member-loading-guard-trial"
COOPERATIVE_MEMBER_LOADING_GUARD_TRIAL_ENV = "MAABANGDREAM_COOPERATIVE_MEMBER_LOADING_GUARD_TRIAL"
# 旧入口只为兼容启动脚本保留，保护状态以持久化运行选项为准。

_native_timing_trial_enabled = False


def configure_agent_runtime_flags(arguments: Sequence[str]) -> dict[str, bool]:
    """配置已验收的补偿和本次进程显式启用的开发候选。"""

    global _native_timing_trial_enabled
    _native_timing_trial_enabled = not (
        DISABLE_NATIVE_TIMING_COMPENSATION_ARG in arguments
        or os.environ.get(NATIVE_TIMING_TRIAL_ENV) == "0"
    )
    # 保留旧启用入口；关闭参数始终优先，普通启动默认使用已验收的身份规则。
    regional_level_drift_trial = (
        DISABLE_REGIONAL_LEVEL_DRIFT_ARG not in arguments
        and (
            REGIONAL_LEVEL_DRIFT_TRIAL_ARG in arguments
            or os.environ.get(REGIONAL_LEVEL_DRIFT_TRIAL_ENV) != "0"
        )
    )
    configure_regional_level_drift_trial(regional_level_drift_trial)
    return {
        "native_timing_compensation": _native_timing_trial_enabled,
        "regional_level_drift_trial": regional_level_drift_trial,
        "cooperative_member_loading_guard_enabled": cooperative_member_loading_guard_enabled(),
        "deprecated_cooperative_member_loading_guard_trial_requested": (
            COOPERATIVE_MEMBER_LOADING_GUARD_TRIAL_ARG in arguments
            or os.environ.get(COOPERATIVE_MEMBER_LOADING_GUARD_TRIAL_ENV) == "1"
        ),
    }


def native_timing_compensation_enabled() -> bool:
    """返回当前 Agent 进程实际启用的 Native timing 补偿状态。"""

    return _native_timing_trial_enabled


def native_wait_jitter_trial_enabled() -> bool:
    """等待成本异常值候选未经过真机验收，仅由本次进程显式启用。"""
    return os.environ.get(NATIVE_WAIT_JITTER_TRIAL_ENV) == "1"


def cooperative_member_loading_guard_enabled() -> bool:
    """启动诊断读取持久选项；旧 trial 参数和环境变量已废弃，不覆盖界面。"""
    return RealtimeProfileStore(
        Path(__file__).resolve().parents[2] / "profiles"
    ).runtime_options()["cooperative_member_loading_guard_enabled"]
