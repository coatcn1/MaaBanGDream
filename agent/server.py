from __future__ import annotations

import sys

from maa.agent.agent_server import AgentServer
from maa.tasker import Tasker

import common_recover  # noqa: F401 - registration happens at import time
import realtime.frame_observer  # noqa: F401 - registration happens at import time
import realtime.note_observer  # noqa: F401 - registration happens at import time
import realtime.profile_action  # noqa: F401 - registration happens at import time
import realtime.rehearsal_action  # noqa: F401 - registration happens at import time
import realtime.profile_play_action  # noqa: F401 - registration happens at import time


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("Maa Agent socket id is required")
    Tasker.set_log_dir("./debug")
    AgentServer.start_up(sys.argv[-1])
    AgentServer.join()
    AgentServer.shut_down()


if __name__ == "__main__":
    main()
