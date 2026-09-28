from __future__ import annotations

import json
import hashlib
from pathlib import Path

from agent.realtime.difficulty_action import DIFFICULTY_TARGETS


ROOT = Path(__file__).parents[1]


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_all_count_inputs_accept_zero_and_reject_out_of_range():
    import re

    options = load(ROOT / "interface.json")["option"]
    for name in ("AutoLiveCount", "RealtimeLiveCount", "ChallengeCount", "CooperativeCount", "MedleyCount", "FesCount"):
        pattern = options[name]["inputs"][0]["verify"]
        for count in range(1001):
            expected = count <= 999 and (name != "MedleyCount" or count % 3 == 0)
            assert bool(re.fullmatch(pattern, str(count))) == expected, (name, count)
        for invalid in ("-1", "01", "1.5", "", "10000"):
            assert re.fullmatch(pattern, invalid) is None


def test_all_pipeline_clicks_use_the_foreground_guard():
    for path in (ROOT / "resource/pipeline").glob("*.json"):
        for name, node in load(path).items():
            assert node.get("action") != "Click", f"unguarded click: {path.name}:{name}"


def test_interface_references_existing_entry_and_resource():
    interface = load(ROOT / "interface.json")
    assert interface["interface_version"] == 2
    assert interface["version"] == "1.4.3"
    assert interface["license"] == "PolyForm-Noncommercial-1.0.0"
    assert interface["github"] == "https://github.com/coatcn1/MaaBanGDream"
    assert "mirrorchyan_rid" not in interface
    assert [task["name"] for task in interface["task"]] == [
        "AutoLive", "RealtimeLive", "CooperativeLive", "ContinuousRealtimeLive",
        "RealtimeCalibration", "DailyFreeGacha", "ChallengeLive",
        "MedleyLive", "FesLive", "ManualFlowRecording",
    ]
    assert {
        task["name"]: task["label"] for task in interface["task"]
    } == {
        "AutoLive": "🎶 自动演出",
        "RealtimeLive": "🎹 单人实时演奏",
        "CooperativeLive": "🤝 协力演出",
        "ContinuousRealtimeLive": "⚡ 一键实时演奏",
        "RealtimeCalibration": "🎯 实时演奏校准",
        "DailyFreeGacha": "🎁 每日免费抽卡",
        "ChallengeLive": "🏆 挑战演出",
        "MedleyLive": "🎼 组曲演奏",
        "FesLive": "🎪 团队演出 Fes",
        "ManualFlowRecording": "📹 手动流程录像",
    }
    assert interface["resource"][0]["path"] == ["./resource"]
    nodes = {}
    for path in (ROOT / "resource/pipeline").glob("*.json"):
        nodes.update(load(path))
    for task in interface["task"]:
        assert task["entry"] in nodes


def test_medley_pipeline_merges_options_and_defers_recovery_to_flow():
    interface = load(ROOT / "interface.json")
    pipeline = load(ROOT / "resource" / "pipeline" / "medley_live.json")
    options = interface["option"]

    assert pipeline["MedleyProcessConflictGuard"]["next"] == [
        "MedleyTourTypeConfigure"
    ]
    assert "MedleyRecover" not in pipeline
    assert pipeline["MedleyTourTypeConfigure"]["next"] == [
        "MedleySongModeConfigure"
    ]
    assert pipeline["MedleySongModeConfigure"]["next"] == [
        "MedleyDifficultyConfigure"
    ]
    assert pipeline["MedleyDifficultyConfigure"]["next"] == [
        "MedleyCountConfigure"
    ]
    assert pipeline["MedleyCountConfigure"]["next"] == ["MedleyDebugConfigure"]
    assert pipeline["MedleyDebugConfigure"]["next"] == ["MedleyFlow"]
    assert all(
        pipeline[name]["custom_action"] == "MedleyLiveConfigure"
        for name in (
            "MedleyTourTypeConfigure",
            "MedleySongModeConfigure",
            "MedleyDifficultyConfigure",
            "MedleyCountConfigure",
            "MedleyDebugConfigure",
        )
    )
    free = next(
        case for case in options["MedleyTourType"]["cases"]
        if case["name"] == "Free"
    )
    task = next(
        case for case in options["MedleyTourType"]["cases"]
        if case["name"] == "Task"
    )
    assert free["option"] == ["MedleySongMode", "MedleyDifficulty"]
    assert "option" not in task
    assert {
        case["name"] for case in options["MedleyDifficulty"]["cases"]
    } == {"Easy", "Normal", "Hard", "Expert", "Special"}
    count = options["MedleyCount"]
    assert count["type"] == "input"
    assert count["inputs"][0]["default"] == "3"
    assert count["inputs"][0]["pipeline_type"] == "int"
    assert count["pipeline_override"]["MedleyCountConfigure"][
        "custom_action_param"
    ] == {"count": "{Count}"}
    assert count["pipeline_override"]["MedleyComplete"][
        "custom_action_param"
    ] == {
        "task_name": "MedleyLive",
        "label": "组曲演奏",
        "total": "{Count}",
        "status": "success",
    }
    assert count["pipeline_override"]["MedleyFailure"][
        "custom_action_param"
    ] == {
        "task_name": "MedleyLive",
        "label": "组曲演奏",
        "total": "{Count}",
        "status": "failure",
        "reason": "组曲演奏流程未完成",
        "reason_source": "latest",
    }
    assert not any(
        "result" in str(node.get("template", "")).casefold()
        and "judgement" not in str(node.get("template", "")).casefold()
        for node in pipeline.values()
    )


