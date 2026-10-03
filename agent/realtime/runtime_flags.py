from __future__ import annotations

import os
from collections.abc import Sequence


NATIVE_TIMING_TRIAL_ARG = "--native-timing-trial"
DISABLE_NATIVE_TIMING_COMPENSATION_ARG = "--disable-native-timing-compensation"
NATIVE_TIMING_TRIAL_ENV = "MAABANGDREAM_NATIVE_TIMING_TRIAL"
NATIVE_WAIT_JITTER_TRIAL_ENV = "MAABANGDREAM_NATIVE_WAIT_JITTER_TRIAL"
COOPERATIVE_MEMBER_LOADING_GUARD_TRIAL_ARG = "--cooperative-member-loading-guard-trial"
COOPERATIVE_MEMBER_LOADING_GUARD_TRIAL_ENV = "MAABANGDREAM_COOPERATIVE_MEMBER_LOADING_GUARD_TRIAL"

_native_timing_trial_enabled = False
_cooperative_member_loading_guard_trial_enabled = False


def configure_agent_runtime_flags(arguments: Sequence[str]) -> dict[str, bool]:
    """配置已验收的补偿和本次进程显式启用的开发候选。"""

    global _native_timing_trial_enabled
    global _cooperative_member_loading_guard_trial_enabled
    _native_timing_trial_enabled = not (
        DISABLE_NATIVE_TIMING_COMPENSATION_ARG in arguments
        or os.environ.get(NATIVE_TIMING_TRIAL_ENV) == "0"
    )
    _cooperative_member_loading_guard_trial_enabled = (
        COOPERATIVE_MEMBER_LOADING_GUARD_TRIAL_ARG in arguments
    )
    return {
        "native_timing_compensation": _native_timing_trial_enabled,
        "cooperative_member_loading_guard_trial": cooperative_member_loading_guard_enabled(),
    }


def native_timing_compensation_enabled() -> bool:
    """返回当前 Agent 进程实际启用的 Native timing 补偿状态。"""

    return _native_timing_trial_enabled


def native_wait_jitter_trial_enabled() -> bool:
    """等待成本异常值候选未经过真机验收，仅由本次进程显式启用。"""
    return os.environ.get(NATIVE_WAIT_JITTER_TRIAL_ENV) == "1"


def cooperative_member_loading_guard_enabled() -> bool:
    """加载页保护默认关闭；命令行可跨越 MFA 子进程的环境变量过滤。"""
    return (
        _cooperative_member_loading_guard_trial_enabled
        or os.environ.get(COOPERATIVE_MEMBER_LOADING_GUARD_TRIAL_ENV) == "1"
    )
