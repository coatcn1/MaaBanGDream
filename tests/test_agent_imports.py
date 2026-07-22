from __future__ import annotations

import importlib


def test_all_custom_action_modules_import_together():
    for module in (
        "agent.realtime.profile_action",
        "agent.realtime.rehearsal_action",
        "agent.realtime.profile_play_action",
    ):
        importlib.import_module(module)