def test_minimal_navigation_contract():
    common = load(ROOT / "resource/pipeline/common.json")
    nodes = load(ROOT / "resource/pipeline/minimal_navigation.json")
    merged = common | nodes
    for name in (
        "MinimalHomeMarker", "HomeLive", "FreeLive", "SongSelectMarker", "BackToHome"
    ):
        assert name in merged
    assert nodes["MinimalHomeMarker"]["next"] == ["HomeLive"]
    assert nodes["MinimalNavigation"]["action"] == "StartApp"
    assert "StartGame" not in nodes["MinimalNavigation"]["next"]
    assert nodes["BackToHome"]["custom_action"] == "CommonRecover"
    assert nodes["FreeLive"]["on_error"] == ["CommonRecover"]


def test_all_home_live_click_markers_use_the_validated_threshold():
    pipeline_dir = ROOT / "resource" / "pipeline"
    for path in (pipeline_dir / name for name in (
        "minimal_navigation.json",
        "auto_live.json",
        "realtime_multi_live.json",
        "cooperative_live.json",
        "challenge_live.json",
        "fes_live.json",
    )):
        nodes = json.loads(path.read_text(encoding="utf-8"))
        markers = [
            node for node in nodes.values()
            if node.get("template") == "home_live.png"
        ]
        assert markers, path.name
        assert all(node.get("threshold") == .82 for node in markers), path.name


def test_templates_exist_and_are_lossless_png():
    for pipeline in (ROOT / "resource/pipeline").glob("*.json"):
        for node in load(pipeline).values():
            template = node.get("template")
            if template:
                image = ROOT / "resource/image" / template
                assert image.is_file(), f"missing {image}"
                assert image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_recovery_is_bounded_and_shared():
    common = load(ROOT / "resource/pipeline/common.json")
    params = common["CommonRecover"]["custom_action_param"]
    assert params["escape_interval_ms"] == 1500
    assert params["escape_timeout_ms"] == 60000
    assert params["restart_limit"] == 2
    assert params["click_nodes"] == ["LoginTapToStart", "LoginNext", "CommonClose"]
    download = common["ResourceDownloadConfirm"]
    assert download["template"] == "login_resource_download.png"
    assert download["roi"] == [620, 520, 300, 120]
    assert download["threshold"] == .9
    assert download["custom_action"] == "ForegroundClick"
    download_page = common["ResourceDownloadPageMarker"]
    assert download_page["template"] == "login_resource_download_page.png"
    assert download_page["roi"] == [360, 80, 260, 110]
    assert download_page["threshold"] == .9
    download_progress = common["ResourceDownloadProgressMarker"]
    assert download_progress["template"] == "login_resource_download_progress.png"
    assert download_progress["roi"] == [20, 550, 240, 100]
    assert download_progress["threshold"] == .82
    refresh = common["CommonRefreshScreen"]
    assert refresh == {
        "recognition": "DirectHit",
        "action": "DoNothing",
    }
    assert common["MedleyResultRefreshScreen"] == {
        "recognition": "DirectHit",
        "action": "DoNothing",
        "pre_delay": 0,
        "post_delay": 0,
    }
    report = common["TaskReportVisible"]
    assert report == {
        "recognition": "DirectHit",
        "action": "DoNothing",
    }
    assert common["HomeMarker"]["threshold"] == 0.75


def test_all_home_markers_accept_the_current_home_screen_score():
    for path in (ROOT / "resource" / "pipeline").glob("*.json"):
        for node in load(path).values():
            if node.get("template") == "home_marker.png":
                assert node["threshold"] == 0.75


def test_all_pipeline_references_exist_and_nodes_are_unique():
    merged = {}
    for path in (ROOT / "resource/pipeline").glob("*.json"):
        nodes = load(path)
        duplicates = set(merged) & set(nodes)
        assert not duplicates, f"duplicate nodes in {path}: {sorted(duplicates)}"
        merged.update(nodes)
    for name, node in merged.items():
        for field in ("next", "on_error"):
            refs = node.get(field, [])
            if isinstance(refs, str):
                refs = [refs]
            for ref in refs:
                if isinstance(ref, dict):
                    ref = ref["name"]
                assert ref in merged, f"{name}.{field} references missing {ref}"


def test_auto_live_safety_and_timeout_contract():
    nodes = load(ROOT / "resource/pipeline/auto_live.json")
    prepare_order = nodes["AutoLivePrepare"]["next"]
    # The auto-live buttons only exist in formal mode. A rehearsal-mode
    # prepare page must be switched back first, or every check misses and
    # the loop deadlocks (live issue: entering auto play after calibration
    # left the game in rehearsal mode).
    assert prepare_order[:4] == [
        "AutoLiveRehearsalToFormal",
        "AutoLiveQuotaExhausted",
        "AutoLiveEnabled",
        "AutoLiveDisabled",
    ]
    rehearsal = nodes["AutoLiveRehearsalToFormal"]
    assert rehearsal["recognition"] == "TemplateMatch"
    assert rehearsal["template"] == "rehearsal_mode_marker.png"
    assert rehearsal["custom_action"] == "ForegroundClick"
    assert rehearsal["target"] == [55, 520]
    assert rehearsal["next"] == prepare_order[1:]
    for looper in ("AutoLivePrepareClose", "AutoLiveDisabled"):
        assert nodes[looper]["next"][0] == "AutoLiveRehearsalToFormal"
    quota = nodes["AutoLiveQuotaExhausted"]
    assert quota["recognition"] == "TemplateMatch"
    assert quota["template"] == "auto_live_exhausted.png"
    assert quota["custom_action"] == "TaskOutcome"
    assert quota["custom_action_param"]["status"] == "failure"
    assert nodes["AutoLiveDisabled"]["max_hit"] == 3
    assert nodes["AutoLivePrepare"]["target"] == [1040, 615, 100, 45]
    assert "AutoLiveStart" not in nodes["AutoLiveDisabled"]["next"]
    assert nodes["AutoLiveStart"]["timeout"] == 600000
    assert nodes["AutoLiveEnabled"]["next"] == ["AutoLiveStart"]
    incoming_to_start = [
        name
        for name, node in nodes.items()
        if "AutoLiveStart" in node.get("next", [])
    ]
    assert incoming_to_start == ["AutoLiveEnabled"]
    assert nodes["AutoLiveStart"]["timeout"] == 600000
    assert nodes["AutoLiveStart"]["target"] is True
    assert nodes["AutoLiveStart"]["next"] == ["AutoLiveResult"]
    assert nodes["AutoLiveResult"]["custom_action"] == "CompletedLiveRecover"
    assert nodes["AutoLiveResult"]["custom_action_param"]["home_node"] == (
        "AutoLiveHomeMarker"
    )
    result_recover = nodes["AutoLiveResult"]["custom_action_param"]
    assert result_recover["back_only"] is True
    assert result_recover["back_acceleration_click_point"] == [1279, 719]
    assert result_recover["click_nodes"] == []
    assert result_recover["back_only_click_nodes"] == [
        "AutoLiveStorySkipConfirmLarge",
        "AutoLiveStorySkipConfirm",
        "AutoLiveStorySkip",
        "AutoLiveStoryMenu",
    ]
    assert result_recover["escape_interval_ms"] == 500
    assert result_recover["restart_limit"] == 1
    assert result_recover["login_start_node"] == "AutoLiveLoginScreenMarker"
    assert result_recover["login_start_target"] == [640, 635]
    assert result_recover["login_tap_target"] == [640, 360]
    assert result_recover["escape_after_login_start"] is True


