from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from agent.realtime import medley_action
from agent.realtime.medley_action import (
    MedleyFlow,
    MedleyLiveFlow,
    MedleySessionStore,
    MedleySong,
    _same_speed,
    configure_medley_settings,
    detect_medley_stage,
    lineup_matches,
    outer_task_id_from_argv,
    session_matches_settings,
    session_matches_outer_task,
    song_identity_matches,
)


def song(
    index: int,
    *,
    digest: str | None = None,
    difficulty: str = "Expert",
    level: int = 26,
    note_speed: float = 5.0,
    bestdori_song_id: int | None = None,
) -> MedleySong:
    digest = digest or f"{index:016x}"
    return MedleySong(
        index=index,
        requested_difficulty=difficulty,
        difficulty=difficulty,
        song_id=f"song-jacket-phash-v2-{digest}",
        song_id_method="song-jacket-phash-v2",
        bestdori_song_id=bestdori_song_id,
        title=f"Song {index}",
        title_confidence=0.95,
        level=level,
        expected_notes=100 + index,
        profile=f"profile-{index}.json",
        note_speed=note_speed,
    )


def stage_frame(stage: int | None) -> np.ndarray:
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    if stage is not None:
        image[110:149, 20:1258] = (90, 90, 90)
        x, y = medley_action.STAGE_POINTS[stage - 1]
        cv2.fillConvexPoly(
            image,
            np.array([[x - 18, y - 10], [x + 18, y - 10], [x, y + 14]]),
            (0, 0, 255),
        )
    return image


def install_result_cadence(flow: MedleyFlow, events: list[object]) -> None:
    back_next = [False]

    def step(_phase: str) -> str:
        if back_next[0]:
            events.append("back")
            action = "BACK"
        else:
            events.append(medley_action.RESULT_ANIMATION_SKIP_POINT)
            action = "最右下角"
        back_next[0] = not back_next[0]
        return action

    flow.result_cadence_step = step


def test_segmented_settings_merge_without_overwriting_other_choices():
    configure_medley_settings({
        "reset": True,
        "tour_type": "free",
    })
    configure_medley_settings({"song_mode": "current"})
    settings = configure_medley_settings({
        "difficulty": "Special",
        "count": 3,
        "debug_recording": True,
    })

    assert settings == {
        "tour_type": "free",
        "song_mode": "current",
        "difficulty": "Special",
        "count": 3,
        "debug_recording": True,
        "diagnostic_trace": True,
    }
    with pytest.raises(ValueError, match="巡演类型"):
        configure_medley_settings({"tour_type": "invalid"})
    settings = configure_medley_settings({"count": 6})
    assert settings["count"] == 6
    with pytest.raises(ValueError, match="3 的倍数"):
        configure_medley_settings({"count": 2})
    configure_medley_settings({"reset": True})


def test_nested_action_arguments_keep_stable_task_node_name():
    argv = MedleyFlow.action_argv({"phase": "start"})
    assert argv.node_name == "MedleyFlow"


@pytest.mark.parametrize("stage", [1, 2, 3])
def test_stage_detector_reads_active_song_marker(stage):
    assert detect_medley_stage(stage_frame(stage)) == stage


def test_stage_detector_rejects_page_without_marker():
    assert detect_medley_stage(stage_frame(None)) is None


def test_profile_speeds_must_match_for_all_three_songs():
    assert _same_speed((song(1), song(2), song(3))) == 5.0
    with pytest.raises(RuntimeError, match="流速不一致"):
        _same_speed((song(1), song(2, note_speed=4.5), song(3)))


def test_home_speed_must_match_all_profiles_before_group_starts():
    flow = object.__new__(MedleyFlow)
    flow.home_verified_speed = 5.0
    songs = (song(1), song(2), song(3))
    assert flow.validate_home_speed(songs, enabled=True) == 5.0
    flow.home_verified_speed = 4.5
    with pytest.raises(RuntimeError, match="主页实际复核流速"):
        flow.validate_home_speed(songs, enabled=True)


def test_resume_restores_only_same_session_speed(monkeypatch):
    calls = []
    flow = object.__new__(MedleyFlow)
    monkeypatch.setattr(
        medley_action,
        "publish_verified_performance_settings",
        lambda **kwargs: calls.append(("receipt", kwargs)),
    )
    monkeypatch.setattr(
        medley_action,
        "activate_speed_settings_target",
        lambda target: calls.append(("target", target)),
    )

    flow.restore_session_speed(
        {"speed_verified": True, "note_speed": 5.0},
        song(2),
    )

    assert calls[0][1]["difficulty"] == "Expert"
    assert calls[0][1]["profile"] == "profile-2.json"
    assert calls[1][1]["note_speed"] == 5.0
    with pytest.raises(RuntimeError, match="流速凭据"):
        flow.restore_session_speed(
            {"speed_verified": False, "note_speed": 5.0},
            song(2),
        )


def test_session_store_is_atomic_and_preserves_old_profile_layout(tmp_path):
    store = MedleySessionStore(tmp_path / "profiles" / "medley-sessions")
    settings = {
        "tour_type": "free",
        "song_mode": "random",
        "difficulty": "Expert",
    }
    session = store.start(
        settings=settings,
        songs=(song(1), song(2), song(3)),
        outer_task_id=101,
    )
    store.update(session, completed_songs=1, stage="ready-2")

    loaded = store.latest("free")
    assert loaded is not None
    assert loaded["completed_songs"] == 1
    assert loaded["stage"] == "ready-2"
    assert loaded["round_index"] == 1
    assert loaded["completed_before_round"] == 0
    assert loaded["outer_task_id"] == 101
    assert not list(store.root.glob("*.tmp"))
    assert not (tmp_path / "profiles" / "calibration-sessions").exists()


def test_session_resume_requires_matching_free_options():
    session = {
        "outer_task_id": 101,
        "tour_type": "free",
        "song_mode": "random",
        "requested_difficulty": "Expert",
    }
    assert session_matches_settings(session, {
        "tour_type": "free",
        "song_mode": "random",
        "difficulty": "Expert",
    })
    assert not session_matches_settings(session, {
        "tour_type": "free",
        "song_mode": "current",
        "difficulty": "Expert",
    })
    assert session_matches_settings(
        {"tour_type": "task"},
        {"tour_type": "task", "song_mode": "current", "difficulty": "Easy"},
    )


def test_session_resume_requires_same_outer_task_id_and_rejects_legacy():
    assert session_matches_outer_task({"outer_task_id": 88}, 88)
    assert not session_matches_outer_task({"outer_task_id": 88}, 89)
    assert not session_matches_outer_task({}, 88)


def test_outer_task_id_requires_real_task_detail_id():
    assert outer_task_id_from_argv(
        SimpleNamespace(task_detail=SimpleNamespace(task_id=456))
    ) == 456
    with pytest.raises(RuntimeError, match="task_id"):
        outer_task_id_from_argv(SimpleNamespace(task_detail=None))


def test_song_identity_requires_difficulty_level_and_song_match():
    expected = song(1, bestdori_song_id=186)
    assert song_identity_matches(
        expected,
        replace(expected, song_id="song-jacket-phash-v2-ffffffffffffffff"),
    )
    assert not song_identity_matches(
        expected,
        replace(expected, bestdori_song_id=395),
    )
    assert not song_identity_matches(
        expected,
        replace(expected, level=27),
    )
    assert lineup_matches(
        (song(1), song(2), song(3)),
        (song(1), song(2), song(3)),
    )
    assert not lineup_matches(
        (song(1), song(2), song(3)),
        (song(1), song(2, level=27), song(3)),
    )


def test_selection_defers_missing_title_when_cover_level_confirm_chart(
    monkeypatch,
):
    selection = SimpleNamespace(
        fingerprints=("song-jacket-phash-v2-0000000000000001",),
        bestdori_song_id=125,
        title="天下トーイツ A to Z☆",
        expected_notes=713,
    )

    class Repository:
        def resolve(self, song_id, difficulty, *, level, title):
            assert song_id == selection.fingerprints[0]
            assert difficulty == "Expert"
            assert level == 26
            assert title is None
            return SimpleNamespace(
                selection=selection,
                reason="confirmed local chart",
            )

    monkeypatch.setattr(
        medley_action,
        "_resolve_profile",
        lambda *_args, **_kwargs: ("expert.json", 5.0),
    )

    observed = medley_action.build_medley_song(
        index=1,
        requested_difficulty="Expert",
        difficulty="Expert",
        identity=SimpleNamespace(
            song_id=selection.fingerprints[0],
            method="song-jacket-phash-v2",
        ),
        level=26,
        title_reading=None,
        image=np.zeros((720, 1280, 3), dtype=np.uint8),
        repository=Repository(),
        profile_store=object(),
    )

    assert observed.bestdori_song_id == 125
    assert observed.title == "天下トーイツ A to Z☆"
    assert observed.title_confidence == 0.0


def test_free_current_song_selects_only_difficulty_and_allows_duplicate_slots(
    monkeypatch,
):
    difficulty_params = {}
    flow = object.__new__(MedleyFlow)
    flow.settings = {
        "song_mode": "current",
        "difficulty": "Expert",
        "debug_recording": False,
    }
    flow.context = object()
    flow.repository = object()
    flow.profile_store = object()
    flow.click = lambda _point: None
    flow.wait = lambda _seconds: None
    flow.capture = lambda: np.zeros((720, 1280, 3), dtype=np.uint8)
    flow.open_free_song = lambda _index: flow.capture()

    class Difficulty:
        def run(self, _context, argv):
            difficulty_params.update(
                medley_action.parse_custom_action_params(
                    argv.custom_action_param
                )
            )
            medley_action.reset_live_run(
                mode="medley",
                difficulty="Expert",
                requested_difficulty="Expert",
                prepared_for_play=False,
            )
            return True

    monkeypatch.setattr(medley_action, "RealtimeDifficultySelect", Difficulty)
    monkeypatch.setattr(
        medley_action,
        "_resolve_profile",
        lambda *_args, **_kwargs: ("expert.json", 5.0),
    )

    first = flow.snapshot_free_song(1, ())
    assert first.index == 1
    assert first.song_id == medley_action.UNKNOWN_SONG_ID
    assert first.bestdori_song_id is None
    assert difficulty_params["identity_read"] is False
    assert "song_title_roi" not in difficulty_params
    second = flow.snapshot_free_song(2, (first,))
    assert second.index == 2
    assert second.song_id == medley_action.UNKNOWN_SONG_ID


