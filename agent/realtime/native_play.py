"""C++ Native 演奏后端：整曲 minitouch 定时脚本一次下发。

分工：
- 谱面动作编译、w(ait) 计时、分类型延迟补偿、jlog 解析与校准全部在 C++；
- 本模块只做设备编排、触发时机与生命周期管理（Python 编排层）；
- 任何失败都回退 Legacy（上层在 native_realtime_enabled 时仍要求
  chart_prediction 可用，本模块构造失败会直接抛出让外层回退）。
"""

from __future__ import annotations

import time
import threading
from pathlib import Path
from typing import Any, Callable

from . import native_engine
from .native_minitouch import NativeMinitouchDevice


LANE_CENTERS = (190, 340, 490, 640, 790, 940, 1090)


class NativeMinitouchBackend:
    """在谱面锁定后接管触控：设备端按谱面时间演奏整曲。"""

    def __init__(
        self,
        chart_path: str | Path,
        *,
        adb_path: str,
        serial: str,
        judgement_y: float = 565.0,
        lane_centers: tuple[float, ...] = LANE_CENTERS,
        press_bias_ms: int = 0,
        max_wait_ms: int = 250,
        clock: Callable[[], float] = time.perf_counter,
        initial_offsets: dict[str, float] | None = None,
    ) -> None:
        if not native_engine.available():
            raise RuntimeError(
                "Native 模块不可用："
                f"{native_engine.unavailable_reason() or 'unknown'}"
            )
        # C++ 侧编译谱面与动作流（与 Python 参考实现差分一致）。
        self._timeline = native_engine.compile_chart(chart_path)
        self._actions: list[dict[str, object]] = list(
            self._timeline.compile_actions({})
        )
        if not self._actions:
            raise RuntimeError("谱面没有可编译的动作")
        self._first_due_s = min(
            float(action["due_s"]) for action in self._actions
        )
        self._end_s = max(
            float(action["due_s"]) for action in self._actions
        )
        self._clock = clock
        self._config = {
            "judgement_y": float(judgement_y),
            "press_bias_ms": int(press_bias_ms),
            "max_wait_ms": int(max_wait_ms),
            "lane_centers": list(lane_centers),
        }
        self._compiler = native_engine.touch_script_compiler(
            offsets=initial_offsets
        )
        self._calibrator = native_engine.latency_calibrator()
        self._device = NativeMinitouchDevice(adb_path, serial)
        self._state = "idle"  # idle -> ready -> triggered -> finished
        self._triggered = False
        self._first_read_delay_s = 0.004
        self._song_offset_s: float | None = None
        self._trigger_deadline_s: float | None = None
        self._finished_at_s: float | None = None
        self._seen_logs: set[str] = set()
        self._probe_published_at: float | None = None
        self._publish_error: str | None = None
        self._device_thread: threading.Thread | None = None
        self._device_error: str | None = None
        self._last_calib_log_at = 0.0
        self._logged_offset_ms: float | None = None
        self._logged_lead_wait = False

    @property
    def active(self) -> bool:
        return self._state == "triggered"

    @property
    def takeover(self) -> bool:
        """触发后保持接管直至结束：引擎不再派发谱面触控。"""
        # 只有真正下发过整曲脚本才接管；启动/发布失败必须回退 Legacy。
        return self._triggered

    @property
    def finished(self) -> bool:
        return self._state == "finished"

    @property
    def song_offset_s(self) -> float | None:
        return self._song_offset_s

    @property
    def first_read_delay_ms(self) -> float:
        return self._first_read_delay_s * 1000.0

    @property
    def calibrated_offsets(self) -> dict[str, float]:
        offsets = self._calibrator.offsets
        return {
            "down_ms": offsets.down_ms,
            "up_ms": offsets.up_ms,
            "move_ms": offsets.move_ms,
            "wait_ms": offsets.wait_ms,
            "interval_ms": offsets.interval_ms,
        }

    def _observe_new_logs(self) -> None:
        for line in self._device.recent_logs:
            if line in self._seen_logs or not line.startswith("jlog "):
                continue
            self._seen_logs.add(line)
            event = native_engine.parse_minitouch_log(line)
            if event is None:
                continue
            if self._probe_published_at is not None and event["command"].startswith("w "):
                nominal = float(event["command"].split()[1])
                if nominal <= 1:
                    # w 0 探测回读：往返延迟的一半近似首读延迟。
                    round_trip = (
                        self._clock() - self._probe_published_at
                    )
                    estimate = min(0.015, max(0.001, round_trip / 2))
                    self._first_read_delay_s = estimate
                    self._probe_published_at = None
            elif (
                self._triggered
                and not self._logged_lead_wait
                and event["command"].startswith("w ")
            ):
                nominal = float(event["command"].split()[1])
                if nominal >= 100:
                    self._logged_lead_wait = True
                    print(
                        "NativeMinitouch lead_wait "
                        f"nominal={nominal:.0f}ms "
                        f"cost={event['cost_ms']:.3f}ms",
                        flush=True,
                    )
            self._calibrator.observe(event)

    def _start_device(self) -> None:
        try:
            self._device.start()
            # 首读延迟探测：发一条 w 0，回读 jlog 估计 publish->设备读取的
            # 单程延迟，用它对齐整曲脚本的绝对起点。
            self._probe_published_at = self._clock()
            self._device.publish("c\nw 0\nc\n")
            print(
                "NativeMinitouch device_ready "
                f"max={self._device.max_x}x{self._device.max_y} "
                f"contacts={self._device.max_contacts}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 - 失败回退 Legacy
            self._device_error = f"{type(exc).__name__}: {exc}"
            self._device.stop()
            print(
                "NativeMinitouch device_start_failed "
                f"reason={self._device_error}",
                flush=True,
            )

    def frame(self, now: float, planner: Any) -> None:
        """每帧由引擎调用：驱动设备启动、谱面触发与 jlog 归集。"""
        self._observe_new_logs()
        if self._state == "idle":
            if self._device_thread is None:
                # 设备启动可能阻塞数秒（push/握手），绝不能在引擎热循环里
                # 同步执行，否则会吞掉整个前奏与谱面锁定窗口。
                self._device_thread = threading.Thread(
                    target=self._start_device, daemon=True
                )
                self._device_thread.start()
            if self._device_thread.is_alive():
                return
            if self._device_error is not None:
                self._publish_error = self._device_error
                self._state = "finished"
                print(
                    f"NativeMinitouch disabled reason={self._device_error}",
                    flush=True,
                )
                return
            if not self._device.connected:
                self._publish_error = "minitouch 未连接"
                self._state = "finished"
                return
            self._state = "ready"
            return
        if self._state == "finished":
            return
        if self._state == "ready":
            if not bool(getattr(planner, "chart_calibrated", False)):
                if now - self._last_calib_log_at > 3.0:
                    self._last_calib_log_at = now
                    print(
                        "NativeMinitouch waiting_calibration "
                        f"chart_calibrated=False "
                        f"disable_reason={getattr(planner, 'chart_disable_reason', None)}",
                        flush=True,
                    )
                return
            offset_ms = float(
                getattr(planner, "chart_song_offset_ms", None) or 0.0
            )
            self._song_offset_s = offset_ms / 1000.0
            # 时间域换算：引擎 now 是 perf_counter 绝对值，而 C++ 编译器把
            # due_s - song_offset_s 视为“相对锚点的引擎秒”。统一换算到相对
            # 域，否则绝对时间戳会与谱面秒相减，把所有动作都判成已过期。
            song_now = getattr(planner, "song_time_s")(now)
            if song_now is None:
                return
            relative_now = song_now - self._song_offset_s
            # 锁定可能晚于首音：只编译尚未来得及演奏的动作，已过去的
            # 判定交给 Legacy 已派发的输入，避免脚本补按造成双按。
            cutoff = relative_now + 0.05
            future = [
                action
                for action in self._actions
                if float(action["due_s"]) - self._song_offset_s > cutoff
            ]
            if not future:
                self._state = "finished"
                print(
                    "NativeMinitouch no_future_actions "
                    f"offset_ms={offset_ms:.3f} "
                    f"song_now={song_now:.3f}",
                    flush=True,
                )
                return
            first_due = (
                float(future[0]["due_s"]) - self._song_offset_s
            )
            if self._logged_offset_ms != offset_ms:
                self._logged_offset_ms = offset_ms
                print(
                    "NativeMinitouch calibrated "
                    f"offset_ms={offset_ms:.3f} "
                    f"future={len(future)}/{len(self._actions)} "
                    f"first_due_s={first_due:.3f} "
                    f"relative_now={relative_now:.3f} "
                    f"lead_ms={(first_due - relative_now) * 1000:.1f}",
                    flush=True,
                )
            # 触发窗口：临近首音时立即整曲下发；脚本内的首段 w 吸收剩余
            # 等待，设备端执行不受 PC 帧率影响。
            lead = first_due - relative_now
            if lead > 1.2:
                if now - self._last_calib_log_at > 2.0:
                    self._last_calib_log_at = now
                    print(
                        "NativeMinitouch armed "
                        f"lead_ms={lead * 1000:.1f} "
                        f"song_offset_ms={offset_ms:.3f}",
                        flush=True,
                    )
                return
            start_engine_time = relative_now + self._first_read_delay_s
            try:
                script = self._compiler.compile(
                    future,
                    dict(self._config, song_offset_s=self._song_offset_s),
                    float(start_engine_time),
                )
                text = "".join(script)
                self._device.publish(text)
            except Exception as exc:  # noqa: BLE001 - 失败回退 Legacy
                self._publish_error = f"{type(exc).__name__}: {exc}"
                self._state = "finished"
                print(
                    "NativeMinitouch trigger_failed "
                    f"reason={self._publish_error}",
                    flush=True,
                )
                return
            self._triggered = True
            self._state = "triggered"
            self._finished_at_s = (
                max(float(action["due_s"]) for action in future)
                - self._song_offset_s + 1.5
            )
            print(
                "NativeMinitouch triggered "
                f"song_offset_ms={offset_ms:.3f} "
                f"lead_ms={(lead * 1000):.1f} "
                f"first_read_delay_ms={self.first_read_delay_ms:.3f} "
                f"actions={len(future)}/{len(self._actions)}",
                flush=True,
            )
            return
        if self._state == "triggered":
            if self._finished_at_s is not None:
                song_now = getattr(planner, "song_time_s")(now)
                if (
                    song_now is not None
                    and song_now - self._song_offset_s >= self._finished_at_s
                ):
                    self._state = "finished"
            return

    def stop(self) -> None:
        """停止/异常/终态清理：r 释放触点并拆除设备端进程。"""
        offsets = self._calibrator.offsets
        print(
            "NativeMinitouch session_stats "
            f"triggered={self._triggered} "
            f"jlog_events={self._calibrator.event_count} "
            f"down={offsets.down_ms:.3f} up={offsets.up_ms:.3f} "
            f"move={offsets.move_ms:.3f} wait={offsets.wait_ms:.3f} "
            f"interval={offsets.interval_ms:.3f} "
            f"first_read_delay_ms={self.first_read_delay_ms:.3f} "
            f"publish_error={self._publish_error}",
            flush=True,
        )
        try:
            self._device.stop()
        finally:
            self._state = "finished"
