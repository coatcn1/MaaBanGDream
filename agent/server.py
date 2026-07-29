from __future__ import annotations

import sys

from maa.agent.agent_server import AgentServer
from maa.tasker import Tasker

import common_recover  # noqa: F401 - registration happens at import time
import live_select  # noqa: F401 - registration happens at import time
import task_reporting  # noqa: F401 - registration happens at import time
import foreground_click  # noqa: F401 - registration happens at import time
import process_conflict_guard  # noqa: F401 - registration happens at import time
import realtime.frame_observer  # noqa: F401 - registration happens at import time
import realtime.note_observer  # noqa: F401 - registration happens at import time
import realtime.profile_action  # noqa: F401 - registration happens at import time
import realtime.rehearsal_action  # noqa: F401 - registration happens at import time
import realtime.profile_play_action  # noqa: F401 - registration happens at import time
import realtime.formal_preflight  # noqa: F401 - registration happens at import time
import realtime.calibration_action  # noqa: F401 - registration happens at import time
import realtime.difficulty_action  # noqa: F401 - registration happens at import time
import realtime.runtime_options  # noqa: F401 - registration happens at import time
import realtime.performance_settings_action  # noqa: F401 - registration happens at import time
import realtime.game_effect_settings_action  # noqa: F401 - registration happens at import time


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("Maa Agent socket id is required")
    Tasker.set_log_dir("./debug")
    AgentServer.start_up(sys.argv[-1])
    AgentServer.join()
    AgentServer.shut_down()


if __name__ == "__main__":
    main()