@pytest.mark.parametrize("opens_on", [1, 2, 3])
def test_open_free_song_retries_until_selection_page_is_confirmed(monkeypatch, opens_on):
    overview = np.zeros((720, 1280, 3), dtype=np.uint8)
    selection = np.ones_like(overview)
    clicks = []
    clock = [0.0]
    flow = object.__new__(MedleyFlow)
    flow.click = clicks.append
    flow.capture = lambda: selection if len(clicks) >= opens_on else overview
    flow.wait = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    monkeypatch.setattr(medley_action.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(medley_action, "song_selection_visible", lambda image: image is selection, raising=False)

    assert flow.open_free_song(2) is selection
    assert clicks == [medley_action.FREE_SLOT_POINTS[1]] * opens_on


@pytest.mark.parametrize("delivery_time", [1.5, 3.0])
def test_open_free_song_does_not_click_again_after_delayed_page_delivery(monkeypatch, delivery_time):
    overview = np.zeros((720, 1280, 3), dtype=np.uint8)
    selection = np.ones_like(overview)
    clicks = []
    clock = [0.0]
    flow = object.__new__(MedleyFlow)
    flow.click = clicks.append
    flow.capture = lambda: selection if clock[0] >= delivery_time else overview
    flow.wait = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    monkeypatch.setattr(medley_action.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(medley_action, "song_selection_visible", lambda image: image is selection, raising=False)

    assert flow.open_free_song(1) is selection
    assert clicks == [medley_action.FREE_SLOT_POINTS[0]]


def test_unopened_free_song_never_sends_random_or_difficulty_input(monkeypatch):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    clock = [0.0]
    clicks = []
    evidence = []
    flow = object.__new__(MedleyFlow)
    flow.settings = {"song_mode": "random", "difficulty": "Hard", "debug_recording": False}
    flow.click = clicks.append
    flow.capture = lambda: image
    flow.wait = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    flow.save_debug_image = lambda name, _image: evidence.append(name)
    monkeypatch.setattr(medley_action.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(medley_action, "song_selection_visible", lambda _image: False, raising=False)
    monkeypatch.setattr(
        medley_action, "RealtimeDifficultySelect",
        lambda: (_ for _ in ()).throw(AssertionError("总览页不能执行难度点击")),
    )

    with pytest.raises(RuntimeError, match="歌曲选择页"):
        flow.snapshot_free_song(1, ())

    assert clicks == [medley_action.FREE_SLOT_POINTS[0]] * 3
    assert len(evidence) == 1


def test_open_free_song_stopping_never_retries_or_saves_business_failure(monkeypatch):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    clicks = []
    flow = object.__new__(MedleyFlow)
    flow.capture = lambda: image
    flow.click = clicks.append
    flow.wait = lambda _seconds: (_ for _ in ()).throw(
        medley_action.ScreenRefreshCancelled("task is stopping")
    )
    flow.save_debug_image = lambda *_args: (_ for _ in ()).throw(
        AssertionError("用户停止不能记录业务失败")
    )
    monkeypatch.setattr(medley_action, "song_selection_visible", lambda _image: False)

    with pytest.raises(medley_action.ScreenRefreshCancelled):
        flow.open_free_song(1)

    assert clicks == [medley_action.FREE_SLOT_POINTS[0]]


def test_preparation_title_replaces_deferred_selection_title(monkeypatch):
    expected = replace(
        song(1, bestdori_song_id=125),
        requested_difficulty="Special",
        title="天下トーイツ A to Z☆",
        title_confidence=0.0,
    )
    observed = replace(
        song(1, bestdori_song_id=125),
        title="天下トーイツ A to Z☆",
        title_confidence=0.91,
    )
    flow = object.__new__(MedleyFlow)
    flow.read_preparation_song = lambda _index, _image, **_kwargs: observed
    monkeypatch.setattr(medley_action, "detect_medley_stage", lambda _image: 1)

    confirmed = flow.confirm_preparation(
        expected,
        np.zeros((720, 1280, 3), dtype=np.uint8),
    )

    assert confirmed.requested_difficulty == "Special"
    assert confirmed.bestdori_song_id == 125
    assert confirmed.title_confidence == pytest.approx(0.91)


def test_preparation_identity_fills_an_unidentified_free_slot(monkeypatch):
    expected = replace(
        song(1),
        song_id=medley_action.UNKNOWN_SONG_ID,
        song_id_method="unknown",
        bestdori_song_id=None,
        title="",
        title_confidence=0.0,
        level=0,
        expected_notes=None,
    )
    observed = replace(
        song(1, bestdori_song_id=125),
        title="天下トーイツ A to Z☆",
        title_confidence=0.91,
    )
    flow = object.__new__(MedleyFlow)
    flow.read_preparation_song = lambda _index, _image, **_kwargs: observed
    monkeypatch.setattr(medley_action, "detect_medley_stage", lambda _image: 1)

    confirmed = flow.confirm_preparation(
        expected,
        np.zeros((720, 1280, 3), dtype=np.uint8),
    )

    assert confirmed.bestdori_song_id == 125
    assert confirmed.song_id == observed.song_id
    assert confirmed.title_confidence == pytest.approx(0.91)
    assert confirmed.profile == expected.profile
    assert confirmed.note_speed == expected.note_speed


def test_preparation_missing_identity_preserves_known_song_for_final_cover(
    monkeypatch,
):
    expected = song(1, bestdori_song_id=125)
    observed = replace(
        expected,
        song_id=medley_action.UNKNOWN_SONG_ID,
        song_id_method="unknown",
        bestdori_song_id=None,
        title="",
        title_confidence=0.0,
        expected_notes=None,
    )
    flow = object.__new__(MedleyFlow)
    flow.read_preparation_song = lambda _index, _image, **_kwargs: observed
    monkeypatch.setattr(medley_action, "detect_medley_stage", lambda _image: 1)

    confirmed = flow.confirm_preparation(
        expected,
        np.zeros((720, 1280, 3), dtype=np.uint8),
    )

    assert confirmed.song_id == expected.song_id
    assert confirmed.bestdori_song_id == 125
    assert confirmed.title == expected.title


def test_medley_preparation_missing_level_defers_to_final_cover(monkeypatch):
    monkeypatch.setattr(
        medley_action,
        "_resolve_profile",
        lambda *_args, **_kwargs: ("expert.json", 5.0),
    )

    class Repository:
        def resolve(self, *_args, **_kwargs):
            raise AssertionError("缺等级时不得用旧封面预解析谱面")

    deferred = medley_action.build_medley_song(
        index=1,
        requested_difficulty="Expert",
        difficulty="Expert",
        identity=SimpleNamespace(
            song_id="song-jacket-phash-v2-1111111111111111",
            method="song-jacket-phash-v2",
        ),
        level=None,
        title_reading=None,
        image=np.zeros((720, 1280, 3), dtype=np.uint8),
        repository=Repository(),
        profile_store=object(),
        allow_deferred_identity=True,
        title_source="preparation",
    )

    assert deferred.preparation_identity_pending_final_cover is True
    assert deferred.song_id == medley_action.UNKNOWN_SONG_ID
    assert deferred.level == 0


def test_medley_preparation_identity_resolution_error_defers_to_final_cover(
    monkeypatch,
):
    monkeypatch.setattr(
        medley_action,
        "_resolve_profile",
        lambda *_args, **_kwargs: ("expert.json", 5.0),
    )

    class Repository:
        def resolve(self, *_args, **_kwargs):
            return SimpleNamespace(selection=None, reason="cover conflicts")

        def identify_by_cover_title(self, *_args, **_kwargs):
            return SimpleNamespace(identity=None, reason="title conflicts")

    deferred = medley_action.build_medley_song(
        index=1,
        requested_difficulty="Expert",
        difficulty="Expert",
        identity=SimpleNamespace(
            song_id="song-jacket-phash-v2-1111111111111111",
            method="song-jacket-phash-v2",
        ),
        level=27,
        title_reading=SimpleNamespace(text="冲突标题", confidence=0.99),
        image=np.zeros((720, 1280, 3), dtype=np.uint8),
        repository=Repository(),
        profile_store=object(),
        allow_deferred_identity=True,
        title_source="preparation",
    )

    assert deferred.preparation_identity_pending_final_cover is True
    assert deferred.song_id == medley_action.UNKNOWN_SONG_ID
    assert deferred.bestdori_song_id is None


def test_medley_build_confirm_activate_keeps_pending_identity_isolated(
    monkeypatch,
):
    monkeypatch.setattr(
        medley_action,
        "_resolve_profile",
        lambda *_args, **_kwargs: ("expert.json", 5.0),
    )

    class Repository:
        def resolve(self, *_args, **_kwargs):
            return SimpleNamespace(selection=None, reason="cover conflicts")

        def identify_by_cover_title(self, *_args, **_kwargs):
            return SimpleNamespace(identity=None, reason="title conflicts")

    observed = medley_action.build_medley_song(
        index=1,
        requested_difficulty="Expert",
        difficulty="Expert",
        identity=SimpleNamespace(
            song_id="song-jacket-phash-v2-1111111111111111",
            method="song-jacket-phash-v2",
        ),
        level=27,
        title_reading=SimpleNamespace(text="高置信冲突标题", confidence=0.99),
        image=np.zeros((720, 1280, 3), dtype=np.uint8),
        repository=Repository(),
        profile_store=object(),
        allow_deferred_identity=True,
        title_source="preparation",
    )
    flow = object.__new__(MedleyFlow)
    flow.settings = {"debug_recording": False}
    flow.read_preparation_song = lambda _index, _image, **_kwargs: observed
    monkeypatch.setattr(medley_action, "detect_medley_stage", lambda _image: 1)

    confirmed = flow.confirm_preparation(
        song(1, bestdori_song_id=125),
        np.zeros((720, 1280, 3), dtype=np.uint8),
    )
    flow.activate_song(confirmed)

    run = medley_action.current_live_run()
    assert confirmed.preparation_identity_pending_final_cover is True
    assert confirmed.song_id == medley_action.UNKNOWN_SONG_ID
    assert run is not None
    assert run.preparation_identity_pending_final_cover is True
    assert run.song_id == medley_action.UNKNOWN_SONG_ID


def test_medley_preparation_identity_conflict_defers_to_final_cover(monkeypatch):
    expected = song(1, digest="1111111111111111", bestdori_song_id=125)
    observed = song(
        1, digest="2222222222222222", bestdori_song_id=126, level=27,
    )
    flow = object.__new__(MedleyFlow)
    flow.read_preparation_song = lambda _index, _image, **_kwargs: observed
    monkeypatch.setattr(medley_action, "detect_medley_stage", lambda _image: 1)

    deferred = flow.confirm_preparation(
        expected, np.zeros((720, 1280, 3), dtype=np.uint8),
    )

    assert deferred.preparation_identity_pending_final_cover is True
    assert deferred.song_id == medley_action.UNKNOWN_SONG_ID
    assert deferred.bestdori_song_id is None
    assert deferred.title_confidence == 0.0
    assert deferred.level == 27


def test_medley_final_identity_restores_verified_cn_level_without_rejecting_cover():
    pending = replace(
        song(1, bestdori_song_id=None, level=27),
        song_id=medley_action.UNKNOWN_SONG_ID,
        title="",
        title_confidence=0.0,
        preparation_identity_pending_final_cover=True,
    )
    medley_action.reset_live_run(
        mode="medley", difficulty="Expert", prepared_for_play=True,
    )
    medley_action.update_live_run(
        song_id="song-jacket-phash-v2-f479f8f8f4f05220",
        song_id_method="song-jacket-phash-v2",
        song_title="蒼穹へのトレイル",
        song_title_confidence=0.96,
        final_cover_confirmed=True,
    )
    global_selection = SimpleNamespace(
        bestdori_song_id=581, title="蒼穹へのトレイル",
        expected_notes=785, level=26,
    )
    cn_selection = SimpleNamespace(
        bestdori_song_id=581, title="蒼穹へのトレイル",
        expected_notes=785, level=27,
    )

    class Repository:
        def resolve(self, *_args, **kwargs):
            return SimpleNamespace(
                selection=(
                    global_selection if kwargs["level"] is None else cn_selection
                ),
                reason="confirmed",
            )

    flow = object.__new__(MedleyFlow)
    flow.repository = Repository()
    completed = flow.complete_final_cover_identity(pending)

    assert completed.level == 27
    assert completed.preparation_identity_pending_final_cover is False
    assert medley_action.current_live_run().song_level == 27


def test_play_requires_final_cover_title_when_earlier_reads_failed(monkeypatch):
    deferred = replace(
        song(1, bestdori_song_id=125),
        title="天下トーイツ A to Z☆",
        title_confidence=0.0,
    )
    flow = object.__new__(MedleyFlow)
    flow.settings = {"debug_recording": False, "diagnostic_trace": True}
    flow.context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False),
    )
    flow.run_preflight = lambda _song, _image: (deferred, False)
    flow.click = lambda _point: None
    flow.handle_pre_live_confirm = lambda: None

    class Sessions:
        @staticmethod
        def update_song(session, _song):
            return session

        @staticmethod
        def update(session, **changes):
            session.update(changes)
            return session

    flow.sessions = Sessions()
    medley_action.reset_live_run(
        mode="medley",
        difficulty="Expert",
        requested_difficulty="Expert",
        prepared_for_play=True,
    )
    captured = {}

    class ProfilePlay:
        def run(self, _context, argv):
            captured.update(
                medley_action.parse_custom_action_params(
                    argv.custom_action_param
                )
            )
            medley_action.update_live_run(
                song_id="song-jacket-phash-v2-0000000000000001",
                song_id_method="song-jacket-phash-v2",
                song_title="天下トーイツ A to Z☆",
                song_title_confidence=0.96,
                final_cover_confirmed=True,
                final_cover_song_id="song-jacket-phash-v2-0000000000000001",
            )
            return True

    monkeypatch.setattr(medley_action, "RealtimeProfilePlay", ProfilePlay)
    flow.repository = SimpleNamespace(resolve=lambda *_args, **_kwargs: SimpleNamespace(
        selection=SimpleNamespace(
            fingerprints=("song-jacket-phash-v2-0000000000000001",),
            bestdori_song_id=125,
            title="天下トーイツ A to Z☆",
            expected_notes=713,
        ),
        reason="confirmed local chart",
    ))

    session, updated = flow.play_song(
        {"session_id": "session", "songs": []},
        deferred,
        np.zeros((720, 1280, 3), dtype=np.uint8),
    )

    assert updated.title_confidence == pytest.approx(0.96)
    assert updated.observed_title == "天下トーイツ A to Z☆"
    assert updated.title_source == "final-cover"
    assert captured["require_final_cover_title"] is True
    assert captured["native_prearm_deferred"] is False
    assert session["completed_songs"] == 1


def test_play_failure_reads_life_depletion_from_engine_error_report(
    tmp_path,
    monkeypatch,
):
    """Native 完整性门禁二次分类不能掩盖已经确认的生命归零。"""
    monkeypatch.setattr(medley_action, "PROJECT_ROOT", tmp_path)
    report = tmp_path / "screencap" / "medley-session-song1.json"
    report.parent.mkdir()
    report.write_text(
        json.dumps({
            "result_status": "engine_error",
            "reason": "Native 演奏未通过完整性门禁",
            "terminal_reason": "演出失败：生命值归零",
            "native": {"game_terminal_reason": "演出失败：生命值归零"},
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    flow = object.__new__(MedleyFlow)
    flow.outer_task_id = 101
    flow.settings = {"debug_recording": False, "diagnostic_trace": False}
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    failed_song = song(1)
    flow.run_preflight = lambda _song, _image: (failed_song, False)
    flow.click = lambda _point: None
    flow.handle_pre_live_confirm = lambda: None
    flow.sessions = SimpleNamespace(
        update_song=lambda session, _song: session,
        update=lambda session, **changes: session | changes,
    )
    medley_action.reset_live_run(
        mode="medley",
        difficulty="Expert",
        requested_difficulty="Expert",
        prepared_for_play=True,
    )

    class ProfilePlay:
        def run(self, _context, _argv):
            return False

    monkeypatch.setattr(medley_action, "RealtimeProfilePlay", ProfilePlay)

    with pytest.raises(medley_action.MedleyPlayFailure) as raised:
        flow.play_song(
            {"session_id": "session", "songs": []},
            failed_song,
            np.zeros((720, 1280, 3), dtype=np.uint8),
        )

    assert raised.value.retryable is True
    assert raised.value.reason == "演出失败：生命值归零"


def test_retry_failed_round_discards_group_and_restarts_from_completed_count(
    monkeypatch,
):
    """第 7 首空血后废弃本组三首，进度仍显示已完成 6 首。"""
    flow = object.__new__(MedleyFlow)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow._play_failure_retries = 0
    flow._play_failure_retry_limit = 1
    flow._next_round_completed = 0
    flow._home_ready = False
    updates = []
    recovered = []
    restored = []
    clicks = []
    flow.sessions = SimpleNamespace(
        update=lambda session, **changes: updates.append(changes) or session | changes,
    )
    flow.recover_home = lambda **kwargs: recovered.append(kwargs)
    flow.restore_progress = lambda completed: restored.append(completed) or True
    flow.click = lambda point: clicks.append(point)
    def restart_group():
        assert flow._next_round_completed == 6
        assert flow._home_ready is True
        assert flow._play_failure_retries == 1
        # 重新选出的完整三曲都成功后，任务才可从 6 推进到 9。
        return 9

    flow.run = restart_group
    discarded = []
    monkeypatch.setattr(
        medley_action,
        "discard_prearmed_backend",
        lambda reason: discarded.append(reason) or True,
    )
    failure = medley_action.MedleyPlayFailure(
        song_index=1,
        report_path="screencap/medley-session-song1.json",
        reason="演出失败：生命值归零",
        result_status="engine_error",
        retryable=True,
    )

    assert flow.retry_failed_round(
        {"completed_before_round": 6, "completed_songs": 0},
        failure,
    ) == 9
    assert recovered == [{}]
    assert restored == [6]
    assert discarded == ["medley-play-failure-retry"]
    assert updates[-1] == {
        "status": "superseded",
        "terminal_reason": "play_failure_retry: 演出失败：生命值归零",
    }
    # 该分支只委托 CommonRecover 返回主页，绝不直接触碰星石“继续”。
    assert clicks == []


def test_retry_event_write_failure_does_not_block_home_recovery(monkeypatch):
    flow = object.__new__(MedleyFlow)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow._play_failure_retries = 0
    flow._play_failure_retry_limit = 1
    flow._next_round_completed = 0
    flow._home_ready = False
    flow.sessions = SimpleNamespace(update=lambda session, **_changes: session)
    recovered = []
    flow.recover_home = lambda **kwargs: recovered.append(kwargs)
    flow.restore_progress = lambda _completed: True
    flow.run = lambda: "restarted"
    monkeypatch.setattr(
        medley_action,
        "append_current_run_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk busy")),
    )
    failure = medley_action.MedleyPlayFailure(
        song_index=2,
        report_path="screencap/medley-session-song2.json",
        reason="演出失败：生命值归零",
        result_status="engine_error",
        retryable=True,
    )

    assert flow.retry_failed_round(
        {"completed_before_round": 6, "completed_songs": 1},
        failure,
    ) == "restarted"
    assert recovered == [{}]


@pytest.mark.parametrize("retry_limit, attempts", [(0, 0), (1, 1)])
def test_retry_failed_round_respects_configured_bound(retry_limit, attempts):
    flow = object.__new__(MedleyFlow)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow._play_failure_retries = attempts
    flow._play_failure_retry_limit = retry_limit
    flow.sessions = SimpleNamespace(update=lambda session, **_changes: session)
    restored = []
    flow.restore_progress = lambda completed: restored.append(completed) or True
    flow.recover_home = lambda: (_ for _ in ()).throw(
        AssertionError("重试耗尽时不能恢复后重开")
    )
    failure = medley_action.MedleyPlayFailure(
        song_index=1,
        report_path="screencap/medley-session-song1.json",
        reason="演出失败：生命值归零",
        result_status="engine_error",
        retryable=True,
    )

    with pytest.raises(RuntimeError, match="重试次数已耗尽"):
        flow.retry_failed_round({"completed_before_round": 6}, failure)

    assert restored == [6]


@pytest.mark.parametrize(("song_index", "completed_songs"), [(2, 1), (3, 2)])
def test_later_song_failure_rewinds_group_progress_before_retry(
    song_index,
    completed_songs,
):
    flow = object.__new__(MedleyFlow)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow._play_failure_retries = 0
    flow._play_failure_retry_limit = 1
    flow._next_round_completed = 0
    flow._home_ready = False
    flow.sessions = SimpleNamespace(update=lambda session, **_changes: session)
    restored = []
    flow.restore_progress = lambda completed: restored.append(completed) or True
    flow.recover_home = lambda **_kwargs: None
    flow.run = lambda: "restarted"
    failure = medley_action.MedleyPlayFailure(
        song_index=song_index,
        report_path=f"screencap/medley-session-song{song_index}.json",
        reason="演出失败：生命值归零",
        result_status="engine_error",
        retryable=True,
    )

    assert flow.retry_failed_round(
        {"completed_before_round": 6, "completed_songs": completed_songs},
        failure,
    ) == "restarted"
    assert restored == [6]


def test_retry_failed_round_keeps_user_stop_neutral():
    flow = object.__new__(MedleyFlow)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=True))
    flow.sessions = SimpleNamespace(update=lambda session, **_changes: session)
    flow.recover_home = lambda: (_ for _ in ()).throw(
        AssertionError("用户停止时不得继续恢复或重试")
    )
    failure = medley_action.MedleyPlayFailure(
        song_index=1,
        report_path="screencap/medley-session-song1.json",
        reason="演出失败：生命值归零",
        result_status="engine_error",
        retryable=True,
    )

    with pytest.raises(medley_action.ScreenRefreshCancelled):
        flow.retry_failed_round({"completed_before_round": 6}, failure)


