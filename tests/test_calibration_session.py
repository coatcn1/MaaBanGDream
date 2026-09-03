from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.realtime.calibration_session import (
    CalibrationSessionStore,
    FORMAL_STAGE,
    REHEARSAL_STAGES,
)
from agent.realtime.profile_store import EnvironmentSignature, RealtimeProfileStore


def signature(*, note_speed: float = 5.0) -> EnvironmentSignature:
    return EnvironmentSignature(
        (1280, 720), 240, 60, "standard", note_speed, 1, 1, True,
    )


def result(song: str, *, miss: int = 0, completed: bool = True,
           survived: bool = True, valid: bool = True) -> dict:
    return {
        "valid": valid,
        "song_id": song,
        "perfect": 300 - miss,
        "great": 0,
        "good": 0,
        "bad": 0,
        "miss": miss,
        "fast": 0,
        "slow": 0,
        "confidence": 1.0,
        "completed": completed,
        "survived": survived,
    }


def create_store(tmp_path: Path) -> CalibrationSessionStore:
    profiles = RealtimeProfileStore(tmp_path / "profiles")
    return CalibrationSessionStore(tmp_path / "sessions", profiles)


def test_formal_life_failure_rejects_session_without_pause(tmp_path):
    store = create_store(tmp_path)
    session = store.start(
        difficulty="Expert",
        song_mode="random",
        environment=signature(),
        initial_offset_ms=10,
    )
    session = store.begin_round(
        session,
        REHEARSAL_STAGES[0],
        10,
        report_path=str(tmp_path / "rehearsal.json"),
    )
    session = store.finish_round(
        session,
        REHEARSAL_STAGES[0],
        result("song-a"),
        suggested_offset_ms=10,
    )
    assert session["next_stage"] == FORMAL_STAGE
    session = store.begin_round(
        session,
        FORMAL_STAGE,
        10,
        report_path=str(tmp_path / "round.json"),
    )
    session = store.finish_round(
        session,
        FORMAL_STAGE,
        {
            "valid": False,
            "completed": False,
            "life_failed": True,
            "result_status": "life_failed",
        },
        suggested_offset_ms=10,
        termination_reason="演出失败：生命值归零",
    )
    attempt = session["attempts"][-1]
    assert attempt["status"] == "life-failed"
    assert session["status"] == "rejected"
    assert session["terminal_reason"] == "formal_validation_life_failed"
    assert session["next_stage"] is None


def test_new_session_immediately_creates_unaccepted_candidate(tmp_path):
    store = create_store(tmp_path)
    session = store.start(
        difficulty="Hard",
        song_mode="current",
        environment=signature(),
        initial_offset_ms=7,
        current_song_id="song-a",
    )

    candidate = RealtimeProfileStore(tmp_path / "profiles").load(
        session["candidate_profile"],
    )
    assert candidate["accepted"] is False
    assert session["next_stage"] == REHEARSAL_STAGES[0]
    assert session["status"] == "active"


def test_each_round_begin_and_end_is_atomically_persisted(tmp_path):
    store = create_store(tmp_path)
    session = store.start(
        difficulty="Hard", song_mode="current",
        environment=signature(), initial_offset_ms=0,
        current_song_id="song-a",
    )
    session = store.begin_round(session, REHEARSAL_STAGES[0], 0)
    on_disk = json.loads(Path(session["_path"]).read_text(encoding="utf-8"))
    assert on_disk["attempts"][-1]["status"] == "running"

    session = store.finish_round(
        session, REHEARSAL_STAGES[0], result("song-a"),
        suggested_offset_ms=4,
    )
    on_disk = json.loads(Path(session["_path"]).read_text(encoding="utf-8"))
    assert on_disk["attempts"][-1]["status"] == "completed"
    assert on_disk["next_stage"] == FORMAL_STAGE


