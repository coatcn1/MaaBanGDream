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
        "runtime_options": {"life_safety_enabled": True, "life_exit_threshold": 200},
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
        "life_safety_enabled": True,
        "life_exit_threshold": 200,
    }

    result = handle_request(
        {
            "operation": "update-runtime-options",
            "runtime_options": {"life_safety_enabled": False, "life_exit_threshold": 350},
        },
        root=tmp_path,
    )
    assert result["runtime_options"]["life_exit_threshold"] == 350
    assert store.load(path.name)["accepted"] is True
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("threshold", [9, 991])
def test_runtime_options_reject_invalid_life_threshold(tmp_path, threshold):
    with pytest.raises(ValueError, match="life_exit_threshold"):
        handle_request(
            {
                "operation": "update-runtime-options",
                "runtime_options": {"life_safety_enabled": True, "life_exit_threshold": threshold},
            },
            root=tmp_path,
        )
