from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from .frame_observer_core import (
    LatestFrameObserver,
    ObservationStats,
    write_observation_report,
)


def _parameters(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("RealtimeObserve 参数必须是 JSON 对象")
        return value
    return {}


@AgentServer.custom_action("RealtimeObserve")
class RealtimeObserve(CustomAction):
    """Short, screenshot-only controller benchmark. It never sends input."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        params = _parameters(argv.custom_action_param)
        observer = LatestFrameObserver()
        controller = context.tasker.controller
        started_at = datetime.now(timezone.utc)
        stats = observer.run(
            lambda: controller.post_screencap().wait().get(),
            lambda: context.tasker.stopping,
            duration_seconds=float(params.get("duration_seconds", 5)),
            frame_timeout_ms=int(params.get("frame_timeout_ms", 150)),
        )
        report = write_observation_report(
            Path(__file__).resolve().parents[2]
            / "debug" / "screencap-benchmarks",
            stats,
            method_label=str(params.get("method_label", "controller-configured")),
            started_at=started_at,
        )
        print(
            "RealtimeObserve "
            f"frames={stats.frames} elapsed={stats.elapsed_seconds:.3f}s "
            f"fps={stats.effective_fps:.2f} "
            f"capture_mean={stats.capture_mean_ms:.1f}ms "
            f"capture_p50={stats.capture_p50_ms:.1f}ms "
            f"capture_p95={stats.capture_p95_ms:.1f}ms "
            f"max_capture={stats.maximum_capture_ms:.1f}ms "
            f"over_100ms={stats.over_100ms_frames} "
            f"over_150ms={stats.over_150ms_frames} "
            f"timeouts={stats.timed_out_frames} invalid={stats.invalid_frames} "
            f"stopped={stats.stopped} report={report.name}",
            flush=True,
        )
        return not stats.stopped and stats.frames > 0