def test_retry_failed_round_keeps_paused_session_when_home_recovery_fails():
    flow = object.__new__(MedleyFlow)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    flow._play_failure_retries = 0
    flow._play_failure_retry_limit = 1
    updates = []
    flow.sessions = SimpleNamespace(
        update=lambda session, **changes: updates.append(changes) or session | changes,
    )
    flow.restore_progress = lambda _completed: True
    flow.recover_home = lambda **_kwargs: (_ for _ in ()).throw(
        RuntimeError("主页不可达")
    )
    failure = medley_action.MedleyPlayFailure(
        song_index=1,
        report_path="screencap/medley-session-song1.json",
        reason="演出失败：生命值归零",
        result_status="engine_error",
        retryable=True,
    )

    with pytest.raises(RuntimeError, match="retry_recovery_failed"):
        flow.retry_failed_round({"completed_before_round": 6}, failure)

    assert updates[-1]["status"] == "paused"
    assert "主页不可达" in updates[-1]["terminal_reason"]


@pytest.mark.parametrize(
    ("payload", "latest_reason"),
    [
        ({"result_status": "preflight_error", "reason": "Profile 不匹配"}, ""),
        ({"result_status": "engine_error", "reason": "身份冲突"}, ""),
        ({"result_status": "stopped", "reason": "用户已停止任务"}, "用户已停止任务"),
    ],
)
def test_play_failure_hard_conflict_and_stop_are_not_retryable(
    tmp_path,
    payload,
    latest_reason,
):
    report = tmp_path / "failure.json"
    report.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    failure = medley_action.read_medley_play_failure(report, latest_reason)

    assert failure.retryable is False