def test_realtime_start_clicks_require_visible_transition_confirmation():
    nodes = load(ROOT / "resource/pipeline/realtime_multi_live.json")
    for name in ("RealtimeLiveRehearsalStart", "RealtimeLiveFormalStart"):
        node = nodes[name]
        assert node["custom_action"] == "ForegroundClick"
        assert node["custom_action_param"] == {
            "confirm_absent_node": name,
            "confirm_attempts": 3,
            "confirm_interval_ms": 750,
        }


def test_realtime_start_handles_optional_pre_live_settings_confirmation():
    nodes = load(ROOT / "resource/pipeline/realtime_multi_live.json")
    cases = (
        (
            "RealtimeLiveRehearsalStart",
            "RealtimeLiveRehearsalPostStart",
            "RealtimeLiveRehearsalSettingsConfirm",
            "RealtimeLivePlay",
        ),
        (
            "RealtimeLiveFormalStart",
            "RealtimeLiveFormalPostStart",
            "RealtimeLiveFormalSettingsConfirm",
            "RealtimeLiveFormalPlay",
        ),
    )
    for start_name, post_start_name, confirm_name, play_name in cases:
        assert nodes[start_name]["next"] == [post_start_name]
        assert nodes[post_start_name]["post_delay"] == 1500
        assert nodes[post_start_name]["next"] == [confirm_name, play_name]
        confirm = nodes[confirm_name]
        assert confirm["template"] == "pre_live_settings_confirm.png"
        assert confirm["roi"] == [600, 520, 380, 180]
        assert confirm["custom_action"] == "ForegroundClick"
        assert confirm["target"] is True
        assert confirm["next"] == [play_name]

    play_nodes = [
        name for name, node in nodes.items()
        if node.get("custom_action") == "RealtimeProfilePlay"
    ]
    assert play_nodes
    assert all(
        nodes[name]["custom_action_param"]["startup_timeout_seconds"] == 60
        for name in play_nodes
    )


def test_auto_live_entry_recovers_to_home_before_navigation():
    nodes = load(ROOT / "resource/pipeline/auto_live.json")
    assert nodes["AutoLive"]["next"] == ["AutoLiveProcessConflictGuard"]
    assert nodes["AutoLiveRecover"]["next"] == ["AutoLiveRoundGate"]
    assert nodes["AutoLiveRoundGate"]["next"] == ["AutoLiveHomeLive"]
    recover = nodes["AutoLiveEnsureHome"]
    assert recover["custom_action"] == "CommonRecover"
    assert recover["custom_action_param"]["home_node"] == "AutoLiveHomeMarker"
    assert recover["custom_action_param"]["escape_interval_ms"] == 1500
    assert recover["custom_action_param"]["escape_timeout_ms"] == 60000
    assert recover["custom_action_param"]["restart_limit"] == 2
    assert recover["next"] == ["AutoLiveHomeLive"]


def test_cold_login_uses_stable_menu_marker_before_back():
    auto = load(ROOT / "resource/pipeline/auto_live.json")
    marker = auto["AutoLiveLoginScreenMarker"]
    assert marker["template"] == "login_menu_marker.png"
    assert marker["roi"] == [1150, 590, 130, 130]

    for path, node_name in (
        ("auto_live.json", "AutoLiveEnsureHome"),
        ("realtime_multi_live.json", "RealtimeLiveEnsureHome"),
        ("challenge_live.json", "ChallengeEnsureHome"),
    ):
        params = load(ROOT / "resource/pipeline" / path)[node_name][
            "custom_action_param"
        ]
        assert params["login_start_node"] == "AutoLiveLoginScreenMarker"
        assert params["login_start_target"] == [640, 635]
        assert params["login_marker_priority_attempts"] == 3
        assert params["login_tap_target"] == [640, 360]
        assert params["escape_after_login_start"] is True


