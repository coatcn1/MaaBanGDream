"""Native Realtime Engine V2 的 Python 适配层。

边界（见交接文档）：
- Native 默认关闭；一旦显式开启，任何 import / 编译 / 设备失败都 fail-closed；
- 谱面编译、动作调度、相位同步与定时 minitouch 脚本编译都在 C++ 完成；
- 脚本的 TCP 发布端也在 C++（MinitouchClient），Python 只做设备进程编排；
- 本模块不接管用户默认真实演奏，只在显式开启且 Native 可用时被上层选择。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


_NATIVE_DIR = Path(__file__).resolve().parent / "native"
_module: Any | None = None
_import_error: str | None = None

LANE_CENTERS = (190, 340, 490, 640, 790, 940, 1090)

SYNC_CONFIG_DEFAULTS = {
    "match_tol_s": 0.15,
    "max_mad_s": 0.05,
    "min_margin_s": 0.20,
    "min_samples": 6,
    "min_samples_with_anchor": 2,
    "min_margin_samples": 2,
    "min_lanes": 2,
    "prelude_grace_s": 2.0,
    "anchor_default_uncertainty_s": 0.6,
    "min_offset_s": -30.0,
    "max_offset_s": 10.0,
    "offset_step_s": 0.005,
    "sync_chart_window_s": 30.0,
}


def _load() -> bool:
    """惰性导入 .pyd；失败只记录原因，绝不在热路径抛异常。"""
    global _module, _import_error
    if _module is not None:
        return True
    try:
        if str(_NATIVE_DIR) not in sys.path:
            sys.path.insert(0, str(_NATIVE_DIR))
        import maabangdream_realtime  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - 由显式选择点决定是否失败
        _import_error = f"{type(exc).__name__}: {exc}"
        return False
    _module = maabangdream_realtime
    return True


def available() -> bool:
    """Native 模块是否可在当前固定 CPython 环境导入。"""
    return _load()


def unavailable_reason() -> str | None:
    """上一次导入失败的原因（仅诊断）。"""
    return _import_error


def native_version() -> str | None:
    if not _load():
        return None
    return str(_module.version())


def compile_chart(chart_path: str | Path) -> Any:
    """用 Native 编译谱面时间轴；失败由本局选择点按独占策略处理。"""
    if not _load():
        raise RuntimeError(
            f"Native 模块不可用：{_import_error or 'unknown error'}"
        )
    return _module.ChartTimeline.from_file(str(chart_path))


def compile_touch_script(
    actions: list[dict[str, object]],
    *,
    song_offset_s: float = 0.0,
    press_bias_ms: int = 0,
    offsets: dict[str, float] | None = None,
    start_engine_time: float = 0.0,
) -> list[str]:
    """用 C++ 把整曲动作编译成定时 minitouch 脚本。

    时间敏感的脚本生成、分类型延迟补偿与取整损失补偿全部在 C++ 完成；
    每个 w 前自动插入 c 冲刷触点，返回的每一行都是 minitouch v1 命令
    （w/d/m/u/c）。
    """
    if not _load():
        raise RuntimeError(
            f"Native 模块不可用：{_import_error or 'unknown error'}"
        )
    latency = offsets or {}
    native_offsets = _module.TouchLatencyOffsets(
        float(latency.get("down_ms", 0.0)),
        float(latency.get("up_ms", 0.0)),
        float(latency.get("move_ms", 0.0)),
        float(latency.get("wait_ms", 0.0)),
        float(latency.get("interval_ms", 0.0)),
    )
    config = {
        "song_offset_s": float(song_offset_s),
        "press_bias_ms": int(press_bias_ms),
        "lane_centers": list(LANE_CENTERS),
    }
    compiler = _module.TouchScriptCompiler(native_offsets)
    return list(
        compiler.compile(list(actions), config, float(start_engine_time))
    )


def touch_script_compiler(
    offsets: dict[str, float] | None = None,
) -> Any:
    """构造带跨切片状态（残差/取整损失）的 C++ 脚本编译器。"""
    if not _load():
        raise RuntimeError(
            f"Native 模块不可用：{_import_error or 'unknown error'}"
        )
    latency = offsets or {}
    native_offsets = _module.TouchLatencyOffsets(
        float(latency.get("down_ms", 0.0)),
        float(latency.get("up_ms", 0.0)),
        float(latency.get("move_ms", 0.0)),
        float(latency.get("wait_ms", 0.0)),
        float(latency.get("interval_ms", 0.0)),
    )
    return _module.TouchScriptCompiler(native_offsets)


def minitouch_client() -> Any:
    """C++ 实现的 minitouch 脚本 TCP 发布端（传输不参与时序）。"""
    if not _load():
        raise RuntimeError(
            f"Native 模块不可用：{_import_error or 'unknown error'}"
        )
    return _module.MinitouchClient()


def parse_minitouch_log(line: str) -> dict[str, object] | None:
    """解析设备端 jlog 回读行；握手行返回 None。"""
    if not _load():
        raise RuntimeError(
            f"Native 模块不可用：{_import_error or 'unknown error'}"
        )
    return _module.parse_minitouch_log(line)


def latency_calibrator() -> Any:
    """C++ 分类型延迟统计器：消费 jlog，产出下一切片的分类型 offset。"""
    if not _load():
        raise RuntimeError(
            f"Native 模块不可用：{_import_error or 'unknown error'}"
        )
    return _module.LatencyCalibrator()


def playback_session(
    *,
    publish: Any,
    request_reset: Any = None,
    fallback_stop: Any = None,
    clock: Any = None,
    config: dict[str, object] | None = None,
) -> Any:
    """构造 C++ 滚动分块演奏会话。"""
    if not _load():
        raise RuntimeError(
            f"Native 模块不可用：{_import_error or 'unknown error'}"
        )
    return _module.PlaybackSession(
        publish=publish,
        request_reset=request_reset,
        fallback_stop=fallback_stop,
        clock=clock,
        config=dict(config or {}),
    )


class NativeRealtimeEngine:
    """离线诊断外观：谱面编译、逐 tick 调度与视觉相位同步。"""

    def __init__(
        self,
        chart: Any,
        *,
        press_bias_ms: int = 0,
    ) -> None:
        if not _load():
            raise RuntimeError(
                f"Native 模块不可用：{_import_error or 'unknown error'}"
            )
        if isinstance(chart, _module.ChartTimeline):
            self._timeline = chart
        elif isinstance(chart, (str, Path)):
            self._timeline = _module.ChartTimeline.from_file(str(chart))
        else:
            raise TypeError("chart 必须是谱面路径或 Native ChartTimeline")
        self._actions = self._timeline.compile_actions({})
        self._scheduler: Any | None = None

    @property
    def actions(self) -> list[dict[str, object]]:
        return list(self._actions)

    def start(self, *, song_offset_s: float, press_bias_ms: int = 0) -> None:
        """以锁定的 song_offset 启动调度；offset 未锁定时禁止调用。"""
        config = {
            "song_offset_s": float(song_offset_s),
            "press_bias_ms": int(press_bias_ms),
            "lane_centers": list(LANE_CENTERS),
        }
        self._scheduler = _module.ActionScheduler(self._actions, config)

    def tick(self, now_s: float) -> list[dict[str, object]]:
        if self._scheduler is None:
            raise RuntimeError("NativeRealtimeEngine.start() 必须先调用")
        return list(self._scheduler.tick(float(now_s)))

    def stop(self) -> list[dict[str, object]]:
        """fail-closed：释放全部仍在生命周期内的触点。"""
        if self._scheduler is None:
            return []
        return list(self._scheduler.stop())

    def stats(self) -> dict[str, object]:
        if self._scheduler is None:
            return {}
        return dict(self._scheduler.stats())

    def synchronizer(
        self,
        *,
        sync_config: dict[str, object] | None = None,
    ) -> Any:
        config = dict(SYNC_CONFIG_DEFAULTS)
        if sync_config:
            config.update(sync_config)
        return _module.SongClockSynchronizer(self._timeline, config)


def resolve_engine(
    options: dict[str, Any] | None,
    *,
    chart_available: bool,
) -> str:
    """上层引擎决策：默认 Legacy；显式 Native 选择必须 fail-closed。"""
    settings = options or {}
    if not bool(settings.get("native_realtime_enabled", False)):
        return "legacy"
    if not chart_available:
        raise RuntimeError("Native 已显式启用，但未找到可靠的本地谱面")
    if not available():
        raise RuntimeError(
            "Native 已显式启用，但模块不可用："
            f"{unavailable_reason() or 'unknown error'}"
        )
    return "native"
