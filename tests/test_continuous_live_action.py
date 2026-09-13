import json
from types import SimpleNamespace

import numpy as np
import pytest

from agent.realtime import continuous_live_action

from agent.realtime.continuous_live_action import (
    ContinuousFinalCoverRecognizer,
    ContinuousRealtimeLive,
    ContinuousSongEvidence,
    ListenerDiagnosticCapture,
    configure_continuous_settings,
    continuous_song_params,
    current_continuous_settings,
    require_recent_speed_settings,
    run_continuous_listener,
)
from agent.realtime.final_cover import FinalCoverConfirmation, FinalCoverResolution
from agent.realtime.live_session import current_live_run
from agent.realtime.chart_repository import (
    CatalogSongIdentity,
    CatalogSongResolution,
    ChartResolution,
)
from agent.realtime.song_identity import SongIdentity
from agent.realtime.song_title_ocr import FINAL_COVER_TITLE_ROI, TitleReading


class Recognizer:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.resets = 0

    def observe(self, _image):
        return next(self.outcomes)

    def reset(self):
        self.resets += 1


def test_continuous_requires_recent_actual_speed_readback(monkeypatch):
    calls = []
    monkeypatch.setattr(
        continuous_live_action,
        "verified_settings",
        lambda difficulty, **kwargs: calls.append((difficulty, kwargs)) or None,
    )

    with pytest.raises(RuntimeError, match="不能用 MFA 目标配置冒充"):
        require_recent_speed_settings("Expert")

    assert calls == [("Expert", {"max_age_seconds": 900})]


def test_continuous_accepts_recent_actual_speed_readback(monkeypatch):
    verified = object()
    monkeypatch.setattr(
        continuous_live_action,
        "verified_settings",
        lambda _difficulty, **_kwargs: verified,
    )

    assert require_recent_speed_settings("Expert") is verified


def test_continuous_never_checks_speed_readback_when_setting_is_disabled(monkeypatch):
    monkeypatch.setattr(
        continuous_live_action,
        "verified_settings",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("关闭开关后不得检查流速读回")
        ),
    )

    assert require_recent_speed_settings("Expert", enabled=False) is None


def test_continuous_options_merge_difficulty_and_debug_without_overwrite():
    configure_continuous_settings({"reset": True, "difficulty": "Expert"})
    configure_continuous_settings({
        "debug_recording": False,
        "diagnostic_trace": True,
    })

    settings = current_continuous_settings()

    assert settings["difficulty"] == "Expert"
    assert settings["debug_recording"] is False
    assert settings["diagnostic_trace"] is True
    assert settings["dpi"] == 240
    assert settings["game_fps"] == 60


def test_continuous_watcher_accepts_maafw_null_custom_action_param(monkeypatch):
    configure_continuous_settings({"reset": True, "difficulty": "Expert"})
    monkeypatch.setattr(
        continuous_live_action.RealtimeProfileStore,
        "runtime_options",
        lambda _self: {"note_speed_settings_enabled": False},
    )
    monkeypatch.setattr(
        continuous_live_action,
        "resolve_profile_for_settings_gate",
        lambda *_args, **_kwargs: SimpleNamespace(
            profile_path=SimpleNamespace(name="expert.json"),
            note_speed=5.0,
        ),
    )
    monkeypatch.setattr(
        continuous_live_action,
        "run_continuous_listener",
        lambda *_args, **_kwargs: None,
    )
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=object())
    )
    argv = SimpleNamespace(custom_action_param="null")

    assert ContinuousRealtimeLive()._run(context, argv) is True
    assert current_continuous_settings()["difficulty"] == "Expert"


def test_listener_has_no_input_without_confirmed_opening_identity():
    captures = []
    plays = []
    stopped = False

    def capture():
        captures.append("capture")
        return object()

    def stopping():
        nonlocal stopped
        if len(captures) >= 5:
            stopped = True
        return stopped

    completed = run_continuous_listener(
        capture,
        stopping,
        lambda _evidence: plays.append("play") or True,
        recognizer=Recognizer([None] * 5),
        sleeper=lambda _: None,
    )

    assert completed is False
    assert len(captures) == 5
    assert plays == []


