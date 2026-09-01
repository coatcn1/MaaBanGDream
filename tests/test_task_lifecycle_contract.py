from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_count_overrides_update_max_hit_and_progress_total_together():
    interface = load(ROOT / "interface.json")
    expected = {
        "AutoLiveCount": ("AutoLiveRoundGate", "AutoLive", "自动演出"),
        "RealtimeLiveCount": (
            "RealtimeLiveRoundGate",
            "RealtimeLive",
            "实时演奏",
        ),
        "ChallengeCount": (
            "ChallengeRoundGate",
            "ChallengeLive",
            "挑战演出",
        ),
    }
    for option_name, (gate, task_name, label) in expected.items():
        override = interface["option"][option_name]["pipeline_override"][gate]
        assert override["max_hit"] == "{Count}"
        assert override["custom_action_param"] == {
            "task_name": task_name,
            "label": label,
            "total": "{Count}",
            "phase": "start",
        }


def test_cooperative_count_is_owned_by_the_cooperative_flow():
    interface = load(ROOT / "interface.json")
    count = interface["option"]["CooperativeCount"]
    assert count["pipeline_override"] == {
        "CooperativeCountConfigure": {
            "custom_action_param": {"count": "{Count}"}
        }
    }


def test_live_select_actions_wait_for_loading_without_fixed_targets():
    for filename in (
        "auto_live.json",
        "realtime_multi_live.json",
        "cooperative_live.json",
        "challenge_live.json",
    ):
        nodes = load(ROOT / "resource" / "pipeline" / filename)
        for node in nodes.values():
            if node.get("custom_action") != "LiveSelectFind":
                continue
            params = node["custom_action_param"]
            assert params["timeout_ms"] == 10000
            assert params["interval_ms"] == 500
            assert "target" not in node


def test_failure_focus_is_visible_and_framework_failure_is_preserved():
    names = {
        "auto_live.json": (
            "AutoLiveFailure",
            "AutoLiveQuotaExhausted",
        ),
        "challenge_live.json": (
            "ChallengeNoEvent",
            "ChallengePointStillOpen",
            "ChallengeLifeSafetyStop",
        ),
        "realtime_calibration.json": ("RealtimeCalibrationFailure",),
        "realtime_multi_live.json": ("RealtimeLiveFailure",),
        "cooperative_live.json": ("CooperativeFailure",),
    }
    for filename, node_names in names.items():
        nodes = load(ROOT / "resource" / "pipeline" / filename)
        for node_name in node_names:
            node = nodes[node_name]
            assert node["custom_action"] == "TaskOutcome"
            assert node["custom_action_param"]["status"] == "failure"
            display = node["focus"]["Node.Action.Failed"]["display"]
            assert display == ["log"]
            assert "on_error" not in node


def test_task_entries_and_navigation_emit_visible_stage_logs():
    expected = {
        "auto_live.json": ("AutoLive", "AutoLiveHomeLive"),
        "realtime_multi_live.json": ("RealtimeMultiLive", "RealtimeLiveHomeLive"),
        "cooperative_live.json": ("CooperativeLive", "CooperativeHomeLive"),
        "challenge_live.json": ("ChallengeLive", "ChallengeHomeLive"),
    }
    for filename, (entry_name, live_name) in expected.items():
        nodes = load(ROOT / "resource" / "pipeline" / filename)
        entry_focus = nodes[entry_name]["focus"]
        assert entry_focus["Node.Action.Starting"]["display"] == ["log"]
        assert entry_focus["Node.Action.Succeeded"]["display"] == ["log"]
        live_focus = nodes[live_name]["focus"]
        assert live_focus["Node.Action.Starting"]["display"] == ["log"]
        assert live_focus["Node.Action.Succeeded"]["display"] == ["log"]


def test_pipeline_logs_never_refer_to_an_unspecified_previous_line():
    for path in (ROOT / "resource" / "pipeline").glob("*.json"):
        assert "上一条日志" not in path.read_text(encoding="utf-8"), path.name
