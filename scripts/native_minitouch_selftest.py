"""Native minitouch 设备链路自检（不产生真实触控）。

只发布 `c`/`w` 计时命令，验证：push/启动/forward/握手、设备端毫秒等待、
jlog 回读与 LatencyCalibrator 统计，全程不触碰屏幕。

用法：
  python scripts/native_minitouch_selftest.py [adb路径] [设备序列号]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent"))

from realtime import native_engine, native_minitouch  # noqa: E402


def main() -> int:
    adb = sys.argv[1] if len(sys.argv) > 1 else r"E:\leidian\mrfz\adb.exe"
    serial = sys.argv[2] if len(sys.argv) > 2 else "emulator-7554"

    if not native_engine.available():
        print(f"Native 模块不可用：{native_engine.unavailable_reason()}")
        return 1

    device = native_minitouch.NativeMinitouchDevice(adb, serial)
    device.start()
    try:
        print(
            f"handshake: max_contacts={device.max_contacts} "
            f"max_x={device.max_x} max_y={device.max_y}"
        )
        if device.max_x <= 0 or device.max_y <= 0:
            print("握手分辨率非法")
            return 1
        device_line = next(
            (line for line in device.recent_stderr if "touch device" in line),
            "",
        )
        print(f"device: {device_line or 'stderr 未捕获设备行'}")

        calibrator = native_engine.latency_calibrator()
        device.publish("c\nw 200\nc\nw 150\nc\n")
        seen: set[str] = set()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            lines = device.recent_logs
            pending = [line for line in lines if line.startswith("jlog ")]
            for line in pending:
                if line in seen:
                    continue
                seen.add(line)
                event = native_engine.parse_minitouch_log(line)
                if event is not None:
                    calibrator.observe(event)
            if len(seen) >= 5:
                break
            time.sleep(0.05)

        logs = [line for line in device.recent_logs if line.startswith("jlog ")]
        print(f"jlog 行数={len(logs)}")
        waits = [
            native_engine.parse_minitouch_log(line)
            for line in logs
        ]
        waits = [event for event in waits if event and event["command"].startswith("w ")]
        if len(waits) != 2:
            print(f"期望 2 条 w jlog，实际 {len(waits)}：{logs}")
            return 1
        for event in waits:
            nominal = int(event["command"].split()[1])
            overshoot = event["cost_ms"] - nominal
            print(
                f"w {nominal}ms 实际 {event['cost_ms']:.3f}ms "
                f"超出 {overshoot:+.3f}ms"
            )
        offsets = calibrator.offsets
        print(
            "calibrated offsets: "
            f"down={offsets.down_ms:.3f} up={offsets.up_ms:.3f} "
            f"move={offsets.move_ms:.3f} wait={offsets.wait_ms:.3f} "
            f"interval={offsets.interval_ms:.3f}"
        )
        if calibrator.event_count < 2:
            print("calibrator 样本不足")
            return 1
        print("OK")
        return 0
    finally:
        device.stop()


if __name__ == "__main__":
    raise SystemExit(main())