def test_listener_stops_after_first_completed_song():
    first = object()
    second = object()
    recognizer = Recognizer([None, first, None, second])
    plays = []

    def play_song(evidence):
        plays.append(evidence)
        return True

    completed = run_continuous_listener(
        lambda: object(),
        lambda: len(plays) >= 2,
        play_song,
        recognizer=recognizer,
        sleeper=lambda _: None,
    )

    assert completed is True
    assert plays == [first]
    assert recognizer.resets == 0


def test_listener_observes_non_playfield_frames_for_stop_diagnostics():
    observed = []

    run_continuous_listener(
        lambda: "cooperative-live-frame",
        lambda: len(observed) >= 3,
        lambda _evidence: True,
        recognizer=Recognizer([None] * 3),
        sleeper=lambda _: None,
        on_frame=observed.append,
    )

    assert observed == ["cooperative-live-frame"] * 3


def test_listener_reports_recognition_state_after_each_observation():
    class DiagnosticRecognizer(Recognizer):
        def diagnostic_state(self):
            return {
                "reason": "等待开场歌曲封面稳定",
                "candidate_frames": 1,
            }

    observations = []
    recognizer = DiagnosticRecognizer([None])

    run_continuous_listener(
        lambda: "opening-frame",
        lambda: bool(observations),
        lambda _evidence: True,
        recognizer=recognizer,
        sleeper=lambda _: None,
        on_observation=lambda image, state: observations.append((image, state)),
    )

    assert observations == [(
        "opening-frame",
        {"reason": "等待开场歌曲封面稳定", "candidate_frames": 1},
    )]


def test_listener_diagnostic_saves_last_waiting_frame_on_stop(tmp_path):
    diagnostic = ListenerDiagnosticCapture(tmp_path)
    diagnostic.observe(np.full((4, 6, 3), 127, dtype=np.uint8))

    path = diagnostic.save("stopped")

    assert path is not None
    assert path.name == "last-frame.png"
    assert path.exists()
    metadata = json.loads((path.parent / "metadata.json").read_text(encoding="utf-8"))
    assert metadata == {"reason": "stopped"}


def test_listener_diagnostic_saves_bounded_recognition_candidate(tmp_path):
    diagnostic = ListenerDiagnosticCapture(
        tmp_path,
        clock=iter([0.0, 2.1]).__next__,
        playfield_detector=lambda _image: False,
    )
    first = np.full((8, 12, 3), 64, dtype=np.uint8)
    stable = np.full((8, 12, 3), 192, dtype=np.uint8)
    diagnostic.observe_recognition(first, {
        "reason": "等待开场歌曲封面稳定",
        "candidate_song_id": "song-jacket-phash-v2-first",
        "candidate_frames": 1,
        "title": None,
        "title_confidence": 0.0,
    })
    diagnostic.observe_recognition(stable, {
        "reason": "song title does not match final cover",
        "candidate_song_id": "song-jacket-phash-v2-stable",
        "candidate_frames": 2,
        "title": "OCR title",
        "title_confidence": 0.88,
    })

    path = diagnostic.save("stopped")
    metadata = json.loads(
        (path.parent / "metadata.json").read_text(encoding="utf-8")
    )

    assert (path.parent / "candidate-001.png").exists()
    assert metadata["reason"] == "stopped"
    assert metadata["recognition_candidates"][0]["candidate_frames"] == 2
    assert metadata["recognition_candidates"][0]["title"] == "OCR title"
    assert len(metadata["recognition_samples"]) == 2


def test_listener_diagnostic_keeps_stable_candidate_when_title_is_missing(
    tmp_path,
):
    diagnostic = ListenerDiagnosticCapture(
        tmp_path,
        clock=lambda: 0.0,
        playfield_detector=lambda _image: False,
    )
    image = np.full((8, 12, 3), 96, dtype=np.uint8)
    diagnostic.observe_recognition(image, {
        "reason": "开场歌曲标题尚未识别",
        "candidate_song_id": "song-jacket-phash-v2-stable",
        "candidate_frames": 2,
        "title": None,
        "title_confidence": 0.0,
    })

    path = diagnostic.save("stopped")
    metadata = json.loads(
        (path.parent / "metadata.json").read_text(encoding="utf-8")
    )

    assert (path.parent / "candidate-001.png").exists()
    assert metadata["recognition_candidates"][0]["title"] is None