def test_free_random_clicks_random_without_reading_identity(monkeypatch):
    flow = object.__new__(MedleyFlow)
    flow.settings = {
        "song_mode": "random",
        "difficulty": "Special",
        "debug_recording": False,
    }
    flow.context = object()
    flow.repository = object()
    flow.profile_store = object()
    clicks = []
    flow.click = clicks.append
    flow.wait = lambda _seconds: None
    flow.capture = lambda: np.zeros((720, 1280, 3), dtype=np.uint8)
    flow.open_free_song = lambda _index: flow.capture()
    calls = {}

    class Difficulty:
        def run(self, _context, argv):
            params = medley_action.parse_custom_action_params(
                argv.custom_action_param
            )
            calls["fallback"] = params["fallback_difficulties"]
            calls["identity_read"] = params["identity_read"]
            medley_action.reset_live_run(
                mode="medley",
                difficulty="Expert",
                requested_difficulty="Special",
                prepared_for_play=False,
            )
            return True

    monkeypatch.setattr(medley_action, "RealtimeDifficultySelect", Difficulty)
    monkeypatch.setattr(
        medley_action,
        "_resolve_profile",
        lambda *_args, **_kwargs: ("expert.json", 5.0),
    )

    selected = flow.snapshot_free_song(2, (song(1),))

    assert selected.difficulty == "Expert"
    assert selected.song_id == medley_action.UNKNOWN_SONG_ID
    assert medley_action.FREE_RANDOM_POINT in clicks
    assert calls["fallback"] == ["Expert"]
    assert calls["identity_read"] is False


def test_task_tour_reader_uses_three_read_only_layouts(monkeypatch):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    targets = []
    covers = []

    def fake_difficulty(_image, layout):
        targets.append(layout)
        return "Expert"

    def fake_fingerprint(cover):
        covers.append(cover.shape)
        return type("Identity", (), {
            "song_id": "song-jacket-phash-v2-0000000000000001",
            "method": "song-jacket-phash-v2",
        })()

    monkeypatch.setattr(medley_action, "selected_difficulty", fake_difficulty)
    monkeypatch.setattr(medley_action, "fingerprint_jacket", fake_fingerprint)
    monkeypatch.setattr(medley_action, "read_song_level", lambda *_args: 26)
    monkeypatch.setattr(
        medley_action,
        "recognize_song_title",
        lambda *_args: medley_action.TitleReading("Song", 0.9),
    )
    monkeypatch.setattr(
        medley_action,
        "build_medley_song",
        lambda **kwargs: song(kwargs["index"]),
    )

    songs = medley_action.read_task_tour_songs(
        image,
        repository=object(),
        profile_store=object(),
    )

    assert [item.index for item in songs] == [1, 2, 3]
    assert targets == [
        layout["targets"] for layout in medley_action.TASK_SLOT_LAYOUTS
    ]
    assert covers == [(158, 158, 3), (158, 153, 3), (158, 158, 3)]


def test_achievement_reward_overview_uses_shared_result_cycle():
    template = medley_action.imread_unicode(
        medley_action.ESC_ONLY_REWARD_TEMPLATES[0]
    )
    assert template is not None
    image = np.full((720, 1280, 3), 255, dtype=np.uint8)
    height, width = template.shape[:2]
    image[55:55 + height, 467:467 + width] = template
    events = []
    flow = object.__new__(MedleyFlow)
    flow.wait = lambda _seconds: None
    flow.capture = lambda: (_ for _ in ()).throw(
        AssertionError("曲间弹窗推进后应由外层重新截图")
    )
    flow.accelerated_result_back = lambda _phase: events.extend([
        medley_action.RESULT_ANIMATION_SKIP_POINT,
        "back",
        medley_action.RESULT_ANIMATION_SKIP_POINT,
    ])

    assert flow.dismiss_reward(image) is True
    assert events == [
        medley_action.RESULT_ANIMATION_SKIP_POINT,
        "back",
        medley_action.RESULT_ANIMATION_SKIP_POINT,
    ]


def test_medley_result_cycle_reuses_shared_accelerated_back(monkeypatch):
    events = []

    class Job:
        def wait(self):
            return self

    class Controller:
        def post_click(self, x, y):
            events.append(("click", (x, y)))
            return Job()

        def post_click_key(self, key):
            events.append(("key", key))
            return Job()

    flow = object.__new__(MedleyFlow)
    flow.context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=Controller())
    )
    monkeypatch.setattr(
        medley_action,
        "require_game_foreground",
        lambda _controller: None,
    )

    flow.accelerated_result_back("test")

    assert events == [
        ("click", medley_action.RESULT_ANIMATION_SKIP_POINT),
        ("key", 4),
        ("click", medley_action.RESULT_ANIMATION_SKIP_POINT),
    ]


