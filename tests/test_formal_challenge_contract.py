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
        assert nodes[name]["custom_action_param"]["require_profile"] is True
        assert nodes[name]["custom_action_param"]["result_back_attempts"] == 30
        assert nodes[name]["custom_action_param"]["result_back_interval_seconds"] == 1.5


def test_calibration_is_single_task_with_three_rehearsal_contract():
    interface = load("interface.json")
    task = next(task for task in interface["task"] if task["name"] == "RealtimeCalibration")
    assert task["entry"] == "RealtimeCalibration"
    nodes = load("resource/pipeline/realtime_calibration.json")
    assert nodes["RealtimeCalibrationRun"]["custom_action"] == "RealtimeCalibrationRun"
    assert nodes["CalibrationCaptureSong"]["custom_action"] == "CalibrationSongIdentity"


def test_challenge_points_and_profile_contract():
    interface = load("interface.json")
    nodes = load("resource/pipeline/challenge_live.json")
    points = interface["option"]["ChallengePoints"]["cases"]
    assert {int(case["name"]): case["pipeline_override"]["ChallengePointSelect"]["target"] for case in points} == {
        200: [875, 212], 400: [875, 286], 800: [875, 359], 1600: [875, 431]
    }
    assert nodes["ChallengeProfileCheck"]["custom_action"] == "RealtimeProfileCheck"
    assert nodes["ChallengePointStillOpen"]["action"] == "StopTask"
    assert nodes["ChallengeStart"]["next"] == ["ChallengePlay"]
    for name in ("ChallengePlay", "ChallengePlayNormal", "ChallengePlayHard", "ChallengePlayExpert", "ChallengePlaySpecial"):
        assert nodes[name]["custom_action_param"]["require_profile"] is True
        assert nodes[name]["custom_action_param"]["result_back_attempts"] == 30
        assert nodes[name]["custom_action_param"]["result_back_interval_seconds"] == 1.5
        assert nodes[name]["on_error"] == ["ChallengeReturnHome"]
