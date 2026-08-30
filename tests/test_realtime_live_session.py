from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import timezone

import pytest

from agent.realtime.live_session import (
    current_live_run,
    current_song_id,
    reset_live_run,
    update_live_run,
)
from agent.realtime.song_identity import UNKNOWN_SONG_ID


def test_reset_starts_a_fresh_immutable_utc_live_run():
    previous = reset_live_run(mode="formal", difficulty="Expert")
    current = reset_live_run(mode="formal", difficulty="Expert")

    assert current_live_run() is current
    assert current.run_id != previous.run_id
    assert current.started_at.tzinfo == timezone.utc
    assert current.song_id == UNKNOWN_SONG_ID
    assert current.song_id_method == "unknown"
    with pytest.raises(FrozenInstanceError):
        current.song_id = "changed"


def test_update_replaces_the_context_without_mutating_existing_references():
    initial = reset_live_run(mode="rehearsal", difficulty="Hard")

    updated = update_live_run(
        song_id="song-phash-v1-0123456789abcdef",
        song_id_method="song-phash-v1",
        actual_note_speed=5.0,
    )

    assert initial.song_id == UNKNOWN_SONG_ID
    assert current_live_run() is updated
    assert current_song_id() == "song-phash-v1-0123456789abcdef"
    assert updated.run_id == initial.run_id
    assert updated.actual_note_speed == 5.0


def test_live_run_mapping_is_ready_for_json_artifacts():
    current = reset_live_run(
        mode="visual-evaluation",
        difficulty="Expert",
        profile_name="expert.json",
        expected_note_speed=5.0,
        note_skin_type=3,
        tap_effect=1,
        judgement_assist=False,
        debug_recording=True,
    )

    payload = current.to_mapping()

    assert payload["started_at"].endswith("Z")
    assert payload["song_id"] == UNKNOWN_SONG_ID
    assert payload["mode"] == "visual-evaluation"
    assert payload["settings"] == {
        "expected_note_speed": 5.0,
        "actual_note_speed": None,
        "note_skin_type": 3,
        "tap_effect": 1,
        "judgement_assist": False,
    }
    assert payload["debug_recording"] is True
    assert payload["recording_path"] is None