def test_multi_live_options_and_loop_contract():
    interface = load(ROOT / "interface.json")
    task = next(task for task in interface["task"] if task["name"] == "AutoLive")
    assert task["option"] == [
        "AutoLiveSongMode",
        "AutoLiveDifficulty",
        "AutoLiveCount",
    ]
    song_mode = interface["option"]["AutoLiveSongMode"]
    assert [case["name"] for case in song_mode["cases"]] == ["Current", "Random"]
    count = interface["option"]["AutoLiveCount"]
    assert count["inputs"][0]["verify"] == "^(?:0|[1-9][0-9]{0,2})$"
    assert count["pipeline_override"]["AutoLiveRoundGate"]["custom_recognition_param"] == {"total": "{Count}"}

    nodes = load(ROOT / "resource/pipeline/auto_live.json")
    assert "max_hit" not in nodes["AutoLiveRoundGate"]
    assert nodes["AutoLiveRoundGate"]["custom_recognition"] == "TaskRoundAvailable"
    assert nodes["AutoLiveRandomSong"]["target"] == [687, 642]
    assert nodes["AutoLiveRandomSong"]["custom_action"] == "RandomSongSelect"
    assert nodes["AutoLiveDifficulty"]["custom_action"] == (
        "RealtimeDifficultySelect"
    )
    assert nodes["AutoLiveDifficulty"]["on_error"] == ["AutoLiveFailure"]
    assert nodes["AutoLiveResult"]["next"] == ["AutoLiveRoundCompleted"]
    assert nodes["AutoLiveRoundCompleted"]["next"] == [
        "AutoLiveRoundGate",
        "AutoLiveComplete",
    ]
    assert nodes["AutoLiveComplete"]["custom_action"] == "TaskOutcome"
    assert nodes["AutoLiveComplete"]["custom_action_param"]["status"] == "success"


def test_auto_live_difficulty_cases_use_verified_selection():
    interface = load(ROOT / "interface.json")
    cases = interface["option"]["AutoLiveDifficulty"]["cases"]
    assert [case["name"] for case in cases] == [
        "Easy",
        "Normal",
        "Hard",
        "Expert",
        "Special",
    ]
    for case in cases:
        assert list(case["pipeline_override"]) == ["AutoLiveDifficulty"]
        override = case["pipeline_override"]["AutoLiveDifficulty"]
        assert list(override) == ["custom_action_param"]
        expected_params = {
            "difficulty": case["name"],
            "max_attempts": 3,
            "verify_delay_seconds": 0.35,
            "track_live_run": False,
            "song_identity": False,
        }
        if case["name"] == "Special":
            expected_params["fallback_difficulties"] = ["Expert"]
        assert override["custom_action_param"] == expected_params