def test_medley_post_result_recovery_uses_back_only_shared_cadence(monkeypatch):
    captured = []
    flow = object.__new__(MedleyFlow)
    flow.context = object()

    def run(_context, argv):
        captured.append(medley_action.json.loads(argv.custom_action_param))
        return True

    monkeypatch.setattr(
        medley_action,
        "CommonRecover",
        lambda: SimpleNamespace(run=run),
    )

    flow.recover_home(result_navigation=True)

    assert captured[0]["back_only"] is True
    assert captured[0]["click_nodes"] == []
    assert captured[0]["back_only_click_nodes"] == list(medley_action.STORY_NODES)
    assert captured[0]["back_acceleration_click_point"] == [1279, 719]
    assert "live_failed_continue_node" not in captured[0]
    assert "live_failed_exit_node" not in captured[0]
    assert "quit_confirm_exit_node" not in captured[0]


def test_medley_failed_live_recovery_uses_dedicated_two_stage_nodes(monkeypatch):
    captured = []
    flow = object.__new__(MedleyFlow)
    flow.context = object()

    def run(_context, argv):
        captured.append(medley_action.json.loads(argv.custom_action_param))
        return True

    monkeypatch.setattr(
        medley_action,
        "CommonRecover",
        lambda: SimpleNamespace(run=run),
    )

    flow.recover_home()

    assert captured[0]["live_failed_continue_node"] == (
        "MedleyLiveFailedContinue"
    )
    assert captured[0]["live_failed_exit_node"] == "MedleyLiveFailedExit"
    assert captured[0]["quit_confirm_exit_node"] == "MedleyQuitConfirmExit"


def test_medley_failed_live_pipeline_nodes_cover_full_button_templates():
    common = json.loads(
        (Path(__file__).parents[1] / "resource/pipeline/common.json").read_text(
            encoding="utf-8"
        )
    )
    expected = {
        "MedleyLiveFailedExit": ("live_failed_exit.png", (386, 408)),
        "MedleyLiveFailedContinue": ("live_failed_continue.png", (653, 414)),
        "MedleyQuitConfirmExit": ("quit_confirm_exit.png", (653, 491)),
    }
    for node_name, (template_name, (x, y)) in expected.items():
        node = common[node_name]
        assert node["template"] == template_name
        assert node["threshold"] == 0.9
        template = medley_action.imread_unicode(
            Path(__file__).parents[1] / "resource/image" / template_name
        )
        assert template is not None
        image = np.zeros((720, 1280, 3), dtype=np.uint8)
        height, width = template.shape[:2]
        image[y:y + height, x:x + width] = template
        left, top, roi_width, roi_height = node["roi"]
        roi = image[top:top + roi_height, left:left + roi_width]
        assert cv2.matchTemplate(
            roi,
            template,
            cv2.TM_CCOEFF_NORMED,
        ).max() >= node["threshold"]
    # 单人/共享节点保持旧 ROI，避免第一层“继续”误配为第二层“退出”。
    assert common["LiveFailedExit"]["roi"] == [240, 390, 280, 130]
    assert common["LiveFailedContinue"]["roi"] == [740, 390, 340, 130]
    assert common["QuitConfirmExit"]["roi"] == [600, 390, 320, 130]


def test_initial_stage_capture_dismisses_reward_overlay_before_resume():
    popup = np.zeros((8, 8, 3), dtype=np.uint8)
    popup[0, 0, 0] = 1
    stage = stage_frame(2)
    frames = iter((popup, stage))
    dismissed = []
    flow = object.__new__(MedleyFlow)
    flow.capture = lambda: next(frames)
    flow.dismiss_reward = lambda image: (
        dismissed.append(int(image[0, 0, 0])) or int(image[0, 0, 0]) == 1
    )

    image = flow.capture_after_reward_overlays()

    assert detect_medley_stage(image) == 2
    assert dismissed == [1, 0]


def test_result_advance_checks_after_each_shared_cadence_step(
    monkeypatch,
):
    before = np.zeros((720, 1280, 3), dtype=np.uint8)
    after = np.ones((720, 1280, 3), dtype=np.uint8)
    events = []
    flow = object.__new__(MedleyFlow)
    flow.wait = lambda _seconds: None
    frames = iter((after,))
    flow.capture = lambda: next(frames)
    install_result_cadence(flow, events)
    monkeypatch.setattr(
        medley_action,
        "judgement_details_visible",
        lambda image: int(image[0, 0, 0]) == 0,
    )
    flow.advance_page(before)

    assert events == [
        medley_action.RESULT_ANIMATION_SKIP_POINT,
    ]


def test_result_advance_waits_for_result_marker_to_leave(monkeypatch):
    before = np.zeros((720, 1280, 3), dtype=np.uint8)
    after = np.full((720, 1280, 3), 20, dtype=np.uint8)
    events = []
    flow = object.__new__(MedleyFlow)
    flow.wait = lambda _seconds: None
    flow.capture = lambda: after
    install_result_cadence(flow, events)
    monkeypatch.setattr(
        medley_action,
        "judgement_details_visible",
        lambda image: image is before,
    )
    flow.advance_page(before)

    assert events == [
        medley_action.RESULT_ANIMATION_SKIP_POINT,
    ]


def test_result_advance_keeps_safe_cycle_running_during_visible_transition(
    monkeypatch,
):
    before = np.zeros((720, 1280, 3), dtype=np.uint8)
    transition = np.full((720, 1280, 3), 20, dtype=np.uint8)
    departed = np.full((720, 1280, 3), 30, dtype=np.uint8)
    events = []
    flow = object.__new__(MedleyFlow)
    flow.wait = lambda _seconds: None
    frames = iter((transition, transition, departed))
    flow.capture = lambda: next(frames)
    install_result_cadence(flow, events)
    monkeypatch.setattr(
        medley_action,
        "judgement_details_visible",
        lambda image: int(image[0, 0, 0]) != 30,
    )
    flow.advance_page(before)

    assert events == [
        medley_action.RESULT_ANIMATION_SKIP_POINT,
        "back",
        medley_action.RESULT_ANIMATION_SKIP_POINT,
    ]


def test_pggbm_advance_bounds_complete_cycles(monkeypatch):
    before = np.zeros((720, 1280, 3), dtype=np.uint8)
    events = []
    flow = object.__new__(MedleyFlow)
    flow.wait = lambda _seconds: None
    flow.capture = lambda: before
    install_result_cadence(flow, events)
    monkeypatch.setattr(
        medley_action,
        "judgement_details_visible",
        lambda _image: True,
    )
    monkeypatch.setattr(
        medley_action,
        "RESULT_NAVIGATION_MAX_CYCLES",
        4,
        raising=False,
    )
    advanced = flow.advance_page(before)

    assert advanced is False
    assert events == [
        medley_action.RESULT_ANIMATION_SKIP_POINT,
        "back",
    ] * 4


def test_collect_results_only_identifies_three_pggbm_pages(
    monkeypatch,
):
    def frame(tag: int) -> np.ndarray:
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        image[0, 0, 0] = tag
        return image

    frames = iter((frame(4), frame(1), frame(4), frame(2), frame(4), frame(3)))
    flow = object.__new__(MedleyFlow)
    flow.capture = lambda: next(frames, frame(9))
    flow.wait = lambda _seconds: None
    flow.dismiss_reward = lambda _image: False
    flow.story_handled = lambda _image: False
    flow.dismiss_quit_confirm = lambda _image: False
    flow.home_or_tour_select = lambda image: int(image[0, 0, 0]) == 9
    flow.parse_stable_result = lambda _song, image: (SimpleNamespace(), image)
    advances = []
    flow.advance_page = lambda image, **_kwargs: advances.append(
        int(image[0, 0, 0])
    )
    actions = []
    install_result_cadence(flow, actions)
    flow.sessions = SimpleNamespace(
        update=lambda session, **changes: session | changes,
    )
    monkeypatch.setattr(
        medley_action,
        "judgement_details_visible",
        lambda image: int(image[0, 0, 0]) in {1, 2, 3},
    )
    saved = []
    monkeypatch.setattr(
        medley_action,
        "finalize_deferred_result",
        lambda path, *_args, **_kwargs: saved.append(path),
    )

    songs = tuple(
        replace(song(index), report_path=f"screencap/song{index}.json")
        for index in (1, 2, 3)
    )
    result = flow.collect_results(
        {"session_id": "test", "results_completed": 0},
        songs,
    )

    assert result["results_completed"] == 3
    assert saved == [
        "screencap/song1.json",
        "screencap/song2.json",
        "screencap/song3.json",
    ]
    assert advances == [1, 2]
    assert actions == [
        medley_action.RESULT_ANIMATION_SKIP_POINT,
        "back",
        medley_action.RESULT_ANIMATION_SKIP_POINT,
    ]