def test_one_key_playback_continues_after_life_depletion():
    params = continuous_song_params({
        "difficulty": "Expert",
        "continue_after_life_depleted": False,
    })

    assert params["continue_after_life_depleted"] is True
    assert params["run_mode"] == "continuous"
    assert params["require_completion"] is True
    assert params["confirm_final_cover"] is True
    assert params["native_prearm_deferred"] is True


def test_opening_recognizer_requires_stable_cover_title_and_selected_difficulty():
    song_id = "song-jacket-phash-v2-c7b9cb102fcfb04a"
    canonical_song_id = "song-jacket-phash-v2-c7bac9172dceb062"
    catalog_song = CatalogSongIdentity(
        bestdori_song_id=50,
        title="FIRE BIRD",
        titles=("FIRE BIRD",),
        fingerprints=(canonical_song_id,),
    )
    selection = SimpleNamespace(
        bestdori_song_id=50,
        title="FIRE BIRD",
        titles=("FIRE BIRD",),
        difficulty="expert",
        level=28,
    )

    class Repository:
        def __init__(self):
            self.calls = []

        def identify_by_cover_title(self, fingerprint, title):
            self.calls.append(("identity", fingerprint, title))
            return CatalogSongResolution(
                catalog_song,
                "confirmed song by final cover and title",
            )

        def resolve(
            self,
            fingerprint,
            difficulty,
            *,
            title,
            bestdori_song_id,
        ):
            self.calls.append(
                (
                    "chart",
                    fingerprint,
                    difficulty,
                    title,
                    bestdori_song_id,
                )
            )
            return ChartResolution(selection, "confirmed local chart")

    repository = Repository()
    title_rois = []
    recognizer = ContinuousFinalCoverRecognizer(
        repository,
        "Expert",
        identify=lambda _image: SongIdentity(song_id, "song-jacket-phash-v2"),
        title_reader=lambda _image, *, roi: (
            title_rois.append(roi) or TitleReading("FIRE BIRD", 0.95)
        ),
    )
    image = np.full((720, 1280, 3), 127, dtype=np.uint8)

    assert recognizer.observe(image) is None
    evidence = recognizer.observe(image)

    assert evidence is not None
    assert evidence.song is catalog_song
    assert evidence.chart_resolution.selection is selection
    assert evidence.observed_title == "FIRE BIRD"
    assert evidence.observed_title_confidence == pytest.approx(0.95)
    assert evidence.observed_frames == 2
    assert evidence.image is not image
    assert repository.calls == [
        ("identity", song_id, "FIRE BIRD"),
        ("chart", canonical_song_id, "Expert", "FIRE BIRD", 50),
    ]
    assert title_rois == [FINAL_COVER_TITLE_ROI] * 2


def test_opening_recognizer_rejects_title_that_conflicts_with_cover():
    song_id = "song-jacket-phash-v2-0123456789abcdef"

    class Repository:
        def identify_by_cover_title(self, _fingerprint, title):
            assert title == "Different Song"
            return CatalogSongResolution(
                None,
                "song title does not match final cover",
            )

        def resolve(self, *_args, **_kwargs):
            raise AssertionError("标题冲突时不得继续解析难度谱面")

    recognizer = ContinuousFinalCoverRecognizer(
        Repository(),
        "Expert",
        identify=lambda _image: SongIdentity(song_id, "song-jacket-phash-v2"),
        title_reader=lambda _image, *, roi: TitleReading(
            "Different Song", 0.95
        ),
    )
    image = np.full((720, 1280, 3), 127, dtype=np.uint8)

    assert recognizer.observe(image) is None
    assert recognizer.observe(image) is None
    assert recognizer.last_reason == "song title does not match final cover"


