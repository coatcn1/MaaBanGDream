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
        assert params["result_back_attempts"] == 30
        assert params["result_back_interval_seconds"] == 1.5
        assert params["note_speed"] == (
            5.0 if name.endswith(("Expert", "Special")) else 2.0
        )


def test_calibration_is_single_task_with_three_rehearsal_contract():
    interface = load("interface.json")
    task = next(task for task in interface["task"] if task["name"] == "RealtimeCalibration")
    assert task["entry"] == "RealtimeCalibration"
    nodes = load("resource/pipeline/realtime_calibration.json")
    assert nodes["RealtimeCalibrationRun"]["custom_action"] == "RealtimeCalibrationRun"
    assert nodes["CalibrationCaptureSong"]["custom_action"] == "CalibrationSongIdentity"
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
    point_failure = nodes["ChallengePointStillOpen"]
    assert point_failure["custom_action"] == "TaskOutcome"
    assert point_failure["custom_action_param"]["status"] == "failure"
    assert nodes["ChallengeStart"]["next"] == ["ChallengePlay"]
    for name in ("ChallengePlay", "ChallengePlayNormal", "ChallengePlayHard", "ChallengePlayExpert", "ChallengePlaySpecial"):
        params = nodes[name]["custom_action_param"]
        assert params["require_profile"] is True
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
