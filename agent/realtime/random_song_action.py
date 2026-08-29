from __future__ import annotations

import json
import time
import traceback

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    from ..foreground_guard import require_game_foreground
except ImportError:  # AgentServer loads modules from the agent directory.
    from foreground_guard import require_game_foreground

from .song_identity import UNKNOWN_SONG_ID, identify_song, same_song


# 1280x720 国服自由演出选歌界面（2026-08-09 实测）。
# 若歌曲列表被游戏内筛选锁成单曲，随机选曲永远抽到同一首，
# 必须先打开右侧筛选面板 -> 恢复默认值 -> 关闭。
SONG_FILTER_BUTTON = (1116, 55)
SONG_FILTER_RESET_BUTTON = (1164, 45)
SONG_FILTER_CLOSE_BUTTON = (964, 655)
FILTER_OPEN_DELAY_SECONDS = 1.2
FILTER_RESET_DELAY_SECONDS = 0.8
FILTER_CLOSE_DELAY_SECONDS = 1.0
RANDOM_VERIFY_DELAY_SECONDS = 1.0


def _point(box) -> tuple[int, int]:
    return box.x + box.w // 2, box.y + box.h // 2


def _click(controller, point: tuple[int, int], delay: float) -> None:
    controller.post_click(*point).wait()
    if delay > 0:
        time.sleep(delay)


def clear_song_filter(controller, params: dict) -> None:
    """Open the song filter panel, restore defaults, then close it."""
    def point(key: str, default):
        value = params.get(key, default)
        return tuple(int(item) for item in value)

    _click(
        controller,
        point("filter_button", SONG_FILTER_BUTTON),
        float(params.get("filter_open_delay_seconds", FILTER_OPEN_DELAY_SECONDS)),
    )
    _click(
        controller,
        point("filter_reset_button", SONG_FILTER_RESET_BUTTON),
        float(params.get("filter_reset_delay_seconds", FILTER_RESET_DELAY_SECONDS)),
    )
    _click(
        controller,
        point("filter_close_button", SONG_FILTER_CLOSE_BUTTON),
        float(params.get("filter_close_delay_seconds", FILTER_CLOSE_DELAY_SECONDS)),
    )


@AgentServer.custom_action("RandomSongSelect")
class RandomSongSelect(CustomAction):
    """Click the random-song button and verify the selected song actually changes.

    The game persists its song-list filter.  When the filter leaves only one
    song visible, the random button always picks that same song and calibration
    can never collect three distinct songs.  This action retries the click and,
    when the song does not change, resets the filter panel before retrying.
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            return self._run(context, argv)
        except Exception as exc:
            traceback.print_exc()
            print(f"RandomSongSelect failed={type(exc).__name__}: {exc}", flush=True)
            return False

    def _run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        params = json.loads(argv.custom_action_param or "{}")
        max_attempts = max(1, int(params.get("max_attempts", 3)))
        preserve_filter = bool(params.get("preserve_filter", False))
        excluded_song_ids = [
            str(item) for item in params.get("excluded_song_ids", [])
        ]
        verify_delay = float(
            params.get("verify_delay_seconds", RANDOM_VERIFY_DELAY_SECONDS)
        )
        controller = context.tasker.controller
        if context.tasker.stopping:
            return True
        require_game_foreground(controller)
        x, y = _point(argv.box)

        before = controller.post_screencap().wait().get()
        before_id = identify_song(before).song_id
        print(
            f"RandomSongSelect before={before_id} target=({x},{y}) "
            f"attempts={max_attempts}",
            flush=True,
        )

        for attempt in range(1, max_attempts + 1):
            if context.tasker.stopping:
                return True
            controller.post_click(x, y).wait()
            if verify_delay > 0:
                time.sleep(verify_delay)
            after = controller.post_screencap().wait().get()
            after_id = identify_song(after).song_id
            changed = (
                before_id != UNKNOWN_SONG_ID
                and after_id != UNKNOWN_SONG_ID
                and after_id != before_id
                and not same_song(before_id, after_id)
            )
            excluded = any(
                after_id == item or same_song(after_id, item)
                for item in excluded_song_ids
            ) if after_id != UNKNOWN_SONG_ID else False
            print(
                f"RandomSongSelect attempt={attempt}/{max_attempts} "
                f"before={before_id} after={after_id} changed={changed} "
                f"excluded={excluded}",
                flush=True,
            )
            if changed and not excluded:
                return True
            if attempt < max_attempts:
                if preserve_filter:
                    # Calibration random mode must keep the user's current
                    # 收藏/分类 filter.  Continue drawing inside that range.
                    if after_id != UNKNOWN_SONG_ID:
                        before_id = after_id
                else:
                    print(
                        "RandomSongSelect resetting song filter before retry",
                        flush=True,
                    )
                    clear_song_filter(controller, params)

        print(
            "RandomSongSelect failed: 点击随机选曲后歌曲未变化，"
            "在允许的尝试次数内未找到新的未使用歌曲；"
            "当前筛选保持不变，校准草稿已保留",
            flush=True,
        )
        return False