@pytest.mark.parametrize("difficulty", ["Easy", "Normal", "Hard", "Expert", "Special"])
def test_stable_medley_result_reads_counts_without_rechecking_song_header(
    monkeypatch, difficulty,
):
    """开演前确认的身份不能被结算标题、等级或难度 OCR 再次否决。"""
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    expected = replace(song(2, difficulty=difficulty), expected_notes=101)
    result = medley_action.LiveResult(100, 1, 0, 0, 0, 0, 1, .95)
    clock = [0.0]
    flow = object.__new__(MedleyFlow)
    flow.wait = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    flow.capture = lambda: image
    monkeypatch.setattr(medley_action.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(medley_action, "judgement_details_visible", lambda _image: True)
    monkeypatch.setattr(
        medley_action, "ResultParser",
        lambda: SimpleNamespace(parse=lambda _image: result),
    )

    def unexpected_identity_read(*_args, **_kwargs):
        raise AssertionError("PGGBM 不应重新识别歌曲标题、难度或等级")

    monkeypatch.setattr(medley_action, "recognize_song_title", unexpected_identity_read)
    monkeypatch.setattr(medley_action, "read_song_level", unexpected_identity_read)

    parsed, stable_image = flow.parse_stable_result(expected, image, timeout_seconds=3)

    assert parsed is result
    assert stable_image is image
    assert clock[0] == 1.0


def test_medley_result_stability_timer_restarts_only_when_counts_change(monkeypatch):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    before = medley_action.LiveResult(100, 1, 0, 0, 0, 0, 1, .95)
    after = medley_action.LiveResult(99, 2, 0, 0, 0, 0, 2, .95)
    clock = [0.0]
    flow = object.__new__(MedleyFlow)
    flow.wait = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    flow.capture = lambda: image
    monkeypatch.setattr(medley_action.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(medley_action, "judgement_details_visible", lambda _image: True)
    monkeypatch.setattr(
        medley_action, "ResultParser",
        lambda: SimpleNamespace(parse=lambda _image: before if clock[0] < .5 else after),
    )

    parsed, _image = flow.parse_stable_result(
        replace(song(1), expected_notes=101), image, timeout_seconds=3,
    )

    assert parsed is after
    assert clock[0] == 1.5


@pytest.mark.parametrize("interruption", ["marker-missing", "invalid-digits"])
def test_medley_result_stability_does_not_span_unreadable_frames(monkeypatch, interruption):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    result = medley_action.LiveResult(100, 1, 0, 0, 0, 0, 1, .95)
    clock = [0.0]
    flow = object.__new__(MedleyFlow)
    flow.wait = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    flow.capture = lambda: image
    monkeypatch.setattr(medley_action.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        medley_action, "judgement_details_visible",
        lambda _image: not (interruption == "marker-missing" and clock[0] == .5),
    )

    def parse(_image):
        if interruption == "invalid-digits" and clock[0] == .5:
            raise ValueError("判定数字正在变化")
        return result

    monkeypatch.setattr(medley_action, "ResultParser", lambda: SimpleNamespace(parse=parse))

    parsed, _image = flow.parse_stable_result(
        replace(song(1), expected_notes=101), image, timeout_seconds=3,
    )

    assert parsed is result
    assert clock[0] == 2.0


def test_medley_result_does_not_accept_unresolved_note_total(monkeypatch):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    result = medley_action.LiveResult(100, 0, 0, 0, 0, 0, 0, .95)
    clock = [0.0]
    flow = object.__new__(MedleyFlow)
    flow.wait = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    flow.capture = lambda: image
    monkeypatch.setattr(medley_action.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(medley_action, "judgement_details_visible", lambda _image: True)
    monkeypatch.setattr(
        medley_action, "ResultParser",
        lambda: SimpleNamespace(
            parse=lambda _image: result,
            resolve_expected_total=lambda _image, **_kwargs: result,
        ),
    )

    with pytest.raises(RuntimeError, match="判定数字未稳定"):
        flow.parse_stable_result(replace(song(1), expected_notes=101), image, timeout_seconds=2)


def test_remaining_medley_results_follow_session_order_without_header_ocr(monkeypatch):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    flow = object.__new__(MedleyFlow)
    flow.capture = lambda: image
    flow.wait = lambda _seconds: None
    flow.dismiss_quit_confirm = lambda _image: False
    flow.home_or_tour_select = lambda _image: False
    flow.result_header_matches = lambda *_args: (_ for _ in ()).throw(
        AssertionError("已确认离开上一成绩页后不应再 OCR 标题")
    )
    parsed = []
    flow.parse_stable_result = lambda expected, image: (
        parsed.append(expected.index) or SimpleNamespace(), image,
    )
    advances = []
    flow.advance_page = lambda _image: advances.append(1) or True
    flow.sessions = SimpleNamespace(update=lambda session, **changes: session | changes)
    monkeypatch.setattr(medley_action, "judgement_details_visible", lambda _image: True)
    saved = []
    monkeypatch.setattr(
        medley_action, "finalize_deferred_result",
        lambda path, *_args, **_kwargs: saved.append(path),
    )
    songs = tuple(
        replace(song(index, bestdori_song_id=index), report_path=f"song{index}.json")
        for index in (1, 2, 3)
    )

    session = flow.collect_results({"session_id": "test", "results_completed": 1}, songs)

    assert session["results_completed"] == 3
    assert parsed == [2, 3]
    assert saved == ["song2.json", "song3.json"]
    assert advances == [1]


def test_collect_results_fails_immediately_if_home_arrives_before_third_result(
    monkeypatch,
):
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    flow = object.__new__(MedleyFlow)
    flow.capture = lambda: image
    flow.dismiss_quit_confirm = lambda _image: False
    flow.home_or_tour_select = lambda _image: True
    advances = []
    flow.advance_page = lambda *_args, **_kwargs: advances.append("advance")

    with pytest.raises(RuntimeError, match="仅保存 2/3"):
        flow.collect_results(
            {"session_id": "test", "results_completed": 2},
            (song(1), song(2), song(3)),
        )

    assert advances == []


def test_collect_results_treats_summary_as_unknown_and_recovers_with_same_cycle(
    monkeypatch,
    tmp_path,
):
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    flow = object.__new__(MedleyFlow)
    flow.capture = lambda: image
    flow.wait = lambda _seconds: None
    flow.dismiss_quit_confirm = lambda _image: False
    flow.home_or_tour_select = lambda _image: False
    flow.story_handled = lambda _image: False
    actions = []
    install_result_cadence(flow, actions)
    recovered = []
    flow.recover_home = lambda **kwargs: recovered.append(kwargs)
    monkeypatch.setattr(
        medley_action,
        "judgement_details_visible",
        lambda _image: False,
    )
    monkeypatch.setattr(
        medley_action,
        "medley_score_summary_visible",
        lambda _image: (_ for _ in ()).throw(
            AssertionError("组曲结算不应识别巡演总分页")
        ),
        raising=False,
    )
    monkeypatch.setattr(
        medley_action,
        "RESULT_NAVIGATION_MAX_CYCLES",
        2,
        raising=False,
    )
    monkeypatch.setattr(medley_action, "PROJECT_ROOT", tmp_path)
    clock = [0.0]

    def monotonic():
        clock[0] += 10.0
        return clock[0]

    monkeypatch.setattr(medley_action.time, "monotonic", monotonic)

    with pytest.raises(RuntimeError, match="连续 2 次 BACK"):
        flow.collect_results(
            {"session_id": "known-summary", "results_completed": 0},
            (song(1), song(2), song(3)),
        )

    assert actions == [
        medley_action.RESULT_ANIMATION_SKIP_POINT,
        "back",
        medley_action.RESULT_ANIMATION_SKIP_POINT,
        "back",
    ]
    assert recovered == [{"result_navigation": True}]
    assert (
        tmp_path / "screencap" / "medley-result-timeout-known-summary.png"
    ).exists()


def test_collect_results_uses_complete_cycle_on_unidentified_page(
    monkeypatch,
):
    def frame(tag: int) -> np.ndarray:
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        image[0, 0, 0] = tag
        return image

    actions = []
    result_frames = iter((frame(1), frame(2), frame(3)))
    flow = object.__new__(MedleyFlow)

    def capture():
        if len(actions) < 3:
            return frame(5)
        return next(result_frames, frame(9))

    flow.capture = capture
    flow.wait = lambda _seconds: None
    install_result_cadence(flow, actions)
    flow.story_handled = lambda _image: False
    flow.dismiss_quit_confirm = lambda _image: False
    flow.home_or_tour_select = lambda image: int(image[0, 0, 0]) == 9
    flow.parse_stable_result = lambda _song, image: (SimpleNamespace(), image)
    flow.advance_page = lambda _image, **_kwargs: None
    flow.sessions = SimpleNamespace(
        update=lambda session, **changes: session | changes,
    )
    monkeypatch.setattr(
        medley_action,
        "judgement_details_visible",
        lambda image: int(image[0, 0, 0]) in {1, 2, 3},
    )
    monkeypatch.setattr(
        medley_action,
        "finalize_deferred_result",
        lambda *_args, **_kwargs: None,
    )
    clock = [0.0]

    def monotonic():
        clock[0] += 0.3
        return clock[0]

    monkeypatch.setattr(medley_action.time, "monotonic", monotonic)
    songs = tuple(
        replace(song(index), report_path=f"screencap/song{index}.json")
        for index in (1, 2, 3)
    )

    result = flow.collect_results(
        {"session_id": "test", "results_completed": 0},
        songs,
    )

    assert result["results_completed"] == 3
    assert actions == [
        medley_action.RESULT_ANIMATION_SKIP_POINT,
        "back",
        medley_action.RESULT_ANIMATION_SKIP_POINT,
    ]


def test_collect_results_observes_pggbm_after_back_before_next_click(
    monkeypatch,
):
    """PGGBM 在 BACK 后出现时，必须先截图，不能被下一次点击越过。"""

    class PggbmObserved(RuntimeError):
        pass

    def frame(tag: int) -> np.ndarray:
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        image[0, 0, 0] = tag
        return image

    state = {"page": "summary", "back_next": False}
    actions = []
    flow = object.__new__(MedleyFlow)
    flow.capture = lambda: frame({
        "summary": 4,
        "pggbm": 1,
        "home": 9,
    }[state["page"]])
    flow.wait = lambda _seconds: None
    flow.story_handled = lambda _image: False
    flow.dismiss_quit_confirm = lambda _image: False
    flow.home_or_tour_select = lambda image: int(image[0, 0, 0]) == 9
    flow.parse_stable_result = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        PggbmObserved("PGGBM observed before the next click")
    )

    def whole_cycle(_phase):
        actions.extend([
            medley_action.RESULT_ANIMATION_SKIP_POINT,
            "back",
            medley_action.RESULT_ANIMATION_SKIP_POINT,
        ])
        state["page"] = "home"

    def cadence_step(_phase):
        if state["back_next"]:
            actions.append("back")
            state["back_next"] = False
            state["page"] = "pggbm"
            return "BACK"
        actions.append(medley_action.RESULT_ANIMATION_SKIP_POINT)
        state["back_next"] = True
        return "最右下角"

    flow.accelerated_result_back = whole_cycle
    flow.result_cadence_step = cadence_step
    monkeypatch.setattr(
        medley_action,
        "judgement_details_visible",
        lambda image: int(image[0, 0, 0]) == 1,
    )

    with pytest.raises(PggbmObserved, match="before the next click"):
        flow.collect_results(
            {"session_id": "checkpoint", "results_completed": 0},
            (song(1), song(2), song(3)),
        )

    assert actions == [
        medley_action.RESULT_ANIMATION_SKIP_POINT,
        "back",
    ]


def test_unknown_result_exhaustion_recovers_home_before_failure(
    tmp_path,
    monkeypatch,
):
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    flow = object.__new__(MedleyFlow)
    flow.capture = lambda: image
    flow.wait = lambda _seconds: None
    actions = []
    install_result_cadence(flow, actions)
    flow.story_handled = lambda _image: False
    flow.dismiss_quit_confirm = lambda _image: False
    flow.home_or_tour_select = lambda _image: False
    recovered = []
    flow.recover_home = lambda **kwargs: recovered.append(kwargs)
    monkeypatch.setattr(
        medley_action,
        "judgement_details_visible",
        lambda _image: False,
    )
    monkeypatch.setattr(
        medley_action,
        "RESULT_NAVIGATION_MAX_CYCLES",
        2,
    )
    monkeypatch.setattr(medley_action, "PROJECT_ROOT", tmp_path)
    clock = [0.0]

    def monotonic():
        clock[0] += 0.6
        return clock[0]

    monkeypatch.setattr(medley_action.time, "monotonic", monotonic)

    with pytest.raises(RuntimeError, match="连续 2 次 BACK"):
        flow.collect_results(
            {"session_id": "test", "results_completed": 0},
            (song(1), song(2), song(3)),
        )

    assert recovered == [{"result_navigation": True}]
    assert actions == [
        medley_action.RESULT_ANIMATION_SKIP_POINT,
        "back",
        medley_action.RESULT_ANIMATION_SKIP_POINT,
        "back",
    ]
    assert (tmp_path / "screencap" / "medley-result-timeout-test.png").exists()


def test_matching_second_stage_resumes_without_home_recovery():
    songs = (song(1), song(2), song(3))
    session = {
        "outer_task_id": 101,
        "tour_type": "free",
        "song_mode": "random",
        "requested_difficulty": "Expert",
        "completed_songs": 1,
        "stage": "ready-2",
        "songs": [medley_action.asdict(item) for item in songs],
    }

    class Sessions:
        def latest(self, _tour_type):
            return session

        def update(self, value, **changes):
            value.update(changes)
            return value

    flow = object.__new__(MedleyFlow)
    flow.outer_task_id = 101
    flow.settings = {
        "tour_type": "free",
        "song_mode": "random",
        "difficulty": "Expert",
    }
    flow.sessions = Sessions()
    flow.profile_store = type("Profiles", (), {
        "runtime_options": lambda self: {"note_speed_settings_enabled": False}
    })()
    flow.capture = lambda: stage_frame(2)
    flow.pending_report_ready = lambda _song: False
    confirmed = []
    flow.confirm_preparation = lambda current, _image: confirmed.append(current.index)
    recovery_calls = []
    flow.recover_home = lambda **kwargs: recovery_calls.append(kwargs)
    flow.initialise_progress = lambda completed, **_kwargs: confirmed.append(
        ("progress", completed)
    )
    flow.progress = lambda _phase: True
    flow.wait_for_stage = lambda index: stage_frame(index)
    played = []

    def play(current_session, current_song, _image):
        played.append(current_song.index)
        current_session["completed_songs"] = current_song.index
        return current_session, current_song

    flow.play_song = play
    flow.collect_results = lambda current, _songs: current

    assert flow.run() is True
    assert confirmed[:2] == [2, ("progress", 1)]
    assert played == [2, 3]
    assert recovery_calls == [{"result_navigation": True}]


def test_new_outer_task_supersedes_old_session_and_restarts_from_zero(
    monkeypatch,
):
    """重新点击开始不是同一 Maa task，不能继承旧任务已经完成的 6 首。"""
    old_session = {
        "outer_task_id": 101,
        "tour_type": "free",
        "song_mode": "random",
        "requested_difficulty": "Expert",
        "target_count": 9,
        "completed_before_round": 6,
        "completed_songs": 0,
        "results_completed": 0,
        "stage": "ready-1",
        "status": "paused",
        "songs": [medley_action.asdict(song(index)) for index in range(1, 4)],
    }
    updates = []
    starts = []

    class Sessions:
        def latest(self, _tour_type):
            return old_session if old_session["status"] != "superseded" else None

        def update(self, value, **changes):
            updates.append(changes)
            value.update(changes)
            return value

        def start(self, **kwargs):
            starts.append(kwargs)
            return {
                "outer_task_id": kwargs["outer_task_id"],
                "completed_before_round": kwargs["completed_before_round"],
                "completed_songs": 0,
                "results_completed": 0,
            }

    flow = object.__new__(MedleyFlow)
    flow.outer_task_id = 202
    flow.settings = {
        "tour_type": "free",
        "song_mode": "random",
        "difficulty": "Expert",
        "count": 9,
    }
    flow.sessions = Sessions()
    flow.profile_store = SimpleNamespace(
        runtime_options=lambda: {"note_speed_settings_enabled": False}
    )
    flow.capture_after_reward_overlays = lambda: object()
    flow.recover_home = lambda **_kwargs: None
    flow.speed_gate = lambda _difficulty: None
    flow.navigate_to_tour = lambda: object()
    flow.choose_tour_type = lambda: object()
    fresh_songs = (song(1), song(2), song(3))
    flow.select_free_songs = lambda: fresh_songs
    flow.validate_home_speed = lambda *_args, **_kwargs: 5.0
    flow.click = lambda _point: None
    flow.wait = lambda _seconds: None
    flow.capture = lambda: object()
    flow.confirm_preparation = lambda _song, _image: None
    flow.ensure_progress = lambda *_args, **_kwargs: None
    flow.wait_for_stage = lambda _index: object()
    flow.play_song = lambda session, current, _image: (
        session | {"completed_songs": current.index},
        current,
    )
    flow.progress = lambda _phase: True
    flow.collect_results = lambda session, _songs: session
    flow.finish_round = lambda _session: "finished"
    monkeypatch.setattr(medley_action, "detect_medley_stage", lambda _image: None)

    assert flow.run() == "finished"
    assert updates[0] == {
        "status": "superseded",
        "terminal_reason": "new_task_started",
    }
    assert starts[0]["outer_task_id"] == 202
    assert starts[0]["completed_before_round"] == 0


def test_new_task_at_old_second_stage_fails_closed_without_counting_song():
    old_session = {
        "outer_task_id": 101,
        "tour_type": "free",
        "song_mode": "random",
        "requested_difficulty": "Expert",
        "target_count": 9,
        "completed_before_round": 6,
        "completed_songs": 1,
        "stage": "ready-2",
        "status": "paused",
        "songs": [medley_action.asdict(song(index)) for index in range(1, 4)],
    }
    updates = []

    class Sessions:
        def latest(self, _tour_type):
            return old_session if old_session["status"] != "superseded" else None

        def update(self, value, **changes):
            updates.append(changes)
            value.update(changes)
            return value

    flow = object.__new__(MedleyFlow)
    flow.outer_task_id = 202
    flow.settings = {
        "tour_type": "free",
        "song_mode": "random",
        "difficulty": "Expert",
        "count": 9,
    }
    flow.sessions = Sessions()
    flow.profile_store = SimpleNamespace(
        runtime_options=lambda: {"note_speed_settings_enabled": False}
    )
    flow.capture_after_reward_overlays = lambda: stage_frame(2)

    with pytest.raises(RuntimeError, match="没有匹配的组曲会话"):
        flow.run()

    assert updates == [{
        "status": "superseded",
        "terminal_reason": "new_task_started",
    }]


def test_restart_after_manually_leaving_pending_results_starts_new_round(
    monkeypatch,
):
    """结算已被人工退出时，旧会话不可恢复，但不应阻塞下一次任务。"""
    old_songs = (song(1), song(2), song(3))
    old_session = {
        "outer_task_id": 101,
        "tour_type": "free",
        "song_mode": "random",
        "requested_difficulty": "Expert",
        "completed_songs": 3,
        "results_completed": 0,
        "completed_before_round": 0,
        "stage": "results",
        "status": "paused",
        "songs": [medley_action.asdict(item) for item in old_songs],
    }
    updates = []
    starts = []

    class Sessions:
        def latest(self, _tour_type):
            return old_session

        def update(self, value, **changes):
            updates.append(changes)
            value.update(changes)
            return value

        def start(self, **kwargs):
            starts.append(kwargs)
            return {
                "completed_before_round": 0,
                "completed_songs": 0,
                "results_completed": 0,
            }

    flow = object.__new__(MedleyFlow)
    flow.outer_task_id = 101
    flow.settings = {
        "tour_type": "free",
        "song_mode": "random",
        "difficulty": "Expert",
        "count": 3,
    }
    flow.sessions = Sessions()
    flow.profile_store = SimpleNamespace(
        runtime_options=lambda: {"note_speed_settings_enabled": False}
    )
    home_image = object()
    flow.capture_after_reward_overlays = lambda: home_image
    flow.home_or_tour_select = lambda image: image is home_image
    recovered = []
    flow.recover_home = lambda **kwargs: recovered.append(kwargs)
    flow.speed_gate = lambda _difficulty: None
    flow.navigate_to_tour = lambda: object()
    flow.choose_tour_type = lambda: object()
    new_songs = (song(1), song(2), song(3))
    flow.select_free_songs = lambda: new_songs
    flow.validate_home_speed = lambda *_args, **_kwargs: 5.0
    flow.click = lambda _point: None
    flow.wait = lambda _seconds: None
    flow.capture = lambda: object()
    flow.confirm_preparation = lambda _song, _image: None
    flow.ensure_progress = lambda *_args, **_kwargs: None
    flow.wait_for_stage = lambda _index: object()
    played = []

    def play(current_session, current_song, _image):
        played.append(current_song.index)
        current_session["completed_songs"] = current_song.index
        return current_session, current_song

    flow.play_song = play
    flow.progress = lambda _phase: True
    flow.collect_results = lambda current, _songs: current
    flow.finish_round = lambda _session: "finished"
    monkeypatch.setattr(medley_action, "detect_medley_stage", lambda _image: None)

    assert flow.run() == "finished"
    assert updates[0] == {
        "status": "superseded",
        "terminal_reason": "result_pages_left_before_collection",
    }
    assert len(starts) == 1
    assert played == [1, 2, 3]
    assert recovered == [{}]


def test_restart_from_unrelated_page_does_not_assume_pending_results(
    monkeypatch,
):
    """普通非主页页面应退回主页，不能仅凭旧会话冒充结算续跑。"""
    old_songs = (song(1), song(2), song(3))
    old_session = {
        "outer_task_id": 101,
        "tour_type": "task",
        "song_mode": "random",
        "requested_difficulty": "Expert",
        "target_count": 3,
        "completed_songs": 3,
        "results_completed": 0,
        "completed_before_round": 0,
        "stage": "results",
        "status": "paused",
        "songs": [medley_action.asdict(item) for item in old_songs],
    }
    updates = []
    starts = []

    class Sessions:
        def latest(self, _tour_type):
            return old_session

        def update(self, value, **changes):
            updates.append(changes)
            value.update(changes)
            return value

        def start(self, **kwargs):
            starts.append(kwargs)
            return {
                "completed_before_round": 0,
                "completed_songs": 0,
                "results_completed": 0,
            }

    flow = object.__new__(MedleyFlow)
    flow.outer_task_id = 101
    flow.settings = {
        "tour_type": "task",
        "song_mode": "random",
        "difficulty": "Expert",
        "count": 3,
    }
    flow.sessions = Sessions()
    flow.repository = object()
    flow.profile_store = SimpleNamespace(
        runtime_options=lambda: {"note_speed_settings_enabled": False}
    )
    unrelated_image = object()
    home_image = object()
    flow.capture_after_reward_overlays = lambda: unrelated_image
    flow.capture = lambda: home_image
    flow.home_or_tour_select = lambda image: image is home_image
    flow.dismiss_quit_confirm = lambda _image: False
    flow.save_debug_image = lambda *_args: None
    flow.result_cadence_step = lambda _phase: "BACK"
    flow.wait = lambda _seconds: None
    recovered = []
    flow.recover_home = lambda **kwargs: recovered.append(kwargs)
    flow.speed_gate = lambda _difficulty: None
    flow.navigate_to_tour = lambda: object()
    flow.choose_tour_type = lambda: object()
    new_songs = (song(1), song(2), song(3))
    monkeypatch.setattr(
        medley_action,
        "read_task_tour_songs",
        lambda *_args, **_kwargs: new_songs,
    )
    flow.validate_home_speed = lambda *_args, **_kwargs: 5.0
    flow.click = lambda _point: None
    flow.confirm_preparation = lambda _song, _image: None
    progress = []
    flow.ensure_progress = lambda completed, **kwargs: progress.append(
        (completed, kwargs)
    )
    flow.wait_for_stage = lambda _index: object()

    def play(current_session, current_song, _image):
        current_session["completed_songs"] = current_song.index
        return current_session, current_song

    flow.play_song = play
    flow.progress = lambda _phase: True
    collect_calls = []

    def collect_results(current_session, songs):
        collect_calls.append(current_session)
        if len(collect_calls) == 1:
            return MedleyFlow.collect_results(flow, current_session, songs)
        return current_session

    flow.collect_results = collect_results
    flow.finish_round = lambda _session: "finished"
    monkeypatch.setattr(medley_action, "detect_medley_stage", lambda _image: None)

    assert flow.run() == "finished"
    assert updates[0] == {
        "status": "superseded",
        "terminal_reason": "result_pages_left_before_collection",
    }
    assert len(starts) == 1
    assert progress == [(0, {"next_started": True})]
    assert recovered == []


def test_restart_at_home_after_all_results_saved_finishes_existing_round(
    monkeypatch,
):
    """三张结果已落盘时，即使完成标记前中断，也不能重打一组。"""
    saved_session = {
        "outer_task_id": 101,
        "tour_type": "free",
        "song_mode": "random",
        "requested_difficulty": "Expert",
        "completed_songs": 3,
        "results_completed": 3,
        "completed_before_round": 0,
        "stage": "post-results",
        "status": "paused",
        "songs": [medley_action.asdict(song(index)) for index in range(1, 4)],
    }

    flow = object.__new__(MedleyFlow)
    flow.outer_task_id = 101
    flow.settings = {
        "tour_type": "free",
        "song_mode": "random",
        "difficulty": "Expert",
        "count": 3,
    }
    flow.sessions = SimpleNamespace(latest=lambda _tour_type: saved_session)
    flow.profile_store = SimpleNamespace(
        runtime_options=lambda: {"note_speed_settings_enabled": False}
    )
    home_image = object()
    flow.capture_after_reward_overlays = lambda: home_image
    flow.home_or_tour_select = lambda image: image is home_image
    progress = []
    flow.ensure_progress = lambda completed, **kwargs: progress.append(
        (completed, kwargs)
    )
    finished = []
    flow.finish_round = lambda session: finished.append(session) or "finished"
    monkeypatch.setattr(medley_action, "detect_medley_stage", lambda _image: None)

    assert flow.run() == "finished"
    assert progress == [(3, {"next_started": False})]
    assert finished == [saved_session]


def test_finish_round_starts_the_next_full_round_when_count_is_six():
    updates = []
    flow = object.__new__(MedleyFlow)
    flow.settings = {"count": 6}
    flow.sessions = SimpleNamespace(
        update=lambda session, **changes: updates.append(changes) or session | changes
    )
    recovered = []
    progress = []
    flow.recover_home = lambda **kwargs: recovered.append(kwargs)
    flow.progress = lambda phase: progress.append(phase) or True
    flow.run = lambda: "next-round"

    result = flow.finish_round({"completed_before_round": 0})

    assert result == "next-round"
    assert updates[-1]["completed_total"] == 3
    assert updates[-1]["status"] == "completed"
    assert flow._next_round_completed == 3
    assert recovered == [{"result_navigation": True}]
    assert progress == ["start"]


def test_stage_without_matching_session_fails_before_recovery():
    flow = object.__new__(MedleyFlow)
    flow.settings = {
        "tour_type": "free",
        "song_mode": "random",
        "difficulty": "Expert",
    }
    flow.sessions = type("Sessions", (), {"latest": lambda self, _kind: None})()
    flow.profile_store = type("Profiles", (), {
        "runtime_options": lambda self: {"note_speed_settings_enabled": False}
    })()
    flow.capture = lambda: stage_frame(3)
    flow.recover_home = lambda: (_ for _ in ()).throw(
        AssertionError("身份缺失时不能退出续跑页面")
    )

    with pytest.raises(RuntimeError, match="没有匹配的组曲会话"):
        flow.run()


def test_reconcile_stage_accepts_completed_pending_report(tmp_path, monkeypatch):
    monkeypatch.setattr(medley_action, "PROJECT_ROOT", tmp_path)
    report = tmp_path / "screencap" / "song1.json"
    report.parent.mkdir(parents=True)
    report.write_text(
        '{"result_status":"medley_result_pending","completed":true}',
        encoding="utf-8",
    )
    songs = (
        replace(song(1), report_path="screencap/song1.json"),
        song(2),
        song(3),
    )
    session = {"completed_songs": 0, "stage": "playing-1"}
    flow = object.__new__(MedleyFlow)
    flow.sessions = type("Sessions", (), {
        "update": lambda self, value, **changes: value | changes,
    })()

    reconciled = flow.reconcile_stage(session, songs, 2)

    assert reconciled["completed_songs"] == 1
    assert reconciled["stage"] == "ready-2"


def test_user_stop_pauses_session_without_business_failure(monkeypatch):
    updates = []
    session = {"status": "active"}

    class Sessions:
        def latest(self, _tour_type):
            return session

        def update(self, value, **changes):
            updates.append(changes)
            value.update(changes)
            return value

    class Flow:
        def __init__(self, _context, _argv, settings):
            self.settings = settings
            self.sessions = Sessions()

        def run(self):
            raise medley_action.ScreenRefreshCancelled("task is stopping")

    monkeypatch.setattr(medley_action, "MedleyFlow", Flow)
    configure_medley_settings({"reset": True})
    context = type("Context", (), {
        "tasker": type("Tasker", (), {"stopping": True})(),
    })()
    argv = type("Argv", (), {"custom_action_param": "{}"})()

    assert MedleyLiveFlow().run(context, argv) is True
    assert updates[-1] == {
        "status": "paused",
        "terminal_reason": "user_stopped",
    }


def test_wait_does_not_pass_negative_duration_when_deadline_is_crossed(
    monkeypatch,
):
    flow = object.__new__(MedleyFlow)
    flow.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
    timestamps = iter((10.0, 10.34, 10.36))
    sleeps = []

    monkeypatch.setattr(medley_action.time, "monotonic", lambda: next(timestamps))

    def strict_sleep(seconds):
        if seconds < 0:
            raise ValueError("sleep length must be non-negative")
        sleeps.append(seconds)

    monkeypatch.setattr(medley_action.time, "sleep", strict_sleep)

    flow.wait(0.35)

    assert sleeps == pytest.approx([0.01])
