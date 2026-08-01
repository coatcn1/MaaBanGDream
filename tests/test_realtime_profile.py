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


def test_resolve_latest_selects_newest_accepted_matching_difficulty(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    older = store.write(payload(created_at="2026-07-22T12:00:00"))
    newer = store.write(payload(created_at="2026-07-22T13:00:00"))

    settings = store.resolve_latest(difficulty="Easy", current_signature=SIGNATURE)

    assert settings.profile_path == newer
    assert settings.profile_path != older


def test_select_for_settings_gate_uses_profile_speed_but_checks_other_environment(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    expert_signature = EnvironmentSignature((1280, 720), 240, 60, "standard", 5.0)
    path = store.write(payload(
        difficulty="Expert",
        environment=expert_signature.to_mapping(),
    ))

    settings = store.resolve_latest_for_environment(
        difficulty="Expert",
        current_signature=EnvironmentSignature(
            (1280, 720), 240, 60, "standard", 2.0,
        ),
    )

    assert settings.profile_path == path
    assert settings.note_speed == 5.0


def test_select_for_settings_gate_still_rejects_non_speed_environment_drift(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    store.write(payload(difficulty="Expert"))

    with pytest.raises(ValueError, match="Profile"):
        store.resolve_latest_for_environment(
            difficulty="Expert",
            current_signature=EnvironmentSignature(
                (1920, 1080), 240, 60, "standard", 2.0,
            ),
        )


def test_runtime_options_accept_game_note_speed_upper_bound(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    options = store.runtime_options()
    options["calibration_note_speeds"] = {
        difficulty: 12.0
        for difficulty in RealtimeProfileStore.DIFFICULTIES
    }

    updated = store.update_runtime_options(options)

    assert set(updated["calibration_note_speeds"].values()) == {12.0}


def test_runtime_options_default_to_detector_friendly_game_effects(tmp_path):
    options = RealtimeProfileStore(tmp_path).runtime_options()

    assert options["game_effect_settings_enabled"] is True
    assert options["judgement_assist_effect"] is True
    assert options["tap_effect"] == 1
    assert options["skip_process_conflict_cleanup"] is False


def test_runtime_options_persist_process_conflict_cleanup_switch(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    options = store.runtime_options()
    options["skip_process_conflict_cleanup"] = True

    updated = store.update_runtime_options(options)

    assert updated["skip_process_conflict_cleanup"] is True
    assert store.runtime_options()["skip_process_conflict_cleanup"] is True


@pytest.mark.parametrize("tap_effect", [0, 6, "bad"])
def test_runtime_options_reject_invalid_tap_effect(tmp_path, tap_effect):
    store = RealtimeProfileStore(tmp_path)
    options = store.runtime_options()
    options["tap_effect"] = tap_effect

    with pytest.raises(ValueError, match="tap_effect"):
        store.update_runtime_options(options)


def test_resolve_latest_rejects_when_no_profile_was_accepted(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    store.write(payload(accepted=False))

    with pytest.raises(ValueError, match="没有已验收"):
        store.resolve_latest(difficulty="Easy", current_signature=SIGNATURE)


def test_higher_main_difficulty_profile_is_compatible_and_nearest_wins(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    expert = store.write(payload(difficulty="Expert", accepted=True))
    hard = store.write(payload(difficulty="Hard", accepted=True))

    settings = store.resolve_latest(difficulty="Normal", current_signature=SIGNATURE)

    assert settings.profile_path == hard
    assert settings.profile_path != expert


def test_special_profile_is_isolated_from_main_difficulties(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    store.write(payload(difficulty="Special", accepted=True))

    with pytest.raises(ValueError, match="Normal Profile"):
        store.resolve_latest(difficulty="Normal", current_signature=SIGNATURE)


def test_pinned_invalid_profile_blocks_without_automatic_fallback(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    accepted = store.write(payload(difficulty="Hard", accepted=True))
    draft = store.write(payload(difficulty="Normal", accepted=False))
    store.pin("Easy", draft.name)

    with pytest.raises(ValueError, match="钉选.*尚未通过"):
        store.resolve_latest(difficulty="Easy", current_signature=SIGNATURE)

    assert accepted.exists()


def test_edit_settings_invalidates_profile_and_preserves_records(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    profile = payload(
        difficulty="Easy",
        accepted=True,
        accepted_at="2026-07-22T13:00:00",
        rehearsals=[{"song_id": "song-1", "passed": True}],
    )
    path = store.write(profile)
    store.pin("Easy", path.name)

    updated = store.update_settings(
        path.name,
        target_fps=60,
        timing_offset_ms=-8,
        frame_timeout_ms=160,
        playfield_timeout_ms=1600,
        modified_at="2026-07-22T23:59:00",
    )

    assert updated["accepted"] is False
    assert updated["accepted_at"] == profile["accepted_at"]
    assert updated["invalidated_reason"] == "manual_edit"
    assert updated["modified_at"] == "2026-07-22T23:59:00"
    assert updated["rehearsals"] == profile["rehearsals"]
    assert store.pinned_profile("Easy") == path.name


def test_profile_path_cannot_escape_local_directory(tmp_path):
    store = RealtimeProfileStore(tmp_path / "profiles")

    with pytest.raises(ValueError, match="profiles 目录"):
        store.load("../secret.json")


def test_accept_latest_atomically_activates_matching_draft(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    path = store.write(payload(accepted=False))

    accepted = store.accept_latest(
        difficulty="Easy",
        current_signature=SIGNATURE,
        accepted_at="2026-07-22T15:00:00",
    )

    assert accepted == path
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["accepted"] is True
    assert data["accepted_at"] == "2026-07-22T15:00:00"
    assert not list(tmp_path.glob("*.tmp"))


def test_accept_latest_rejects_environment_drift(tmp_path):
    store = RealtimeProfileStore(tmp_path)
    store.write(payload(accepted=False))

    with pytest.raises(ValueError, match="当前环境不匹配"):
        store.accept_latest(
            difficulty="Easy",
            current_signature=EnvironmentSignature((1280, 720), 320, 60, "standard", 2.0),
        )