def test_auto_live_difficulty_overrides_resolve_in_real_maafw(tmp_path):
    import subprocess
    import sys

    interface = load(ROOT / "interface.json")
    pipeline = load(ROOT / "resource/pipeline/auto_live.json")
    cases = interface["option"]["AutoLiveDifficulty"]["cases"]
    code = """
import json, sys
from maa.resource import Resource
from maa.toolkit import Toolkit
data = json.load(sys.stdin)
Toolkit.init_option(data['log_dir'])
resource = Resource()
assert resource.override_pipeline({'AutoLiveDifficulty': data['base']})
effective = {}
for name, override in data['overrides'].items():
    assert resource.override_pipeline({'AutoLiveDifficulty': override})
    effective[name] = resource.get_node_data('AutoLiveDifficulty')['action']['param']['custom_action_param']
print(json.dumps(effective))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        input=json.dumps({
            "log_dir": str(tmp_path),
            "base": pipeline["AutoLiveDifficulty"],
            "overrides": {
                case["name"]: case["pipeline_override"]["AutoLiveDifficulty"]
                for case in cases
            },
        }),
        text=True,
        capture_output=True,
        check=True,
    )
    effective = json.loads(result.stdout)
    for difficulty, params in effective.items():
        expected_params = {
            "difficulty": difficulty,
            "max_attempts": 3,
            "verify_delay_seconds": 0.35,
            "track_live_run": False,
            "song_identity": False,
        }
        if difficulty == "Special":
            expected_params["fallback_difficulties"] = ["Expert"]
        assert params == expected_params


def test_realtime_observe_is_screenshot_only_and_bounded():
    interface = load(ROOT / "interface.json")
    assert not any(task["name"] == "RealtimeObserve" for task in interface["task"])
    node = load(ROOT / "resource/pipeline/realtime_observe.json")["RealtimeObserve"]
    assert node["action"] == "Custom"
    assert node["custom_action"] == "RealtimeObserve"
    assert node["custom_action_param"] == {
        "duration_seconds": 5,
        "frame_timeout_ms": 150,
    }

    assert not any(task["name"] == "RealtimeNoteObserve" for task in interface["task"])
    note_node = load(ROOT / "resource/pipeline/realtime_note_observe.json")[
        "RealtimeNoteObserve"
    ]
    assert note_node["action"] == "Custom"
    assert note_node["custom_action"] == "RealtimeNoteObserve"
    assert note_node["custom_action_param"] == {
        "duration_seconds": 10,
        "target_fps": 60,
    }

    profile = load(ROOT / "resource/pipeline/realtime_profile.json")[
        "RealtimeProfileDraft"
    ]
    assert profile["custom_action"] == "RealtimeProfileDraft"
    assert profile["custom_action_param"]["difficulty"] == "Easy"
    assert profile["custom_action_param"]["dpi"] == 240

    rehearsal = load(ROOT / "resource/pipeline/realtime_rehearsal.json")[
        "RealtimeEasyRehearsal"
    ]
    assert rehearsal["custom_action"] == "RealtimeEasyRehearsal"
    assert rehearsal["custom_action_param"] == {
        "duration_seconds": 30,
        "dpi": 240,
        "game_fps": 60,
        "render_quality": "standard",
        "note_speed": 2.0,
        "timing_offset_ms": 0,
    }

    profile_play = load(ROOT / "resource/pipeline/realtime_profile_play.json")[
        "RealtimeProfilePlay"
    ]
    assert profile_play["custom_action"] == "RealtimeProfilePlay"
    assert profile_play["custom_action_param"]["difficulty"] == "Easy"
    assert profile_play["custom_action_param"]["duration_seconds"] == 30
    assert not any(task["name"] == "RealtimeProfilePlay" for task in interface["task"])

    full_song = load(ROOT / "resource/pipeline/realtime_full_song.json")[
        "RealtimeFullSong"
    ]
    assert full_song["custom_action"] == "RealtimeProfilePlay"
    assert full_song["custom_action_param"]["duration_seconds"] == 600
    assert full_song["custom_action_param"]["completion_missing_frames"] == 120
    assert full_song["custom_action_param"]["require_completion"] is True
    assert full_song["custom_action_param"]["result_back_attempts"] == 30
    assert full_song["custom_action_param"]["result_back_interval_seconds"] == 1.5
    # The standalone entry remains available for development contracts, but is
    # hidden from MFA so an old checked task cannot run before multi-rehearsal.
    assert not any(task["name"] == "RealtimeFullSong" for task in interface["task"])


def test_imported_template_hashes_match_declared_sources():
    sources = load(ROOT / "docs/template-sources.json")
    assert sources["source"] == "BanGDreamAutoScript HEAD:assets/templates"
    for name, expected in sources["sha256"].items():
        image = ROOT / "resource/image" / name
        actual = hashlib.sha256(image.read_bytes()).hexdigest()
        assert actual == expected, f"source hash mismatch for {name}"


def test_realtime_multi_live_contract_and_options():
    nodes = load(ROOT / "resource/pipeline/realtime_multi_live.json")
    interface = load(ROOT / "interface.json")
    task = next(task for task in interface["task"] if task["name"] == "RealtimeLive")
    assert task["option"] == [
        "RealtimeMode", "RealtimeLiveSongMode", "RealtimeLiveDifficulty",
        "RealtimeLiveCount", "RealtimeLiveDebug",
    ]
    assert nodes["RealtimeMultiLive"]["next"] == [
        "RealtimeLiveProcessConflictGuard"
    ]
    assert nodes["RealtimeLiveRecover"]["next"] == [
        "RealtimeLiveSpeedSettingsGate"
    ]
    assert nodes["RealtimeLiveSpeedSettingsGate"]["custom_action"] == (
        "RealtimeGameSpeedSettingsGate"
    )
    assert nodes["RealtimeLiveSpeedSettingsGate"]["custom_action_param"] == {
        "entry_mode": "home",
        "difficulty": "Easy",
        "require_profile": True,
        "dpi": 240,
        "game_fps": 60,
        "render_quality": "standard",
    }
    assert nodes["RealtimeLiveSpeedSettingsGate"]["next"] == [
        "RealtimeLiveRoundGate"
    ]
    assert "max_hit" not in nodes["RealtimeLiveRoundGate"]
    assert nodes["RealtimeLiveRoundGate"]["custom_recognition"] == "TaskRoundAvailable"
    assert nodes["RealtimeLiveRoundGate"]["next"] == [
        "RealtimeLiveRetryReset"
    ]
    assert nodes["RealtimeLiveRetryReset"]["custom_action"] == (
        "RealtimePlayRetryControl"
    )
    assert nodes["RealtimeLiveRetryCheck"]["next"] == [
        "RealtimeLiveRetryRecover"
    ]
    assert nodes["RealtimeLiveRetryCheck"]["on_error"] == [
        "RealtimeLiveFailure"
    ]
    assert nodes["RealtimeLiveRetryRecover"]["next"] == [
        "RealtimeLiveDebugGate"
    ]
    for node in nodes.values():
        if node.get("custom_action") == "RealtimeProfilePlay":
            assert node["on_error"] == ["RealtimeLiveRetryCheck"]
    assert nodes["RealtimeLiveReturnHome"]["next"] == ["RealtimeLiveRoundCompleted"]
    return_home = nodes["RealtimeLiveReturnHome"]["custom_action_param"]
    assert return_home["back_only"] is True
    assert return_home["back_acceleration_click_point"] == [1279, 719]
    assert return_home["click_nodes"] == []
    assert return_home["back_only_click_nodes"] == [
        "AutoLiveStorySkipConfirmLarge",
        "AutoLiveStorySkipConfirm",
        "AutoLiveStorySkip",
        "AutoLiveStoryMenu",
    ]
    assert return_home["escape_interval_ms"] == 500
    assert return_home["restart_limit"] == 1
    assert return_home["login_start_node"] == "AutoLiveLoginScreenMarker"
    assert return_home["login_start_target"] == [640, 635]
    assert return_home["login_tap_target"] == [640, 360]
    assert return_home["escape_after_login_start"] is True
    challenge = load(ROOT / "resource/pipeline/challenge_live.json")
    challenge_return = challenge["ChallengeReturnHome"]["custom_action_param"]
    assert challenge_return["back_only"] is True
    assert challenge_return["back_acceleration_click_point"] == [1279, 719]
    assert challenge_return["click_nodes"] == []
    assert challenge_return["back_only_click_nodes"] == [
        "AutoLiveStorySkipConfirmLarge",
        "AutoLiveStorySkipConfirm",
        "AutoLiveStorySkip",
        "AutoLiveStoryMenu",
    ]
    assert challenge_return["escape_interval_ms"] == 500
    assert challenge_return["restart_limit"] == 1
    assert challenge_return["login_start_node"] == "AutoLiveLoginScreenMarker"
    assert challenge_return["login_start_target"] == [640, 635]
    assert challenge_return["login_tap_target"] == [640, 360]
    assert challenge_return["escape_after_login_start"] is True
    assert nodes["RealtimeLiveRoundCompleted"]["next"] == [
        "RealtimeLiveRoundGate", "RealtimeLiveComplete"
    ]
    assert nodes["RealtimeLiveRequireProfile"]["custom_action"] == "RealtimeProfileCheck"
    for gate in (
        "RealtimeLiveFormalSettingsGate",
        "RealtimeLiveRehearsalSettingsGate",
    ):
        assert nodes[gate]["custom_action"] == "RealtimePerformanceSettingsGate"
    assert "RealtimeLiveAutoOn" not in nodes
    assert "RealtimeLiveStart" not in nodes

    expected = {
        "Easy": ((715, 545), "RealtimeLivePlay"),
        "Normal": ((827, 545), "RealtimeLivePlayNormal"),
        "Hard": ((940, 545), "RealtimeLivePlayHard"),
        "Expert": ((1051, 545), "RealtimeLivePlayExpert"),
        "Special": ((1180, 545), "RealtimeLivePlaySpecial"),
    }
    difficulty = interface["option"]["RealtimeLiveDifficulty"]
    for case in difficulty["cases"]:
        target, play_node = expected[case["name"]]
        override = case["pipeline_override"]
        selection = override["RealtimeLiveDifficulty"]["custom_action_param"]
        expected_selection = {
            "difficulty": case["name"], "max_attempts": 3,
            "defer_song_title_to_preparation": True,
        }
        if case["name"] == "Special":
            expected_selection["fallback_difficulties"] = ["Expert"]
        assert selection == expected_selection
        assert override["RealtimeLiveSpeedSettingsGate"][
            "custom_action_param"
        ] == {
            "entry_mode": "home",
            "difficulty": case["name"],
            "require_profile": True,
            "dpi": 240,
            "game_fps": 60,
            "render_quality": "standard",
        }
        assert target == tuple(DIFFICULTY_TARGETS[case["name"]])
        assert override["RealtimeLiveRehearsalStart"]["next"] == [
            "RealtimeLiveRehearsalPostStart"
        ]
        assert override["RealtimeLiveRehearsalPostStart"]["next"] == [
            "RealtimeLiveRehearsalSettingsConfirm",
            play_node,
        ]
        assert override["RealtimeLiveRehearsalSettingsConfirm"]["next"] == [
            play_node
        ]
        formal_play_node = play_node.replace(
            "RealtimeLivePlay", "RealtimeLiveFormalPlay"
        )
        assert override["RealtimeLiveFormalStart"]["next"] == [
            "RealtimeLiveFormalPostStart"
        ]
        assert override["RealtimeLiveFormalPostStart"]["next"] == [
            "RealtimeLiveFormalSettingsConfirm",
            formal_play_node,
        ]
        assert override["RealtimeLiveFormalSettingsConfirm"]["next"] == [
            formal_play_node
        ]
        params = nodes[play_node]["custom_action_param"]
        assert params["difficulty"] == case["name"]
        assert params["require_profile"] is True
        assert params["rehearsal_mode"] is True
        assert params["settings_gate_required"] is True
        assert params["debug_recording"] is False
        assert params["require_completion"] is True
        assert params["startup_timeout_seconds"] == 60
        assert params["note_speed"] == (
            5.0 if case["name"] in {"Expert", "Special"} else 2.0
        )
        assert override["RealtimeLiveRequireProfile"]["custom_action_param"][
            "note_speed"
        ] == params["note_speed"]
        assert override["RealtimeLiveFormalSettingsGate"][
            "custom_action_param"
        ]["difficulty"] == case["name"]
        assert override["RealtimeLiveFormalSettingsGate"][
            "custom_action_param"
        ]["require_profile"] is True
        assert override["RealtimeLiveRehearsalSettingsGate"][
            "custom_action_param"
        ]["difficulty"] == case["name"]
        assert override["RealtimeLiveRehearsalSettingsGate"][
            "custom_action_param"
        ]["require_profile"] is True
        assert nodes[play_node]["next"] == ["RealtimeLiveReturnHome"]

    song_mode = interface["option"]["RealtimeLiveSongMode"]
    assert song_mode["cases"][0]["pipeline_override"]["RealtimeLiveSongSelectMarker"]["next"] == ["RealtimeLiveDifficulty"]
    assert song_mode["cases"][1]["pipeline_override"]["RealtimeLiveSongSelectMarker"]["next"] == ["RealtimeLiveRandomSong"]
    assert nodes["RealtimeLiveRandomSong"]["custom_action"] == "RandomSongSelect"
    assert nodes["RealtimeLiveRandomSong"]["custom_action_param"]["max_attempts"] == 3
    count = interface["option"]["RealtimeLiveCount"]
    assert count["inputs"][0]["verify"] == "^(?:0|[1-9][0-9]{0,2})$"
    assert count["pipeline_override"]["RealtimeLiveRoundGate"]["custom_recognition_param"] == {"total": "{Count}"}
    assert nodes["RealtimeLivePrepare"]["next"] == ["RealtimeLiveFormalModeGate"]
    assert nodes["RealtimeLiveDifficulty"]["custom_action"] == "RealtimeDifficultySelect"
    assert nodes["RealtimeLiveFormalModeGate"]["next"] == [
        "RealtimeLiveFormalMarker", "RealtimeLiveRehearsalMarker"
    ]
    assert nodes["RealtimeLiveFormalMarker"]["target"] == [55, 520]
    assert nodes["RealtimeLiveDemoSettingsMarker"]["target"] == [575, 385]
    assert nodes["RealtimeLiveDemoModeOff"]["target"] == [640, 525]
    assert nodes["RealtimeLiveDemoModeOff"]["next"] == [
        "RealtimeLiveRehearsalSettingsGate"
    ]
    assert nodes["RealtimeLiveFormalReady"]["next"] == [
        "RealtimeLiveFormalSettingsGate"
    ]
    assert nodes["RealtimeLiveFormalSettingsGate"]["next"] == [
        "RealtimeLiveFormalStart"
    ]
    assert nodes["RealtimeLiveRehearsalSettingsGate"]["next"] == [
        "RealtimeLiveRehearsalStart"
    ]
    assert nodes["RealtimeLiveRehearsalStart"]["template"] == "rehearsal_start.png"
    debug = interface["option"]["RealtimeLiveDebug"]
    assert debug["default_case"] == "Light"
    assert [case["name"] for case in debug["cases"]] == [
        "Light", "Off", "Full",
    ]
    params = {
        case["name"]: case["pipeline_override"]["RealtimeLiveDebugGate"][
            "custom_action_param"
        ]
        for case in debug["cases"]
    }
    assert params == {
        "Light": {"debug_recording": False, "diagnostic_trace": True},
        "Off": {"debug_recording": False, "diagnostic_trace": False},
        "Full": {"debug_recording": True, "diagnostic_trace": True},
    }
    assert not any(task["name"] == "RealtimeFullSong" for task in interface["task"])


def test_continuous_realtime_live_returns_home_after_internal_result_navigation():
    nodes = load(ROOT / "resource/pipeline/continuous_realtime_live.json")
    interface = load(ROOT / "interface.json")
    task = next(
        task for task in interface["task"]
        if task["name"] == "ContinuousRealtimeLive"
    )

    assert task["entry"] == "ContinuousRealtimeLive"
    assert task["option"] == [
        "ContinuousRealtimeDifficulty", "ContinuousRealtimeDebug",
    ]
    assert nodes["ContinuousRealtimeLive"]["next"] == [
        "ContinuousRealtimeProcessConflictGuard"
    ]
    assert nodes["ContinuousRealtimeProcessConflictGuard"]["custom_action"] == (
        "ProcessConflictGuard"
    )
    assert nodes["ContinuousRealtimeProcessConflictGuard"]["next"] == [
        "ContinuousRealtimeDifficultyConfigure"
    ]
    difficulty_gate = nodes["ContinuousRealtimeDifficultyConfigure"]
    assert difficulty_gate["custom_action"] == "ContinuousRealtimeLiveConfigure"
    assert difficulty_gate["custom_action_param"] == {
        "reset": True,
        "difficulty": "Easy",
    }
    assert difficulty_gate["next"] == ["ContinuousRealtimeDebugConfigure"]
    debug_gate = nodes["ContinuousRealtimeDebugConfigure"]
    assert debug_gate["custom_action"] == "ContinuousRealtimeLiveConfigure"
    assert debug_gate["custom_action_param"] == {
        "debug_recording": False,
        "diagnostic_trace": True,
    }
    assert debug_gate["next"] == ["ContinuousRealtimeWatcher"]
    watcher = nodes["ContinuousRealtimeWatcher"]
    assert watcher["custom_action"] == "ContinuousRealtimeLive"
    assert watcher["next"] == ["ContinuousRealtimeComplete"]
    starting = watcher["focus"]["Node.Action.Starting"]
    assert "已开始识别" in starting["content"]
    assert "任务所选难度" in starting["content"]
    assert "PGGBM" in starting["content"]
    assert "返回主页" in starting["content"]
    assert starting["display"] == ["log", "toast"]
    complete = nodes["ContinuousRealtimeComplete"]
    assert complete["custom_action"] == "TaskOutcome"
    assert complete["custom_action_param"]["status"] == "success"
    assert "PGGBM" in complete["focus"]["Node.Action.Succeeded"]["content"]
    assert "返回主页" in complete["focus"]["Node.Action.Succeeded"]["content"]
    serialized = json.dumps(nodes, ensure_ascii=False)
    assert "Click" not in serialized
    assert "result" not in serialized.lower()

    difficulty = interface["option"]["ContinuousRealtimeDifficulty"]
    assert difficulty["default_case"] == "Easy"
    assert [case["name"] for case in difficulty["cases"]] == [
        "Easy", "Normal", "Hard", "Expert", "Special",
    ]
    for case in difficulty["cases"]:
        params = case["pipeline_override"][
            "ContinuousRealtimeDifficultyConfigure"
        ][
            "custom_action_param"
        ]
        assert params == {"reset": True, "difficulty": case["name"]}
    debug = interface["option"]["ContinuousRealtimeDebug"]
    assert debug["default_case"] == "Light"
    assert [case["name"] for case in debug["cases"]] == [
        "Light", "Off", "Full",
    ]
    params = {
        case["name"]: case["pipeline_override"][
            "ContinuousRealtimeDebugConfigure"
        ][
            "custom_action_param"
        ]
        for case in debug["cases"]
    }
    assert params == {
        "Light": {"debug_recording": False, "diagnostic_trace": True},
        "Off": {"debug_recording": False, "diagnostic_trace": False},
        "Full": {"debug_recording": True, "diagnostic_trace": True},
    }


def test_continuous_difficulty_and_debug_resolve_in_real_maafw(tmp_path):
    import subprocess
    import sys

    interface = load(ROOT / "interface.json")
    pipeline = load(ROOT / "resource/pipeline/continuous_realtime_live.json")
    difficulty_case = next(
        case for case in interface["option"]["ContinuousRealtimeDifficulty"][
            "cases"
        ]
        if case["name"] == "Expert"
    )
    debug_case = next(
        case for case in interface["option"]["ContinuousRealtimeDebug"]["cases"]
        if case["name"] == "Light"
    )
    code = """
