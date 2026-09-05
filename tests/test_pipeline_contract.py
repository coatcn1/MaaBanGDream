from __future__ import annotations

import json
import hashlib
from pathlib import Path

from agent.realtime.difficulty_action import DIFFICULTY_TARGETS


ROOT = Path(__file__).parents[1]


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_all_pipeline_clicks_use_the_foreground_guard():
    for path in (ROOT / "resource/pipeline").glob("*.json"):
        for name, node in load(path).items():
            assert node.get("action") != "Click", f"unguarded click: {path.name}:{name}"


def test_interface_references_existing_entry_and_resource():
    interface = load(ROOT / "interface.json")
    assert interface["interface_version"] == 2
    assert interface["version"] == "1.2.3"
    assert [task["name"] for task in interface["task"]] == [
        "AutoLive", "RealtimeLive", "CooperativeLive", "ContinuousRealtimeLive",
        "RealtimeCalibration", "DailyFreeGacha", "ChallengeLive",
        "ManualFlowRecording",
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
        "ManualFlowRecording": "📹 手动流程录像",
    }
    assert interface["resource"][0]["path"] == ["./resource"]
    nodes = {}
    for path in (ROOT / "resource/pipeline").glob("*.json"):
        nodes.update(load(path))
    for task in interface["task"]:
        assert task["entry"] in nodes


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
    report = common["TaskReportVisible"]
    assert report == {
        "recognition": "DirectHit",
        "action": "DoNothing",
    }
    assert common["HomeMarker"]["threshold"] == 0.82


def test_all_home_markers_accept_the_current_home_screen_score():
    for path in (ROOT / "resource" / "pipeline").glob("*.json"):
        for node in load(path).values():
            if node.get("template") == "home_marker.png":
                assert node["threshold"] == 0.82


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
    assert nodes["AutoLiveResult"]["custom_action"] == "CommonRecover"
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
        (
            "RealtimeLiveVisualEvaluationStart",
            "RealtimeLiveVisualEvaluationPostStart",
            "RealtimeLiveVisualEvaluationSettingsConfirm",
            "RealtimeLiveVisualEvaluationPlay",
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
    assert count["inputs"][0]["verify"] == "^(?:[1-9]|[1-9][0-9])$"
    assert count["pipeline_override"]["AutoLiveRoundGate"]["max_hit"] == "{Count}"

    nodes = load(ROOT / "resource/pipeline/auto_live.json")
    assert nodes["AutoLiveRoundGate"]["max_hit"] == 1
    assert nodes["AutoLiveRandomSong"]["target"] == [687, 642]
    assert nodes["AutoLiveRandomSong"]["custom_action"] == "RandomSongSelect"
    assert nodes["AutoLiveResult"]["next"] == ["AutoLiveRoundCompleted"]
    assert nodes["AutoLiveRoundCompleted"]["next"] == [
        "AutoLiveRoundGate",
        "AutoLiveComplete",
    ]
    assert nodes["AutoLiveComplete"]["custom_action"] == "TaskOutcome"
    assert nodes["AutoLiveComplete"]["custom_action_param"]["status"] == "success"


def test_difficulty_cases_override_only_the_difficulty_target():
    interface = load(ROOT / "interface.json")
    cases = interface["option"]["AutoLiveDifficulty"]["cases"]
    assert [case["name"] for case in cases] == [
        "Easy",
        "Normal",
        "Hard",
        "Expert",
        "Special",
    ]
    assert all(
        list(case["pipeline_override"]) == ["AutoLiveDifficulty"]
        and list(case["pipeline_override"]["AutoLiveDifficulty"]) == ["target"]
        for case in cases
    )


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
        "RealtimeLiveEffectSettingsGate"
    ]
    assert nodes["RealtimeLiveEffectSettingsGate"]["custom_action"] == (
        "RealtimeGameEffectSettingsGate"
    )
    assert nodes["RealtimeLiveEffectSettingsGate"]["next"] == [
        "RealtimeLiveRoundGate"
    ]
    assert nodes["RealtimeLiveRoundGate"]["max_hit"] == 1
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
        assert selection == {"difficulty": case["name"], "max_attempts": 3}
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
        assert params["require_profile"] is False
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
        ]["require_profile"] is False
        assert nodes[play_node]["next"] == ["RealtimeLiveReturnHome"]

    song_mode = interface["option"]["RealtimeLiveSongMode"]
    assert song_mode["cases"][0]["pipeline_override"]["RealtimeLiveSongSelectMarker"]["next"] == ["RealtimeLiveDifficulty"]
    assert song_mode["cases"][1]["pipeline_override"]["RealtimeLiveSongSelectMarker"]["next"] == ["RealtimeLiveRandomSong"]
    assert nodes["RealtimeLiveRandomSong"]["custom_action"] == "RandomSongSelect"
    assert nodes["RealtimeLiveRandomSong"]["custom_action_param"]["max_attempts"] == 3
    count = interface["option"]["RealtimeLiveCount"]
    assert count["inputs"][0]["verify"] == "^(?:[1-9]|[1-9][0-9])$"
    assert count["pipeline_override"]["RealtimeLiveRoundGate"]["max_hit"] == "{Count}"
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


def test_continuous_realtime_live_is_a_pure_listener_task():
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
    watcher = nodes["ContinuousRealtimeWatcher"]
    assert watcher["custom_action"] == "ContinuousRealtimeLive"
    assert "next" not in watcher
    serialized = json.dumps(nodes, ensure_ascii=False)
    assert "Click" not in serialized
    assert "result" not in serialized.lower()

    difficulty = interface["option"]["ContinuousRealtimeDifficulty"]
    assert difficulty["default_case"] == "Easy"
    assert [case["name"] for case in difficulty["cases"]] == [
        "Easy", "Normal", "Hard", "Expert", "Special",
    ]
    for case in difficulty["cases"]:
        params = case["pipeline_override"]["ContinuousRealtimeWatcher"][
            "custom_action_param"
        ]
        assert params["difficulty"] == case["name"]
        assert params["debug_recording"] is False
    debug = interface["option"]["ContinuousRealtimeDebug"]
    assert debug["default_case"] == "Light"
    assert [case["name"] for case in debug["cases"]] == [
        "Light", "Off", "Full",
    ]
    params = {
        case["name"]: case["pipeline_override"]["ContinuousRealtimeWatcher"][
            "custom_action_param"
        ]
        for case in debug["cases"]
    }
    assert params == {
        "Light": {"debug_recording": False, "diagnostic_trace": True},
        "Off": {"debug_recording": False, "diagnostic_trace": False},
        "Full": {"debug_recording": True, "diagnostic_trace": True},
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
            "RealtimeLiveEffectSettingsGate",
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
            "RealtimeCalibrationVisualSettingsGate",
        ),
        (
            "challenge_live.json",
            "ChallengeLive",
            "ChallengeProcessConflictGuard",
            "ChallengeRecover",
            "ChallengeVisualSettingsGate",
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
