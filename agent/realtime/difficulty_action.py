from __future__ import annotations

import json
import time
import traceback

import cv2

try:
    from ..foreground_guard import require_game_foreground
except ImportError:  # AgentServer imports realtime as a top-level package.
    from foreground_guard import require_game_foreground

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from .live_session import reset_live_run, update_live_run
from .song_identity import identify_song


DIFFICULTY_TARGETS = {
    "Easy": (715, 545),
    "Normal": (827, 545),
    "Hard": (940, 545),
    "Expert": (1051, 545),
    "Special": (1180, 545),
}


def selected_difficulty(image) -> str | None:
    """Return the coloured difficulty button on the 1280x720 song screen."""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    scores = {}
    for name, (x, y) in DIFFICULTY_TARGETS.items():
        roi = hsv[max(0, y - 30):y + 20, max(0, x - 25):x + 25]
        scores[name] = float(roi[:, :, 1].mean()) if roi.size else 0.0
    winner = max(scores, key=scores.get)
    return winner if scores[winner] >= 50.0 else None


@AgentServer.custom_action("RealtimeDifficultySelect")
class RealtimeDifficultySelect(CustomAction):
    """Select and confirm difficulty before the start button can be clicked."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            params = json.loads(argv.custom_action_param or "{}")
            requested = str(params.get("difficulty", "Easy"))
            if requested not in DIFFICULTY_TARGETS:
                raise ValueError(f"unsupported difficulty: {requested}")
            attempts = int(params.get("max_attempts", 3))
            if context.tasker.stopping:
                return True
            reset_live_run(
                mode=str(params.get("mode", "realtime")),
                difficulty=requested,
                profile_name=params.get("profile_name"),
                expected_note_speed=params.get("note_speed"),
                debug_recording=bool(params.get("debug_recording", False)),
            )
            controller = context.tasker.controller
            target = DIFFICULTY_TARGETS[requested]
            for attempt in range(1, attempts + 1):
                if context.tasker.stopping:
                    return True
                require_game_foreground(controller)
                controller.post_click(*target).wait()
                time.sleep(float(params.get("verify_delay_seconds", 0.35)))
                image = controller.post_screencap().wait().get()
                recognized = selected_difficulty(image)
                print(
                    f"RealtimeDifficultySelect requested={requested} "
                    f"target={target} attempt={attempt}/{attempts} "
                    f"recognized={recognized}",
                    flush=True,
                )
                if recognized == requested:
                    identity = identify_song(image)
                    update_live_run(
                        song_id=identity.song_id,
                        song_id_method=identity.method,
                        prepared_for_play=True,
                    )
                    print(
                        "RealtimeDifficultySelect "
                        f"song={identity.song_id} method={identity.method}",
                        flush=True,
                    )
                    return True
            print(
                f"RealtimeDifficultySelect failed requested={requested} "
                f"target={target} attempts={attempts}",
                flush=True,
            )
            return False
        except Exception as exc:
            traceback.print_exc()
            print(f"RealtimeDifficultySelect failed={type(exc).__name__}: {exc}", flush=True)
            return False
