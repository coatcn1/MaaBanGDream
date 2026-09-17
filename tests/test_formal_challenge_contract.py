import json
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).parents[1]


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load(name):
    return json.loads(
        (ROOT / name).read_text(encoding="utf-8"),
        object_pairs_hook=unique_object,
    )


def apply_selected_task_options(interface, nodes, task_name, selections):
    """Simulate MFA/Maa's ordered, shallow node-field override contract."""
    merged = deepcopy(nodes)
    task = next(task for task in interface["task"] if task["name"] == task_name)
    for option_name in task["option"]:
        selected_case_name = selections.get(option_name)
        if selected_case_name is None:
            continue
        option = interface["option"][option_name]
        selected_case = next(
            case for case in option["cases"]
            if case["name"] == selected_case_name
        )
        for node_name, fields in selected_case.get("pipeline_override", {}).items():
            merged.setdefault(node_name, {}).update(deepcopy(fields))
    return merged


def test_formal_mode_requires_profile_and_uses_distinct_profile_nodes():
    interface = load("interface.json")
    nodes = load("resource/pipeline/realtime_multi_live.json")
    formal = next(case for case in interface["option"]["RealtimeMode"]["cases"] if case["name"] == "Formal")
    assert formal["pipeline_override"]["RealtimeLiveFormalModeGate"]["next"] == ["RealtimeLiveRequireProfile"]
    assert nodes["RealtimeLiveRequireProfile"]["custom_action"] == "RealtimeProfileCheck"
    for name in ("RealtimeLiveFormalPlay", "RealtimeLiveFormalPlayNormal", "RealtimeLiveFormalPlayHard", "RealtimeLiveFormalPlayExpert", "RealtimeLiveFormalPlaySpecial"):
        params = nodes[name]["custom_action_param"]
        assert params["require_profile"] is True
        assert params["settings_gate_required"] is True
        assert params["result_back_attempts"] == 30
        assert params["result_back_interval_seconds"] == 1.5
        assert params["note_speed"] == (
            5.0 if name.endswith(("Expert", "Special")) else 2.0
        )


def test_formal_mode_keeps_strict_profile_checks_with_every_difficulty():
    interface = load("interface.json")
    nodes = load("resource/pipeline/realtime_multi_live.json")

    for difficulty, note_speed in {
        "Easy": 2.0,
        "Normal": 2.0,
        "Hard": 2.0,
        "Expert": 5.0,
        "Special": 5.0,
    }.items():
        merged = apply_selected_task_options(
            interface,
            nodes,
            "RealtimeLive",
            {
                "RealtimeMode": "Formal",
                "RealtimeLiveDifficulty": difficulty,
            },
        )

        assert merged["RealtimeLiveFormalModeGate"]["next"] == [
            "RealtimeLiveRequireProfile"
        ]
        assert merged["RealtimeLiveRequireProfile"]["custom_action_param"] == {
            "difficulty": difficulty,
            "dpi": 240,
            "game_fps": 60,
            "render_quality": "standard",
            "note_speed": note_speed,
        }
        assert merged["RealtimeLiveFormalReady"]["next"] == [
            "RealtimeLiveFormalSettingsGate"
        ]
        assert merged["RealtimeLiveFormalSettingsGate"][
            "custom_action_param"
        ] == {
            "confirm_preparation_identity": True,
            "difficulty": difficulty,
            "require_profile": True,
            "dpi": 240,
            "game_fps": 60,
            "render_quality": "standard",
        }


def test_calibration_is_single_task_with_three_rehearsal_contract():
    interface = load("interface.json")
    task = next(task for task in interface["task"] if task["name"] == "RealtimeCalibration")
    assert task["entry"] == "RealtimeCalibration"
    nodes = load("resource/pipeline/realtime_calibration.json")
    assert nodes["RealtimeCalibrationProcessConflictGuard"]["next"] == [
        "RealtimeCalibrationRecover"
    ]
    assert nodes["RealtimeCalibrationRecover"]["next"] == [
        "RealtimeCalibrationSpeedSettingsGate"
    ]
    assert nodes["RealtimeCalibrationSpeedSettingsGate"]["custom_action"] == (
        "RealtimeGameSpeedSettingsGate"
    )
    assert nodes["RealtimeCalibrationSpeedSettingsGate"]["custom_action_param"] == {
        "entry_mode": "home",
        "difficulty": "Easy",
        "require_profile": False,
        "dpi": 240,
        "game_fps": 60,
        "render_quality": "standard",
    }
    assert nodes["RealtimeCalibrationSpeedSettingsGate"]["next"] == [
        "CalibrationDifficultySetting"
    ]
    assert nodes["RealtimeCalibrationRun"]["custom_action"] == "RealtimeCalibrationRun"
    assert "CalibrationCaptureSong" not in nodes
    assert nodes["RealtimeCalibrationRoundComplete"]["action"] == "DoNothing"


