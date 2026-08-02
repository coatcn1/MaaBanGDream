from __future__ import annotations

import json
import time
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import cv2

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    from ..task_reporting import record_failure_reason
except ImportError:
    from task_reporting import record_failure_reason

from .life_monitor import LifeDetector
from .profile_play_action import RealtimeProfilePlay, resolve_profile_for_settings_gate


class ListenerDiagnosticCapture:
    """Persist the last passive-listener frame when the task stops or fails."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._latest = None
        self._saved = False

    def observe(self, image) -> None:
        # Screenshots returned by Maa are immutable for this action's purposes;
        # retaining the most recent reference avoids a full-frame copy every 100ms.
        self._latest = image

    def save(self, reason: str) -> Path | None:
        if self._saved or self._latest is None:
            return None
        self._saved = True
        output = self._root / (
            "listener-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        )
        output.mkdir(parents=True, exist_ok=False)
        image_path = output / "last-frame.png"
        if not cv2.imwrite(str(image_path), self._latest):
            raise OSError(f"unable to save listener diagnostic: {image_path}")
        (output / "metadata.json").write_text(
            json.dumps({"reason": reason}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            f"ContinuousRealtimeLive diagnostic={image_path} reason={reason}",
            flush=True,
        )
        return image_path


def continuous_song_params(params: dict) -> dict:
    """Build the one-key playback policy without life-safety pausing.

    Cooperative screens may expose a different or non-interactive pause
    control.  This task must never invoke the life protection pause path.
    """
    return {
        **params,
        "require_profile": True,
        "ignore_note_speed": True,
        "duration_seconds": None,
        "wait_for_completion": True,
        "completion_missing_frames": int(
            params.get("completion_missing_frames", 30)
        ),
        "require_completion": False,
        "save_result_frame": False,
        "use_life_safety": False,
        "continue_after_life_depleted": True,
    }


def run_continuous_listener(
    capture: Callable[[], object],
    stopping: Callable[[], bool],
    play_song: Callable[[], bool],
    *,
    detector: LifeDetector | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    poll_interval_seconds: float = 0.1,
    confirm_frames: int = 3,
    on_frame: Callable[[object], None] | None = None,
) -> None:
    """Watch passively and invoke one-song playback after a stable life bar."""
    detector = detector or LifeDetector()
    visible_streak = 0
    armed = True
    while not stopping():
        image = capture()
        if on_frame is not None:
            on_frame(image)
        reading = detector.detect(image)
        if reading.visible and reading.value >= 20:
            visible_streak += 1
        else:
            visible_streak = 0
            armed = True
        if armed and visible_streak >= confirm_frames:
            armed = False
            if not play_song():
                raise RuntimeError("continuous realtime song playback failed")
            visible_streak = 0
            continue
        sleeper(poll_interval_seconds)


@AgentServer.custom_action("ContinuousRealtimeLive")
class ContinuousRealtimeLive(CustomAction):
    """Passively play every detected song until the user stops the task."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            return self._run(context, argv)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            record_failure_reason(reason)
            traceback.print_exc()
            print(f"ContinuousRealtimeLive failed={reason}", flush=True)
            return False

    def _run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        params = json.loads(argv.custom_action_param or "{}")
        if context.tasker.stopping:
            return True
        settings = resolve_profile_for_settings_gate(context, params)
        print(
            "ContinuousRealtimeLive started "
            f"profile={settings.profile_path.name} "
            f"profile_speed={settings.note_speed:.2f}; "
            "listener does not read or change game note speed, actual speed must match",
            flush=True,
        )

        song_params = continuous_song_params(params)
        diagnostics = ListenerDiagnosticCapture(
            Path(__file__).resolve().parents[2] / "debug" / "recordings"
        )

        def play_song() -> bool:
            if context.tasker.stopping:
                return True
            song_argv = SimpleNamespace(
                custom_action_param=json.dumps(song_params, ensure_ascii=False)
            )
            return RealtimeProfilePlay()._run(context, song_argv)

        try:
            run_continuous_listener(
                lambda: context.tasker.controller.post_screencap().wait().get(),
                lambda: context.tasker.stopping,
                play_song,
                on_frame=diagnostics.observe,
            )
        except Exception:
            diagnostics.save("failed")
            raise
        diagnostics.save("stopped")
        print("ContinuousRealtimeLive stopped by user", flush=True)
        return True
