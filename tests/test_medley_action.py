from __future__ import annotations

from dataclasses import replace
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
    session_matches_settings,
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
    session = store.start(settings=settings, songs=(song(1), song(2), song(3)))
    store.update(session, completed_songs=1, stage="ready-2")

    loaded = store.latest("free")
    assert loaded is not None
    assert loaded["completed_songs"] == 1
    assert loaded["stage"] == "ready-2"
    assert loaded["round_index"] == 1
    assert loaded["completed_before_round"] == 0
    assert not list(store.root.glob("*.tmp"))
    assert not (tmp_path / "profiles" / "calibration-sessions").exists()


def test_session_resume_requires_matching_free_options():
    session = {
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


def test_achievement_reward_overview_is_closed_by_esc_only():
    template = medley_action.imread_unicode(
        medley_action.ESC_ONLY_REWARD_TEMPLATES[0]
    )
    assert template is not None
    image = np.full((720, 1280, 3), 255, dtype=np.uint8)
    height, width = template.shape[:2]
    image[55:55 + height, 467:467 + width] = template
    events = []
    flow = object.__new__(MedleyFlow)
    flow.back = lambda: events.append("back")
    flow.wait = lambda _seconds: None
    flow.capture = lambda: (_ for _ in ()).throw(
        AssertionError("ESC-only 弹窗不应进入坐标点击回退")
    )
    flow.click = lambda point: events.append(point)

    assert flow.dismiss_reward(image) is True
    assert events == ["back"]


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


def test_result_advance_uses_lower_right_only_when_result_marker_does_not_leave(
    monkeypatch,
):
    before = np.zeros((720, 1280, 3), dtype=np.uint8)
    after = np.ones((720, 1280, 3), dtype=np.uint8)
    events = []
    flow = object.__new__(MedleyFlow)
    flow.back = lambda: events.append("back")
    flow.wait = lambda _seconds: None
    frames = iter((before.copy(), before.copy(), after))
    flow.capture = lambda: next(frames)
    flow.click = lambda point: events.append(point)
    monkeypatch.setattr(
        medley_action,
        "judgement_details_visible",
        lambda image: int(image[0, 0, 0]) == 0,
    )
    ticks = iter((0.0, 0.2, 0.4, 1.3, 1.4, 1.5))
    monkeypatch.setattr(medley_action.time, "monotonic", lambda: next(ticks))

    flow.advance_page(before, pggbm=True)

    assert events == ["back", medley_action.RESULT_FALLBACK_POINT]


def test_result_advance_waits_for_result_marker_to_leave(monkeypatch):
    before = np.zeros((720, 1280, 3), dtype=np.uint8)
    after = np.full((720, 1280, 3), 20, dtype=np.uint8)
    events = []
    flow = object.__new__(MedleyFlow)
    flow.back = lambda: events.append("back")
    flow.wait = lambda _seconds: None
    flow.capture = lambda: after
    flow.click = lambda point: events.append(point)
    monkeypatch.setattr(
        medley_action,
        "judgement_details_visible",
        lambda image: image is before,
    )
    ticks = iter((0.0, 0.2))
    monkeypatch.setattr(medley_action.time, "monotonic", lambda: next(ticks))

    flow.advance_page(before, pggbm=True)

    assert events == ["back"]


def test_result_advance_does_not_click_during_visible_transition(monkeypatch):
    before = np.zeros((720, 1280, 3), dtype=np.uint8)
    transition = np.full((720, 1280, 3), 20, dtype=np.uint8)
    departed = np.full((720, 1280, 3), 30, dtype=np.uint8)
    events = []
    flow = object.__new__(MedleyFlow)
    flow.back = lambda: events.append("back")
    flow.wait = lambda _seconds: None
    frames = iter((transition, transition, departed))
    flow.capture = lambda: next(frames)
    flow.click = lambda point: events.append(point)
    monkeypatch.setattr(
        medley_action,
        "judgement_details_visible",
        lambda image: int(image[0, 0, 0]) != 30,
    )
    ticks = iter((0.0, 0.2, 0.4, 1.3, 1.4, 1.5))
    monkeypatch.setattr(medley_action.time, "monotonic", lambda: next(ticks))

    flow.advance_page(before, pggbm=True)

    assert events == ["back"]


def test_summary_advance_waits_for_summary_marker_to_leave(monkeypatch):
    before = np.zeros((720, 1280, 3), dtype=np.uint8)
    transition = np.full((720, 1280, 3), 20, dtype=np.uint8)
    departed = np.full((720, 1280, 3), 30, dtype=np.uint8)
    events = []
    captures = []
    flow = object.__new__(MedleyFlow)
    flow.back = lambda: events.append("back")
    flow.wait = lambda _seconds: None
    frames = iter((transition, transition, departed))

    def capture():
        image = next(frames)
        captures.append(int(image[0, 0, 0]))
        return image

    flow.capture = capture
    flow.click = lambda point: events.append(point)
    monkeypatch.setattr(
        medley_action,
        "medley_score_summary_visible",
        lambda image: int(image[0, 0, 0]) != 30,
        raising=False,
    )
    ticks = iter((0.0, 0.2, 0.4, 1.3, 1.4, 1.5))
    monkeypatch.setattr(medley_action.time, "monotonic", lambda: next(ticks))

    flow.advance_page(before, pggbm=False)

    assert events == ["back"]
    assert captures == [20, 20, 30]


def test_collect_results_does_not_advance_transition_frames_or_third_result(
    monkeypatch,
):
    def frame(tag: int) -> np.ndarray:
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        image[0, 0, 0] = tag
        return image

    frames = iter((
        frame(4), frame(0), frame(1), frame(0), frame(0),
        frame(2), frame(0), frame(3),
    ))
    flow = object.__new__(MedleyFlow)
    flow.capture = lambda: next(frames, frame(9))
    flow.wait = lambda _seconds: None
    flow.dismiss_reward = lambda _image: False
    flow.story_handled = lambda _image: False
    flow.dismiss_quit_confirm = lambda _image: False
    flow.home_or_tour_select = lambda image: int(image[0, 0, 0]) == 9
    flow.result_header_matches = lambda expected, image: (
        int(image[0, 0, 0]) == expected.index
    )
    flow.parse_stable_result = lambda _song, image: (SimpleNamespace(), image)
    advances = []
    flow.advance_page = lambda image, **_kwargs: advances.append(
        int(image[0, 0, 0])
    )
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
        "medley_score_summary_visible",
        lambda image: int(image[0, 0, 0]) == 4,
        raising=False,
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
    assert advances == [4, 1, 2]


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


def test_collect_results_alternates_back_and_lower_right_on_stable_unknown(
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
        if len(actions) < 2:
            return frame(5)
        return next(result_frames, frame(9))

    flow.capture = capture
    flow.wait = lambda _seconds: None
    flow.back = lambda: actions.append("back")
    flow.click = lambda point: actions.append(point)
    flow.dismiss_reward = lambda _image: False
    flow.story_handled = lambda _image: False
    flow.dismiss_quit_confirm = lambda _image: False
    flow.home_or_tour_select = lambda image: int(image[0, 0, 0]) == 9
    flow.result_header_matches = lambda expected, image: (
        int(image[0, 0, 0]) == expected.index
    )
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
        "medley_score_summary_visible",
        lambda _image: False,
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
    assert actions == ["back", medley_action.RESULT_FALLBACK_POINT]


def test_unknown_result_exhaustion_recovers_home_before_failure(
    tmp_path,
    monkeypatch,
):
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    flow = object.__new__(MedleyFlow)
    flow.capture = lambda: image
    flow.wait = lambda _seconds: None
    flow.back = lambda: None
    flow.click = lambda _point: None
    flow.dismiss_reward = lambda _image: False
    flow.story_handled = lambda _image: False
    flow.dismiss_quit_confirm = lambda _image: False
    flow.home_or_tour_select = lambda _image: False
    recovered = []
    flow.recover_home = lambda: recovered.append("home")
    monkeypatch.setattr(
        medley_action,
        "judgement_details_visible",
        lambda _image: False,
    )
    monkeypatch.setattr(
        medley_action,
        "medley_score_summary_visible",
        lambda _image: False,
    )
    monkeypatch.setattr(medley_action, "UNKNOWN_RESULT_MAX_ACTIONS", 2, raising=False)
    monkeypatch.setattr(medley_action, "PROJECT_ROOT", tmp_path)
    clock = [0.0]

    def monotonic():
        clock[0] += 0.6
        return clock[0]

    monkeypatch.setattr(medley_action.time, "monotonic", monotonic)

    with pytest.raises(RuntimeError, match="连续 2 次"):
        flow.collect_results(
            {"session_id": "test", "results_completed": 0},
            (song(1), song(2), song(3)),
        )

    assert recovered == ["home"]
    assert (tmp_path / "screencap" / "medley-result-timeout-test.png").exists()


def test_matching_second_stage_resumes_without_home_recovery():
    songs = (song(1), song(2), song(3))
    session = {
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
    flow.recover_home = lambda: recovery_calls.append("home")
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
    assert recovery_calls == ["home"]


def test_finish_round_starts_the_next_full_round_when_count_is_six():
    updates = []
    flow = object.__new__(MedleyFlow)
    flow.settings = {"count": 6}
    flow.sessions = SimpleNamespace(
        update=lambda session, **changes: updates.append(changes) or session | changes
    )
    recovered = []
    progress = []
    flow.recover_home = lambda: recovered.append("home")
    flow.progress = lambda phase: progress.append(phase) or True
    flow.run = lambda: "next-round"

    result = flow.finish_round({"completed_before_round": 0})

    assert result == "next-round"
    assert updates[-1]["completed_total"] == 3
    assert updates[-1]["status"] == "completed"
    assert flow._next_round_completed == 3
    assert recovered == ["home"]
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