import json, sys
from maa.resource import Resource
from maa.toolkit import Toolkit
data = json.load(sys.stdin)
Toolkit.init_option(data['log_dir'])
resource = Resource()
assert resource.override_pipeline(data['base'])
assert resource.override_pipeline(data['difficulty'])
assert resource.override_pipeline(data['debug'])
print(json.dumps({
    node: resource.get_node_data(node)['action']['param']['custom_action_param']
    for node in data['base']
}))
"""
    nodes = (
        "ContinuousRealtimeDifficultyConfigure",
        "ContinuousRealtimeDebugConfigure",
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        input=json.dumps({
            "log_dir": str(tmp_path),
            "base": {node: pipeline[node] for node in nodes},
            "difficulty": difficulty_case["pipeline_override"],
            "debug": debug_case["pipeline_override"],
        }),
        text=True,
        capture_output=True,
        check=True,
    )
    effective = json.loads(result.stdout)
    assert effective["ContinuousRealtimeDifficultyConfigure"] == {
        "reset": True,
        "difficulty": "Expert",
    }
    assert effective["ContinuousRealtimeDebugConfigure"] == {
        "debug_recording": False,
        "diagnostic_trace": True,
    }


def test_calibration_and_challenge_offer_three_diagnostic_levels():
    interface = load(ROOT / "interface.json")
    for option_name, gate in (
        ("CalibrationDebug", "CalibrationDebugSetting"),
        ("ChallengeDebug", "ChallengeDebugGate"),
    ):
        option = interface["option"][option_name]
        assert option["default_case"] == "Light"
        assert [case["name"] for case in option["cases"]] == [
            "Light", "Off", "Full",
        ]
        params = {
            case["name"]: case["pipeline_override"][gate][
                "custom_action_param"
            ]
            for case in option["cases"]
        }
        assert params == {
            "Light": {"debug_recording": False, "diagnostic_trace": True},
            "Off": {"debug_recording": False, "diagnostic_trace": False},
            "Full": {"debug_recording": True, "diagnostic_trace": True},
        }


def test_task_entries_bootstrap_before_round_execution():
    entries = (
        (
            "auto_live.json",
            "AutoLive",
            "AutoLiveProcessConflictGuard",
            "AutoLiveRecover",
            "AutoLiveRoundGate",
        ),
        (
            "realtime_multi_live.json",
            "RealtimeMultiLive",
            "RealtimeLiveProcessConflictGuard",
            "RealtimeLiveRecover",
            "RealtimeLiveSpeedSettingsGate",
        ),
        (
            "cooperative_live.json",
            "CooperativeLive",
            "CooperativeProcessConflictGuard",
            "CooperativeRecover",
            "CooperativeEntryConfigure",
        ),
        (
            "realtime_calibration.json",
            "RealtimeCalibration",
            "RealtimeCalibrationProcessConflictGuard",
            "RealtimeCalibrationRecover",
            "RealtimeCalibrationSpeedSettingsGate",
        ),
        (
            "challenge_live.json",
            "ChallengeLive",
            "ChallengeProcessConflictGuard",
            "ChallengeRecover",
            "ChallengeSpeedSettingsGate",
        ),
        (
            "fes_live.json",
            "FesLive",
            "FesProcessConflictGuard",
            "FesRecover",
            "FesEntryConfigure",
        ),
    )
    for filename, entry_name, guard_name, recover_name, gate_name in entries:
        nodes = load(ROOT / "resource/pipeline" / filename)
        entry = nodes[entry_name]
        assert entry["next"] == [guard_name]
        guard = nodes[guard_name]
        assert guard["custom_action"] == "ProcessConflictGuard"
        expected_next = recover_name or gate_name
        assert guard["next"] == [expected_next]
        assert guard["on_error"]
        if recover_name:
            recover = nodes[recover_name]
            assert recover["custom_action"] == "CommonRecover"
            assert recover["custom_action_param"]["escape_timeout_ms"] == 60000
            assert recover["custom_action_param"]["restart_limit"] == 2
            assert recover["next"] == [gate_name]


def test_process_conflict_focus_text_does_not_expose_program_identity():
    for path in (
        "auto_live.json", "realtime_multi_live.json",
        "realtime_calibration.json", "challenge_live.json",
    ):
        serialized = json.dumps(
            load(ROOT / "resource/pipeline" / path), ensure_ascii=False,
        )
        assert "ALAS" not in serialized
        assert "AzurLaneAutoScript" not in serialized
        assert "PID" not in serialized


def test_formal_realtime_song_timeout_allows_long_music():
    filenames = (
        "realtime_full_song.json",
        "realtime_multi_live.json",
        "challenge_live.json",
    )
    for filename in filenames:
        nodes = load(ROOT / "resource" / "pipeline" / filename)
        formal_nodes = [
            node
            for node in nodes.values()
            if node.get("custom_action") == "RealtimeProfilePlay"
            and node.get("custom_action_param", {}).get("require_completion")
        ]
        assert formal_nodes
        assert all(
            node["custom_action_param"]["duration_seconds"] == 600
            for node in formal_nodes
        )


def test_live_select_uses_template_then_exact_ocr_action():
    auto = load(ROOT / "resource/pipeline/auto_live.json")
    realtime = load(ROOT / "resource/pipeline/realtime_multi_live.json")
    challenge = load(ROOT / "resource/pipeline/challenge_live.json")

    for nodes, page_name, entry_name, template_name in (
        (auto, "AutoLiveSelectPage", "AutoLiveFreeLive", "AutoLiveFreeLiveTemplate"),
        (
            realtime,
            "RealtimeLiveSelectPage",
            "RealtimeLiveFreeLive",
            "RealtimeLiveFreeLiveTemplate",
        ),
    ):
        assert nodes[page_name]["custom_action"] == "LiveSelectFind"
        assert nodes[page_name]["custom_action_param"]["click"] is False
        assert nodes[entry_name]["custom_action"] == "LiveSelectFind"
        assert nodes[entry_name]["custom_action_param"]["expected"] == "自由演出"
        assert nodes[entry_name]["custom_action_param"]["template_node"] == template_name
        assert "target" not in nodes[entry_name]

    entry = challenge["ChallengeEntry"]
    assert entry["custom_action"] == "LiveSelectFind"
    assert entry["custom_action_param"]["expected"] == "挑战演出"
    assert entry["on_error"] == ["ChallengeNoEvent"]
    assert challenge["ChallengeNoEvent"]["custom_action"] == "TaskOutcome"
    assert challenge["ChallengeNoEvent"]["custom_action_param"]["status"] == "failure"


def test_terminal_states_are_explicit_and_stop_task_is_not_used():
    for path in (ROOT / "resource/pipeline").glob("*.json"):
        for name, node in load(path).items():
            assert node.get("action") != "StopTask", f"ambiguous stop: {path.name}:{name}"

    for filename, complete in (
        ("auto_live.json", "AutoLiveComplete"),
        ("realtime_multi_live.json", "RealtimeLiveComplete"),
        ("challenge_live.json", "ChallengeComplete"),
    ):
        node = load(ROOT / "resource/pipeline" / filename)[complete]
        assert node["custom_action"] == "TaskOutcome"
        assert "Node.Action.Succeeded" in node["focus"]
