from __future__ import annotations

import json

import pytest

from agent.profile_manager import handle_request
from agent.realtime.profile_store import RealtimeProfileStore
from tests.test_realtime_profile import SIGNATURE, payload


def test_list_reports_automatic_selection_and_full_records(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    profile = payload(difficulty="Expert", accepted=True)
    profile["rehearsals"] = [{"song_id": "song-1", "passed": True}]
    path = store.write(profile)

    result = handle_request(
        {"operation": "list", "difficulty": "Easy", "environment": SIGNATURE.to_mapping()},
        root=tmp_path,
    )

    assert result["selection"] == {"mode": "auto", "profile": path.name, "source_difficulty": "Expert"}
    assert result["profiles"][0]["rehearsals"][0]["song_id"] == "song-1"


def test_pin_unpin_and_update_round_trip(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    path = store.write(payload(difficulty="Hard", accepted=True))

    pinned = handle_request(
        {"operation": "pin", "difficulty": "Easy", "profile": path.name}, root=tmp_path
    )
    assert pinned["pinned"]["Easy"] == path.name

    updated = handle_request(
        {
            "operation": "update-settings",
            "profile": path.name,
            "settings": {
                "target_fps": 60,
                "timing_offset_ms": -3,
                "frame_timeout_ms": 150,
                "playfield_timeout_ms": 1500,
            },
        },
        root=tmp_path,
    )
    assert updated["profile"]["accepted"] is False

    unpinned = handle_request(
        {"operation": "unpin", "difficulty": "Easy"}, root=tmp_path
    )
    assert "Easy" not in unpinned["pinned"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_fps", 14),
        ("target_fps", 241),
        ("timing_offset_ms", -251),
        ("timing_offset_ms", 251),
        ("frame_timeout_ms", 49),
        ("frame_timeout_ms", 5001),
        ("playfield_timeout_ms", 10001),
    ],
)
def test_update_rejects_out_of_range_settings(tmp_path, field, value):
    store = RealtimeProfileStore(tmp_path)
    path = store.write(payload(accepted=True))
    settings = {
        "target_fps": 60,
        "timing_offset_ms": 0,
        "frame_timeout_ms": 150,
        "playfield_timeout_ms": 1500,
    }
    settings[field] = value

    with pytest.raises(ValueError):
        handle_request(
            {"operation": "update-settings", "profile": path.name, "settings": settings},
            root=tmp_path,
        )

    assert store.load(path.name)["accepted"] is True


def test_update_rejects_playfield_timeout_below_frame_timeout(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    path = store.write(payload(accepted=True))

    with pytest.raises(ValueError, match="playfield_timeout_ms"):
        handle_request(
            {
                "operation": "update-settings",
                "profile": path.name,
                "settings": {
                    "target_fps": 60,
                    "timing_offset_ms": 0,
                    "frame_timeout_ms": 500,
                    "playfield_timeout_ms": 499,
                },
            },
            root=tmp_path,
        )


@pytest.mark.parametrize("operation", ["pin", "update-settings"])
def test_manager_rejects_profile_path_traversal(tmp_path, operation):
    request = {"operation": operation, "difficulty": "Easy", "profile": "../secret.json"}
    if operation == "update-settings":
        request["settings"] = {
            "target_fps": 60,
            "timing_offset_ms": 0,
            "frame_timeout_ms": 150,
            "playfield_timeout_ms": 1500,
        }

    with pytest.raises(ValueError, match="profiles 目录"):
        handle_request(request, root=tmp_path)


def test_selection_state_is_written_atomically(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    path = store.write(payload(difficulty="Expert", accepted=True))

    store.pin("Easy", path.name)

    state = json.loads((tmp_path / "selection.json").read_text(encoding="utf-8"))
    assert state == {
        "version": 1,
        "pinned": {"Easy": path.name},
        "runtime_options": {
            "skip_process_conflict_cleanup": False,
            "game_effect_settings_enabled": True,
            "note_skin_type": 1,
            "judgement_assist_effect": True,
            "tap_effect": 1,
            "chart_prediction_enabled": True,
            "chart_predict_presses": True,
            "native_realtime_enabled": False,
            "cooperative_jitter_enabled": True,
            "play_failure_retry_count": 1,
            "calibration_note_speeds": {
                "Easy": 2.0,
                "Normal": 2.0,
                "Hard": 2.0,
                "Expert": 5.0,
                "Special": 5.0,
            },
        },
    }
    assert not list(tmp_path.glob("*.tmp"))


def test_runtime_options_default_and_atomic_update_do_not_invalidate_profile(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    path = store.write(payload(accepted=True))

    listed = handle_request(
        {"operation": "list", "difficulty": "Easy", "environment": SIGNATURE.to_mapping()},
        root=tmp_path,
    )
    assert listed["runtime_options"] == {
        "skip_process_conflict_cleanup": False,
        "game_effect_settings_enabled": True,
        "note_skin_type": 1,
        "judgement_assist_effect": True,
        "tap_effect": 1,
            "chart_prediction_enabled": True,
            "chart_predict_presses": True,
            "native_realtime_enabled": False,
            "cooperative_jitter_enabled": True,
            "play_failure_retry_count": 1,
            "calibration_note_speeds": {
            "Easy": 2.0,
            "Normal": 2.0,
            "Hard": 2.0,
            "Expert": 5.0,
            "Special": 5.0,
        },
    }

    result = handle_request(
        {
            "operation": "update-runtime-options",
            "runtime_options": {
                "calibration_note_speeds": {
                    "Easy": 1.5,
                    "Normal": 2.5,
                    "Hard": 3.5,
                    "Expert": 5.0,
                    "Special": 5.5,
                },
            },
        },
        root=tmp_path,
    )
    assert result["runtime_options"]["calibration_note_speeds"]["Hard"] == 3.5
    assert store.load(path.name)["accepted"] is True
    assert not list(tmp_path.glob("*.tmp"))


def test_list_uses_configured_visual_settings_when_environment_omits_them(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    visual_environment = SIGNATURE.to_mapping()
    visual_environment.update({
        "note_skin_type": 7,
        "tap_effect": 4,
        "judgement_assist_effect": False,
    })
    store.write(payload(environment=visual_environment, accepted=True))
    runtime = store.runtime_options()
    runtime.update({
        "note_skin_type": 7,
        "tap_effect": 4,
        "judgement_assist_effect": False,
    })
    store.update_runtime_options(runtime)
    legacy_environment = SIGNATURE.to_mapping()
    for key in ("note_skin_type", "tap_effect", "judgement_assist_effect"):
        legacy_environment.pop(key)

    result = handle_request(
        {
            "operation": "list",
            "difficulty": "Easy",
            "environment": legacy_environment,
        },
        root=tmp_path,
    )

    assert result["profiles"][0]["environment_match"] is True
    assert result["selection"]["profile"] == result["profiles"][0]["filename"]


def test_legacy_life_protection_options_are_dropped_on_write(tmp_path):
    result = handle_request(
        {
            "operation": "update-runtime-options",
            "runtime_options": {
                "life_safety_enabled": True,
                "life_exit_threshold": 200,
                "rehearsal_ignore_life_safety": False,
            },
        },
        root=tmp_path,
    )

    assert "life_safety_enabled" not in result["runtime_options"]
    assert "life_exit_threshold" not in result["runtime_options"]
    assert "rehearsal_ignore_life_safety" not in result["runtime_options"]


@pytest.mark.parametrize(
    ("difficulty", "expected"),
    [
        ("Easy", ("Easy", "Normal", "Hard", "Expert", "Special")),
        ("Normal", ("Normal", "Hard", "Expert", "Special")),
        ("Hard", ("Hard", "Expert", "Special")),
        ("Expert", ("Expert", "Special")),
        ("Special", ("Special", "Expert")),
    ],
)
def test_expert_and_special_share_the_high_difficulty_compatibility_tier(
    difficulty,
    expected,
):
    assert RealtimeProfileStore.compatible_difficulties(difficulty) == expected


def test_special_task_can_pin_expert_profile(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    path = store.write(payload(difficulty="Expert", accepted=True))

    result = handle_request(
        {"operation": "pin", "difficulty": "Special", "profile": path.name},
        root=tmp_path,
    )

    assert result["pinned"]["Special"] == path.name


def test_special_auto_selection_prefers_exact_difficulty_before_expert(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    expert = store.write(payload(difficulty="Expert", accepted=True))
    special = store.write(payload(difficulty="Special", accepted=True))

    result = handle_request(
        {
            "operation": "list",
            "difficulty": "Special",
            "environment": SIGNATURE.to_mapping(),
        },
        root=tmp_path,
    )

    assert result["selection"]["profile"] == special.name
    assert result["selection"]["profile"] != expert.name


@pytest.mark.parametrize("retry_count", [-1, 4, True, 1.5, "two"])
def test_runtime_options_reject_invalid_play_failure_retry_count(
    tmp_path,
    retry_count,
):
    with pytest.raises(ValueError, match="play_failure_retry_count"):
        handle_request(
            {
                "operation": "update-runtime-options",
                "runtime_options": {
                    "play_failure_retry_count": retry_count,
                },
            },
            root=tmp_path,
        )
