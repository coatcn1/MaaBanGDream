"""启动前应用 MFA 传入的文件日志快照，不在任务运行中切换底层日志。"""

from __future__ import annotations

import os


FRAMEWORK_LOGGING_ENV = "MAABANGDREAM_MAAFW_LOGGING"


def _warn(message, warn):
    try:
        if warn is None:
            print(f"warn: {message}", flush=True)
        else:
            warn(message)
    except Exception:
        # 日志通道本身失效时，不能阻止 Agent 启动和必要的输入清理。
        pass


def configure_framework_logging(tasker, *, environ=None, warn=None):
    environment = os.environ if environ is None else environ
    raw = environment.get(FRAMEWORK_LOGGING_ENV)
    enabled = raw == "1"
    if raw not in (None, "0", "1"):
        _warn("MaaFramework 文件日志配置无效，按关闭处理", warn)
    try:
        # 5.10.2 已开启的日志流不能由空路径可靠关闭，必须早于 start_up 应用。
        accepted = tasker.set_log_dir("./debug" if enabled else "")
    except Exception as exc:
        _warn(f"MaaFramework 文件日志初始化失败：{type(exc).__name__}，继续启动", warn)
        return False
    if not accepted:
        _warn("MaaFramework 未接受文件日志设置，继续启动", warn)
        return False
    return enabled