def test_opening_recognizer_allows_visual_legacy_when_difficulty_has_no_chart():
    song_id = "song-jacket-phash-v2-0123456789abcdef"
    catalog_song = CatalogSongIdentity(
        bestdori_song_id=50,
        title="FIRE BIRD",
        titles=("FIRE BIRD",),
        fingerprints=(song_id,),
    )

    class Repository:
        def identify_by_cover_title(self, _fingerprint, _title):
            return CatalogSongResolution(
                catalog_song,
                "confirmed song by final cover and title",
            )

        def resolve(
            self,
            _fingerprint,
            difficulty,
            *,
            title,
            bestdori_song_id,
        ):
            assert difficulty == "Easy"
            assert title == "FIRE BIRD"
            assert bestdori_song_id == 50
            return ChartResolution(None, "no local easy chart for confirmed song")

    recognizer = ContinuousFinalCoverRecognizer(
        Repository(),
        "Easy",
        identify=lambda _image: SongIdentity(song_id, "song-jacket-phash-v2"),
        title_reader=lambda _image, *, roi: TitleReading("FIRE BIRD", 0.95),
    )
    image = np.full((720, 1280, 3), 127, dtype=np.uint8)

    assert recognizer.observe(image) is None
    evidence = recognizer.observe(image)

    assert evidence is not None
    assert evidence.song is catalog_song
    assert evidence.chart_resolution is None


@pytest.mark.parametrize("chart_available", [True, False])
def test_action_hands_opening_identity_to_profile_play(
    monkeypatch,
    chart_available,
):
    song_id = "song-jacket-phash-v2-0123456789abcdef"
    catalog_song = CatalogSongIdentity(
        bestdori_song_id=50,
        title="FIRE BIRD",
        titles=("FIRE BIRD",),
        fingerprints=(song_id,),
    )
    selection = SimpleNamespace(bestdori_song_id=50, level=28)
    confirmation = FinalCoverConfirmation(
        song_id=song_id,
        song_id_method="song-jacket-phash-v2",
        bestdori_song_id=50,
    )
    chart_resolution = (
        FinalCoverResolution(confirmation, selection)
        if chart_available else None
    )
    evidence = ContinuousSongEvidence(
        image=object(),
        song=catalog_song,
        song_id=song_id,
        song_id_method="song-jacket-phash-v2",
        chart_resolution=chart_resolution,
        observed_title="FIRE BIRD",
        observed_title_confidence=0.95,
        observed_frames=2,
    )
    monkeypatch.setattr(
        continuous_live_action.RealtimeProfileStore,
        "runtime_options",
        lambda _self: {"note_speed_settings_enabled": False},
    )
    monkeypatch.setattr(
        continuous_live_action,
        "resolve_profile_for_settings_gate",
        lambda *_args, **_kwargs: SimpleNamespace(
            profile_path=SimpleNamespace(name="expert.json"),
            note_speed=5.0,
        ),
    )
    played = []

    def fake_profile_play(_self, _context, argv):
        run = current_live_run()
        params = json.loads(argv.custom_action_param)
        played.append((run, params))
        return True

    monkeypatch.setattr(
        continuous_live_action.RealtimeProfilePlay,
        "_run",
        fake_profile_play,
    )

    def fake_listener(_capture, _stopping, play_song, **_kwargs):
        return play_song(evidence)

    monkeypatch.setattr(
        continuous_live_action,
        "run_continuous_listener",
        fake_listener,
    )
    context = SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=object())
    )
    argv = SimpleNamespace(custom_action_param=json.dumps({
        "difficulty": "Expert",
        "dpi": 240,
        "game_fps": 60,
        "render_quality": "standard",
    }))

    assert ContinuousRealtimeLive()._run(context, argv) is True
    assert len(played) == 1
    run, params = played[0]
    assert run.mode == "continuous"
    assert run.difficulty == "Expert"
    assert run.song_id == song_id
    assert run.song_title == "FIRE BIRD"
    assert run.song_level == (28 if chart_available else None)
    assert run.final_cover_confirmed is True
    assert params["confirm_final_cover"] is chart_available
    assert params["native_prearm_deferred"] is chart_available