def test_calibration_rounds_bypass_the_shared_multi_live_hit_counter():
    calibration = load("resource/pipeline/realtime_calibration.json")
    multi_live = load("resource/pipeline/realtime_multi_live.json")

    entry = calibration["RealtimeCalibrationSingleLive"]
    assert entry["action"] == "Custom"
    assert entry["custom_action"] == "CommonRecover"
    assert entry["next"] == ["RealtimeLiveDebugGate"]
    assert "max_hit" not in entry
    # 普通演出按入口命中数限制次数；校准嵌套调用仍必须绕过这道次数门禁。
    assert multi_live["RealtimeLiveRoundGate"]["custom_recognition"] == "TaskRoundAvailable"
    assert "max_hit" not in multi_live["RealtimeLiveRoundGate"]


def test_challenge_points_and_profile_contract():
    interface = load("interface.json")
    nodes = load("resource/pipeline/challenge_live.json")
    points = interface["option"]["ChallengePoints"]["cases"]
    assert {int(case["name"]): case["pipeline_override"]["ChallengePointSelect"]["target"] for case in points} == {
        200: [875, 212], 400: [875, 286], 800: [875, 359], 1600: [875, 431]
    }
    assert nodes["ChallengeProfileCheck"]["custom_action"] == "RealtimeProfileCheck"
    assert nodes["ChallengeProfileCheck"]["custom_action_param"]["run_mode"] == (
        "challenge"
    )
    assert nodes["ChallengeRecover"]["next"] == ["ChallengeSpeedSettingsGate"]
    assert nodes["ChallengeSpeedSettingsGate"]["custom_action"] == (
        "RealtimeGameSpeedSettingsGate"
    )
    assert nodes["ChallengeSpeedSettingsGate"]["custom_action_param"] == {
        "entry_mode": "home",
        "difficulty": "Easy",
        "require_profile": True,
        "dpi": 240,
        "game_fps": 60,
        "render_quality": "standard",
        "run_mode": "challenge",
    }
    assert nodes["ChallengeSpeedSettingsGate"]["next"] == [
        "ChallengeRoundGate"
    ]
    assert nodes["ChallengeBandMarker"]["next"] == ["ChallengeSettingsGate"]
    assert nodes["ChallengeSettingsGate"]["custom_action"] == (
        "RealtimePerformanceSettingsGate"
    )
    assert nodes["ChallengeSettingsGate"]["custom_action_param"]["run_mode"] == (
        "challenge"
    )
    assert nodes["ChallengeSettingsGate"]["next"] == ["ChallengeStart"]
    point_failure = nodes["ChallengePointStillOpen"]
    assert point_failure["custom_action"] == "TaskOutcome"
    assert point_failure["custom_action_param"]["status"] == "failure"
    assert nodes["ChallengeStart"]["next"] == ["ChallengePlay"]
    for name in ("ChallengePlay", "ChallengePlayNormal", "ChallengePlayHard", "ChallengePlayExpert", "ChallengePlaySpecial"):
        params = nodes[name]["custom_action_param"]
        assert params["require_profile"] is True
        assert params["settings_gate_required"] is True
        assert params["run_mode"] == "challenge"
        assert params["result_back_attempts"] == 30
        assert params["result_back_interval_seconds"] == 1.5
        assert params["note_speed"] == (
            5.0 if name.endswith(("Expert", "Special")) else 2.0
        )
        assert nodes[name]["on_error"] == ["ChallengeFailure"]
    assert nodes["ChallengeDifficulty"]["custom_action"] == "RealtimeDifficultySelect"
    assert nodes["ChallengeDifficulty"]["custom_action_param"]["mode"] == "challenge"
    for case in interface["option"]["ChallengeDifficulty"]["cases"]:
        override = case["pipeline_override"]
        assert override["ChallengeDifficulty"]["custom_action_param"]["mode"] == (
            "challenge"
        )
        fallback = override["ChallengeDifficulty"]["custom_action_param"].get(
            "fallback_difficulties"
        )
        assert fallback == (["Expert"] if case["name"] == "Special" else None)
        assert override["ChallengeProfileCheck"]["custom_action_param"][
            "run_mode"
        ] == "challenge"
        assert override["ChallengeSettingsGate"]["custom_action_param"][
            "run_mode"
        ] == "challenge"
    assert "ChallengeLifeSafetyGate" not in nodes
    assert "ChallengeLifeSafetyStop" not in nodes
