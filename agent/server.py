from __future__ import annotations

import sys

from maa.agent.agent_server import AgentServer
from maa.toolkit import Toolkit

import common_recover  # noqa: F401 - registration happens at import time


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("Maa Agent socket id is required")
    Toolkit.init_option("./")
    AgentServer.start_up(sys.argv[-1])
    AgentServer.join()
    AgentServer.shut_down()


if __name__ == "__main__":
    main()