def test_technical_failure_pauses_without_consuming_or_adding_a_round(tmp_path):
    store = create_store(tmp_path)
    session = store.start(
        difficulty="Hard", song_mode="current",
        environment=signature(), initial_offset_ms=0,
        current_song_id="song-a",
    )
    session = store.begin_round(session, REHEARSAL_STAGES[0], 0)
    session = store.finish_round(
        session, REHEARSAL_STAGES[0],
        {"valid": False, "technical_reason": "result numbers unstable"},
        suggested_offset_ms=0,
    )

    assert session["status"] == "paused"
    assert session["next_stage"] == REHEARSAL_STAGES[0]
    resumed = store.start(
        difficulty="Hard", song_mode="current",
        environment=signature(), initial_offset_ms=0,
        current_song_id="song-a", resume_mode="auto",
    )
    assert resumed["session_id"] == session["session_id"]
    assert resumed["next_stage"] == REHEARSAL_STAGES[0]


def test_resume_after_rehearsal_starts_at_formal(tmp_path):
    store = create_store(tmp_path)
    session = store.start(
        difficulty="Hard", song_mode="current",
        environment=signature(), initial_offset_ms=0,
        current_song_id="song-a",
    )
    session = store.begin_round(session, REHEARSAL_STAGES[0], 0)
    session = store.finish_round(
        session, REHEARSAL_STAGES[0], result("song-a"),
        suggested_offset_ms=1,
    )

    resumed = store.start(
        difficulty="Hard", song_mode="current",
        environment=signature(), initial_offset_ms=0,
        current_song_id="song-a", resume_mode="auto",
    )
    assert resumed["next_stage"] == FORMAL_STAGE


def test_restart_preserves_old_session_and_creates_new_candidate(tmp_path):
    store = create_store(tmp_path)
    old = store.start(
        difficulty="Hard", song_mode="current",
        environment=signature(), initial_offset_ms=0,
        current_song_id="song-a",
    )
    new = store.start(
        difficulty="Hard", song_mode="current",
        environment=signature(), initial_offset_ms=0,
        current_song_id="song-a", resume_mode="restart",
    )

    assert new["session_id"] != old["session_id"]
    old_disk = json.loads(Path(old["_path"]).read_text(encoding="utf-8"))
    assert old_disk["status"] == "superseded"
    assert old_disk["candidate_profile"] != new["candidate_profile"]


@pytest.mark.parametrize("miss,accepted", [(9, True), (10, False)])
def test_formal_acceptance_boundary_is_miss_less_than_ten(
    tmp_path, miss, accepted,
):
    store = create_store(tmp_path)
    session = store.start(
        difficulty="Hard", song_mode="current",
        environment=signature(), initial_offset_ms=0,
        current_song_id="song-a",
    )
    for stage in REHEARSAL_STAGES:
        session = store.begin_round(session, stage, 0)
        session = store.finish_round(
            session, stage, result("song-a"), suggested_offset_ms=0,
        )
    assert session["next_stage"] == FORMAL_STAGE

    session = store.begin_round(session, FORMAL_STAGE, 0)
    session = store.finish_round(
        session, FORMAL_STAGE, result("song-a", miss=miss),
        suggested_offset_ms=0,
    )
    profile = RealtimeProfileStore(tmp_path / "profiles").load(
        session["candidate_profile"],
    )
    assert profile["accepted"] is accepted
    assert session["status"] == ("accepted" if accepted else "rejected")
    assert session["next_stage"] is None


def test_incompatible_environment_creates_new_session_without_overwrite(tmp_path):
    store = create_store(tmp_path)
    old = store.start(
        difficulty="Hard", song_mode="current",
        environment=signature(note_speed=5.0), initial_offset_ms=0,
        current_song_id="song-a",
    )
    new = store.start(
        difficulty="Hard", song_mode="current",
        environment=signature(note_speed=5.5), initial_offset_ms=0,
        current_song_id="song-a", resume_mode="auto",
    )
    assert new["session_id"] != old["session_id"]
    assert Path(old["_path"]).exists()


def test_random_session_tracks_used_songs(tmp_path):
    store = create_store(tmp_path)
    session = store.start(
        difficulty="Hard", song_mode="random",
        environment=signature(), initial_offset_ms=0,
    )
    session = store.begin_round(session, REHEARSAL_STAGES[0], 0)
    session = store.finish_round(
        session, REHEARSAL_STAGES[0], result("song-a"),
        suggested_offset_ms=0,
    )
    assert session["used_song_ids"] == ["song-a"]
