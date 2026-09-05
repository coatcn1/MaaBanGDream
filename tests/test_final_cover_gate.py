from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from agent.realtime import profile_play_action
from agent.realtime.chart_repository import ChartResolution
from agent.realtime.final_cover import FinalCoverGate, FinalCoverResolver
from agent.realtime.profile_play_action import wait_for_final_cover
from agent.realtime.song_identity import (
    FINAL_SONG_JACKET_ROI,
    fingerprint_jacket,
)


def final_cover_frame(seed: int = 7) -> tuple[np.ndarray, str]:
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    x, y, width, height = FINAL_SONG_JACKET_ROI
    jacket = np.random.default_rng(seed).integers(
        0,
        256,
        size=(height, width, 3),
        dtype=np.uint8,
    )
    image[y:y + height, x:x + width] = jacket
    return image, fingerprint_jacket(jacket).song_id


def selection(song_id: str):
    return SimpleNamespace(
        bestdori_song_id=306,
        difficulty="expert",
        level=28,
        title="SAVIOR OF SONG",
        titles=("SAVIOR OF SONG",),
        fingerprints=(song_id,),
        shared_jacket=True,
    )


def test_final_cover_confirms_only_with_preparation_title_level_and_difficulty():
    image, song_id = final_cover_frame()
    gate = FinalCoverGate(
        selection(song_id),
        difficulty="Expert",
        observed_level=28,
        observed_title="SAVIOR OF SONG",
    )

    confirmation = gate.observe(image)

    assert confirmation is not None
    assert confirmation.song_id == song_id
    assert confirmation.bestdori_song_id == 306


def test_final_cover_rejects_missing_or_conflicting_preparation_evidence():
    image, song_id = final_cover_frame()

    missing_title = FinalCoverGate(
        selection(song_id),
        difficulty="Expert",
        observed_level=28,
        observed_title=None,
    )
    wrong_level = FinalCoverGate(
        selection(song_id),
        difficulty="Expert",
        observed_level=27,
        observed_title="SAVIOR OF SONG",
    )

    assert missing_title.observe(image) is None
    assert "title" in missing_title.last_reason
    assert wrong_level.observe(image) is None
    assert "level" in wrong_level.last_reason


def test_final_cover_does_not_accept_a_distinct_jacket():
    image, _ = final_cover_frame(seed=83)
    _, expected_song_id = final_cover_frame(seed=7)
    gate = FinalCoverGate(
        selection(expected_song_id),
        difficulty="Expert",
        observed_level=28,
        observed_title="SAVIOR OF SONG",
    )

    assert gate.observe(image) is None
    assert gate.confirmed is False


def test_unique_jacket_does_not_depend_on_noisy_title_ocr():
    image, song_id = final_cover_frame()
    chart = selection(song_id)
    chart.shared_jacket = False
    gate = FinalCoverGate(
        chart,
        difficulty="Expert",
        observed_level=28,
        observed_title="乱码标题",
    )

    assert gate.observe(image) is not None


def test_wait_for_final_cover_uses_the_controller_frame_stream():
    loading = np.zeros((720, 1280, 3), dtype=np.uint8)
    cover, song_id = final_cover_frame()

    class Job:
        def __init__(self, image):
            self.image = image

        def wait(self):
            return self

        def get(self):
            return self.image

    class Controller:
        def __init__(self):
            self.frames = iter((loading, cover))

        def post_screencap(self):
            return Job(next(self.frames))

    outcome = wait_for_final_cover(
        Controller(),
        SimpleNamespace(song_level=28, song_title="SAVIOR OF SONG"),
        selection(song_id),
        "Expert",
        lambda: False,
        timeout_seconds=1,
        poll_interval_seconds=0,
    )

    assert outcome.status == "confirmed"
    assert outcome.resolution.confirmation.bestdori_song_id == 306
    assert outcome.resolution.selection.bestdori_song_id == 306


def test_wait_for_final_cover_matches_cover_after_black_transition():
    black = np.zeros((720, 1280, 3), dtype=np.uint8)
    cover, _song_id = final_cover_frame()

    class Job:
        def __init__(self, image):
            self.image = image

        def wait(self):
            return self

        def get(self):
            return self.image

    class Controller:
        def __init__(self):
            self.frames = iter((black, black, cover, cover))

        def post_screencap(self):
            return Job(next(self.frames))

    outcome = wait_for_final_cover(
        Controller(),
        SimpleNamespace(song_level=28, song_title="SAVIOR OF SONG"),
        selection(_song_id),
        "Expert",
        lambda: False,
        timeout_seconds=1,
        poll_interval_seconds=0,
    )

    assert outcome.status == "confirmed"


def test_wait_for_final_cover_defers_playfield_bail_through_black_transition(
    monkeypatch,
):
    black = np.zeros((720, 1280, 3), dtype=np.uint8)
    playfield, _ = final_cover_frame(seed=83)
    clock = {"value": 0.0}
    consumed = []
    monkeypatch.setattr(
        profile_play_action.time,
        "monotonic",
        lambda: clock["value"],
    )

    class Repository:
        def resolve(self, *_args, **_kwargs):
            return ChartResolution(None, "no matching chart")

    class Job:
        def __init__(self, image):
            self.image = image

        def wait(self):
            return self

        def get(self):
            return self.image

    class Controller:
        def __init__(self):
            self.frames = iter((black, playfield, playfield, playfield,
                                playfield, playfield, playfield, playfield))

        def post_screencap(self):
            image = next(self.frames)
            consumed.append(image)
            clock["value"] += 0.1
            return Job(image)

    monkeypatch.setattr(
        "agent.realtime.profile_play_action.PlayfieldDetector",
        lambda: (lambda _image: True),
    )

    outcome = wait_for_final_cover(
        Controller(),
        SimpleNamespace(song_level=28, song_title="SAVIOR OF SONG"),
        None,
        "Expert",
        lambda: False,
        repository=Repository(),
        timeout_seconds=2,
        poll_interval_seconds=0,
    )

    assert outcome.status == "degraded-visual-legacy"
    # 黑场之后不能在第 2 帧立刻放弃，必须等密集采样窗口结束。
    assert len(consumed) >= 4


