import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def load(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


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


def test_visual_evaluation_is_explicit_and_uses_formal_profile_path():
    interface = load("interface.json")
    experiment = next(
        case
        for case in interface["option"]["RealtimeMode"]["cases"]
        if case["name"] == "VisualEvaluation"
    )
    override = experiment["pipeline_override"]
    assert override["RealtimeLiveFormalModeGate"]["next"] == [
        "RealtimeLiveRequireProfile"
    ]
    assert override["RealtimeLiveRequireProfile"]["custom_action_param"][
        "visual_evaluation"
    ] is True
    assert override["RealtimeLiveFormalSettingsGate"]["custom_action_param"][
        "visual_evaluation"
    ] is True
    for node in (
        "RealtimeLiveFormalPlay",
        "RealtimeLiveFormalPlayNormal",
        "RealtimeLiveFormalPlayHard",
        "RealtimeLiveFormalPlayExpert",
        "RealtimeLiveFormalPlaySpecial",
    ):
        params = override[node]["custom_action_param"]
        assert params == {
            "visual_evaluation": True,
            "run_mode": "visual-evaluation",
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
        "RealtimeCalibrationVisualSettingsGate"
    ]
    assert nodes["RealtimeCalibrationVisualSettingsGate"]["custom_action"] == (
        "RealtimeGameEffectSettingsGate"
    )
    assert nodes["RealtimeCalibrationVisualSettingsGate"]["next"] == [
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
    # This shared gate is intentionally stateful for normal 1-99 round tasks,
    # so a calibration Custom Action must never reuse it across nested calls.
    assert multi_live["RealtimeLiveRoundGate"]["max_hit"] == 1


def test_challenge_points_and_profile_contract():
    interface = load("interface.json")
    nodes = load("resource/pipeline/challenge_live.json")
    points = interface["option"]["ChallengePoints"]["cases"]
    assert {int(case["name"]): case["pipeline_override"]["ChallengePointSelect"]["target"] for case in points} == {
        200: [875, 212], 400: [875, 286], 800: [875, 359], 1600: [875, 431]
    }
    assert nodes["ChallengeProfileCheck"]["custom_action"] == "RealtimeProfileCheck"
    assert nodes["ChallengeRecover"]["next"] == ["ChallengeVisualSettingsGate"]
    assert nodes["ChallengeVisualSettingsGate"]["custom_action"] == (
        "RealtimeGameEffectSettingsGate"
    )
    assert nodes["ChallengeVisualSettingsGate"]["next"] == [
        "ChallengeRoundGate"
    ]
    assert nodes["ChallengeBandMarker"]["next"] == ["ChallengeSettingsGate"]
    assert nodes["ChallengeSettingsGate"]["custom_action"] == (
        "RealtimePerformanceSettingsGate"
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
        assert nodes[name]["on_error"] == ["ChallengeLifeSafetyGate"]
    assert nodes["ChallengeDifficulty"]["custom_action"] == "RealtimeDifficultySelect"
    assert nodes["ChallengeLifeSafetyGate"]["custom_action"] == "RealtimeLifeSafetyAbortCheck"
    life_failure = nodes["ChallengeLifeSafetyStop"]
    assert life_failure["custom_action"] == "TaskOutcome"
    assert life_failure["custom_action_param"]["status"] == "failure"
