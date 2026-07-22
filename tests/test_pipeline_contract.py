from __future__ import annotations

import json
import hashlib
from pathlib import Path


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
    assert interface["version"] == "0.5.0"
    assert [task["name"] for task in interface["task"]] == [
        "AutoLive", "RealtimeLive", "RealtimeCalibration", "ChallengeLive"
    ]
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
    assert params["escape_timeout_ms"] == 30000
    assert params["restart_limit"] == 2
    assert params["click_nodes"] == ["LoginTapToStart", "LoginNext", "CommonClose"]


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
    assert prepare_order[:3] == [
        "AutoLiveQuotaExhausted",
        "AutoLiveEnabled",
        "AutoLiveDisabled",
    ]
    assert nodes["AutoLiveQuotaExhausted"]["action"] == "StopTask"
    assert nodes["AutoLiveDisabled"]["max_hit"] == 3
    assert nodes["AutoLivePrepare"]["target"] == [1040, 615, 100, 45]
    assert "AutoLiveStart" not in nodes["AutoLiveDisabled"]["next"]
    assert nodes["AutoLiveEnabled"]["next"] == ["AutoLiveStart"]
    incoming_to_start = [
        name
        for name, node in nodes.items()
        if "AutoLiveStart" in node.get("next", [])
    ]
    assert incoming_to_start == ["AutoLiveEnabled"]
    assert nodes["AutoLiveStart"]["timeout"] == 300000
    assert nodes["AutoLiveStart"]["target"] is True
    assert nodes["AutoLiveStart"]["next"] == ["AutoLiveResult"]
    assert nodes["AutoLiveResult"]["custom_action"] == "CommonRecover"
    assert nodes["AutoLiveResult"]["custom_action_param"]["home_node"] == (
        "AutoLiveHomeMarker"
    )


def test_auto_live_entry_recovers_to_home_before_navigation():
    nodes = load(ROOT / "resource/pipeline/auto_live.json")
    assert nodes["AutoLive"]["next"] == ["AutoLiveRoundGate"]
    assert nodes["AutoLiveRoundGate"]["next"] == ["AutoLiveEnsureHome"]
    recover = nodes["AutoLiveEnsureHome"]
    assert recover["custom_action"] == "CommonRecover"
    assert recover["custom_action_param"]["home_node"] == "AutoLiveHomeMarker"
    assert recover["custom_action_param"]["escape_interval_ms"] == 1500
    assert recover["custom_action_param"]["escape_timeout_ms"] == 60000
    assert recover["custom_action_param"]["restart_limit"] == 2
    assert recover["next"] == ["AutoLiveHomeMarker"]


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
    assert nodes["AutoLiveResult"]["next"] == [
        "AutoLiveRoundGate",
        "AutoLiveComplete",
    ]
    assert nodes["AutoLiveComplete"]["action"] == "DoNothing"


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
    assert full_song["custom_action_param"]["duration_seconds"] == 300
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
    assert nodes["RealtimeMultiLive"]["next"] == ["RealtimeLiveRoundGate"]
    assert nodes["RealtimeLiveRoundGate"]["max_hit"] == 1
    assert nodes["RealtimeLiveReturnHome"]["next"] == [
        "RealtimeLiveRoundGate", "RealtimeLiveComplete"
    ]
    assert nodes["RealtimeLiveRequireProfile"]["custom_action"] == "RealtimeProfileCheck"
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
        assert tuple(override["RealtimeLiveDifficulty"]["target"]) == target
        assert override["RealtimeLiveRehearsalStart"]["next"] == [play_node]
        params = nodes[play_node]["custom_action_param"]
        assert params["difficulty"] == case["name"]
        assert params["require_profile"] is False
        assert params["debug_recording"] is False
        assert params["require_completion"] is True
        assert nodes[play_node]["next"] == ["RealtimeLiveReturnHome"]

    song_mode = interface["option"]["RealtimeLiveSongMode"]
    assert song_mode["cases"][0]["pipeline_override"]["RealtimeLiveSongSelectMarker"]["next"] == ["RealtimeLiveDifficulty"]
    assert song_mode["cases"][1]["pipeline_override"]["RealtimeLiveSongSelectMarker"]["next"] == ["RealtimeLiveRandomSong"]
    count = interface["option"]["RealtimeLiveCount"]
    assert count["inputs"][0]["verify"] == "^(?:[1-9]|[1-9][0-9])$"
    assert count["pipeline_override"]["RealtimeLiveRoundGate"]["max_hit"] == "{Count}"
    assert nodes["RealtimeLivePrepare"]["next"] == ["RealtimeLiveFormalModeGate"]
    assert nodes["RealtimeLiveFormalModeGate"]["next"] == [
        "RealtimeLiveFormalMarker", "RealtimeLiveRehearsalMarker"
    ]
    assert nodes["RealtimeLiveFormalMarker"]["target"] == [55, 520]
    assert nodes["RealtimeLiveDemoSettingsMarker"]["target"] == [575, 385]
    assert nodes["RealtimeLiveDemoModeOff"]["target"] == [640, 525]
    assert nodes["RealtimeLiveRehearsalStart"]["template"] == "rehearsal_start.png"
    debug = interface["option"]["RealtimeLiveDebug"]
    enabled = next(case for case in debug["cases"] if case["name"] == "On")
    assert enabled["pipeline_override"]["RealtimeLiveDebugGate"]["custom_action_param"] == {"debug_recording": True}
    assert not any(task["name"] == "RealtimeFullSong" for task in interface["task"])