def test_wait_for_final_cover_can_defer_chart_resolution_until_coop_cover():
    loading = np.zeros((720, 1280, 3), dtype=np.uint8)
    cover, song_id = final_cover_frame()
    resolved = selection(song_id)
    resolved.shared_jacket = False
    calls = []

    class Repository:
        def resolve(self, fingerprint, difficulty, *, level, title):
            calls.append((fingerprint, difficulty, level, title))
            return ChartResolution(resolved, "confirmed local chart")

    class Job:
        def __init__(self, image):
            self.image = image

        def wait(self):
            return self

        def get(self):
            return self.image

    class Controller:
        def __init__(self):
            self.frames = iter((loading, cover, cover))

        def post_screencap(self):
            return Job(next(self.frames))

    outcome = wait_for_final_cover(
        Controller(),
        SimpleNamespace(song_level=28, song_title="乱码标题"),
        None,
        "Expert",
        lambda: False,
        repository=Repository(),
        timeout_seconds=1,
        poll_interval_seconds=0,
    )

    assert outcome.status == "confirmed"
    assert outcome.resolution.confirmation.song_id == song_id
    assert outcome.resolution.selection is resolved
    assert calls == [(song_id, "expert", 28, "乱码标题")]


def test_wait_for_final_cover_degrades_to_selected_chart_when_playfield_arrives(
    monkeypatch,
):
    image, expected_song_id = final_cover_frame(seed=7)
    wrong_playfield, _ = final_cover_frame(seed=83)

    class Job:
        def wait(self):
            return self

        def get(self):
            return wrong_playfield

    class Controller:
        def post_screencap(self):
            return Job()

    monkeypatch.setattr(
        "agent.realtime.profile_play_action.PlayfieldDetector",
        lambda: (lambda _image: True),
    )

    outcome = wait_for_final_cover(
        Controller(),
        SimpleNamespace(song_level=28, song_title="SAVIOR OF SONG"),
        selection(expected_song_id),
        "Expert",
        lambda: False,
        timeout_seconds=1,
        poll_interval_seconds=0,
    )

    assert outcome.status == "degraded-selected-chart"
    assert outcome.resolution is None
    assert outcome.playfield_seen is True
    assert "does not match" in outcome.reason


def test_wait_for_final_cover_degrades_to_visual_legacy_without_a_chart(
    monkeypatch,
):
    playfield, _ = final_cover_frame(seed=83)

    class Repository:
        def resolve(self, *_args, **_kwargs):
            return ChartResolution(None, "no matching chart")

    class Job:
        def wait(self):
            return self

        def get(self):
            return playfield

    class Controller:
        def post_screencap(self):
            return Job()

    monkeypatch.setattr(
        "agent.realtime.profile_play_action.PlayfieldDetector",
        lambda: (lambda _image: True),
    )

    outcome = wait_for_final_cover(
        Controller(),
        SimpleNamespace(song_level=28, song_title="SAVIOR OF SONG"),
        None,
        "Expert",
        lambda: False,
        repository=Repository(),
        timeout_seconds=1,
        poll_interval_seconds=0,
    )

    assert outcome.status == "degraded-visual-legacy"
    assert outcome.resolution is None
    assert outcome.playfield_seen is True


def test_wait_for_final_cover_keeps_existing_chart_during_independent_coop_scan(
    monkeypatch,
):
    playfield, _ = final_cover_frame(seed=83)

    class Repository:
        def resolve(self, *_args, **_kwargs):
            return ChartResolution(None, "no matching chart")

    class Job:
        def wait(self):
            return self

        def get(self):
            return playfield

    class Controller:
        def post_screencap(self):
            return Job()

    monkeypatch.setattr(
        "agent.realtime.profile_play_action.PlayfieldDetector",
        lambda: (lambda _image: True),
    )

    outcome = wait_for_final_cover(
        Controller(),
        SimpleNamespace(song_level=28, song_title="SAVIOR OF SONG"),
        None,
        "Expert",
        lambda: False,
        repository=Repository(),
        timeout_seconds=1,
        poll_interval_seconds=0,
        fallback_selection_available=True,
    )

    assert outcome.status == "degraded-selected-chart"
    assert outcome.resolution is None


def test_deferred_resolution_rejects_title_fallback_for_wrong_jacket():
    wrong_cover, wrong_song_id = final_cover_frame(seed=83)
    _, expected_song_id = final_cover_frame(seed=7)
    resolved = selection(expected_song_id)
    resolved.shared_jacket = False

    class Repository:
        def resolve(self, fingerprint, difficulty, *, level, title):
            assert fingerprint == wrong_song_id
            return ChartResolution(resolved, "confirmed by title fallback")

    resolver = FinalCoverResolver(
        difficulty="Expert",
        observed_level=28,
        observed_title="SAVIOR OF SONG",
        repository=Repository(),
    )

    assert resolver.observe(wrong_cover) is None
    assert resolver.observe(wrong_cover) is None
    assert resolver.last_reason == "final cover jacket does not match selected chart"
