from __future__ import annotations

import json

import pytest

from agent.realtime.profile_store import EnvironmentSignature, RealtimeProfileStore


SIGNATURE = EnvironmentSignature((1280, 720), 240, 60, "standard", 2.0)
SETTINGS = {
    "target_fps": 60,
    "timing_offset_ms": 12,
    "frame_timeout_ms": 150,
    "playfield_timeout_ms": 1500,
}


def payload(**changes):
    value = {
        "created_at": "2026-07-22T12:00:00",
        "difficulty": "Easy",
        "accepted": True,
        "environment": SIGNATURE.to_mapping(),
        "settings": SETTINGS,
    }
    value.update(changes)
    return value


def test_write_is_atomic_and_profiles_are_local_json(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    path = store.write(payload())

    assert path.name == "easy-20260722120000.json"
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1
    assert not list(tmp_path.glob("*.tmp"))
    assert store.list_profiles(accepted_only=True)[0]["_path"] == path


def test_unaccepted_profile_cannot_control_realtime_play(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    path = store.write(payload(accepted=False))

    with pytest.raises(ValueError, match="用户真机验收"):
        store.resolve(path.name, difficulty="Easy", current_signature=SIGNATURE)


@pytest.mark.parametrize(
    ("field", "value"),
    [("resolution", [1920, 1080]), ("dpi", 320), ("game_fps", 90),
     ("render_quality", "high"), ("note_speed", 2.5)],
)
def test_every_environment_field_invalidates_profile(tmp_path, field, value):
    store = RealtimeProfileStore(tmp_path)
    path = store.write(payload())
    current = SIGNATURE.to_mapping()
    current[field] = value

    with pytest.raises(ValueError, match=field):
        store.resolve(
            path.name,
            difficulty="Easy",
            current_signature=EnvironmentSignature.from_mapping(current),
        )


def test_resolve_returns_bounded_authoritative_settings(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    path = store.write(payload())

    settings = store.resolve(path.name, difficulty="Easy", current_signature=SIGNATURE)

    assert settings.target_fps == 60
    assert settings.timing_offset_ms == 12
    assert settings.profile_path == path


def test_profile_path_cannot_escape_local_directory(tmp_path):
    store = RealtimeProfileStore(tmp_path / "profiles")

    with pytest.raises(ValueError, match="profiles 目录"):
        store.load("../secret.json")
