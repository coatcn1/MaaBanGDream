from __future__ import annotations

from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from agent.realtime import profile_play_action
from agent.realtime.chart_repository import ChartResolution
from agent.realtime.final_cover import FinalCoverGate, FinalCoverResolver
from agent.realtime.profile_play_action import wait_for_final_cover
from agent.realtime.song_identity import (
    FINAL_SONG_JACKET_ROI,
    detect_full_badge,
    fingerprint_jacket,
)


@pytest.mark.parametrize("supply_preflight_black", [False, True])
def test_ordered_startup_ignores_ready_page_until_black(monkeypatch, supply_preflight_black):
    cover, song_id = final_cover_frame()
    black = np.zeros_like(cover)
    clock = [0.0]
    observed = []
    # 即使准备页同时误命中封面和演奏场，也必须先观察本局黑场。
    frames = iter([cover, cover, black, cover] if not supply_preflight_black else [cover])

    class Controller:
        def post_screencap(self):
            clock[0] += 0.1
            image = next(frames)
            return SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: image))

    monkeypatch.setattr(profile_play_action.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(profile_play_action, "PlayfieldDetector", lambda: lambda _: True)
    outcome = wait_for_final_cover(
        Controller(), SimpleNamespace(song_level=28, song_title="SAVIOR OF SONG"),
        selection(song_id), "Expert", lambda: False,
        timeout_seconds=1, poll_interval_seconds=0, require_black_transition=True,
        initial_image=black if supply_preflight_black else None,
        observer=lambda image, now, detail: observed.append(detail["status"]),
    )
    assert outcome.status == "confirmed"
    assert observed == (["black-transition", "confirmed"] if supply_preflight_black else
                        ["waiting-black", "waiting-black", "black-transition", "confirmed"])


def test_ordered_startup_never_degrades_or_completes_without_black(monkeypatch):
    ready, song_id = final_cover_frame()
    clock = [0.0]

    class Controller:
        def post_screencap(self):
            clock[0] += 0.2
            return SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: ready))

    monkeypatch.setattr(profile_play_action.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(profile_play_action, "PlayfieldDetector", lambda: lambda _: True)
    with pytest.raises(RuntimeError, match="全黑开演转场"):
        wait_for_final_cover(
            Controller(), SimpleNamespace(song_level=28, song_title="SAVIOR OF SONG"),
            selection(song_id), "Expert", lambda: False,
            timeout_seconds=1, poll_interval_seconds=0, require_black_transition=True,
        )


def test_ordered_startup_stop_does_not_capture_or_fallback():
    with pytest.raises(InterruptedError):
        wait_for_final_cover(
            None, SimpleNamespace(song_level=28, song_title="SAVIOR OF SONG"),
            selection(final_cover_frame()[1]), "Expert", lambda: True,
            require_black_transition=True,
        )


def test_opening_black_after_false_ready_playfield_is_not_completion(monkeypatch):
    ready, song_id = final_cover_frame()
    black = np.zeros_like(ready)
    clock = [0.0]
    states = []

    class Controller:
        def post_screencap(self):
            clock[0] += 0.1
            image = ready if clock[0] < 0.3 else black
            return SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: image))

    monkeypatch.setattr(profile_play_action.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(profile_play_action, "PlayfieldDetector", lambda: lambda image: image is ready)
    with pytest.raises(RuntimeError, match="黑场后的歌曲封面或完整演奏场"):
        wait_for_final_cover(
            Controller(), SimpleNamespace(song_level=28, song_title="SAVIOR OF SONG"),
            selection(song_id), "Expert", lambda: False,
            timeout_seconds=1, poll_interval_seconds=0, require_black_transition=True,
            observer=lambda image, now, detail: states.append(detail["status"]),
        )
    assert states[:2] == ["waiting-black", "waiting-black"]
    assert set(states[2:]) == {"black-transition"}


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


def full_badged_frame(
    seed: int = 7,
    with_badge: bool = True,
) -> tuple[np.ndarray, str]:
    """构造带/不带 FULL 徽标的最终封面帧，模拟单人 FULL 谱面右上角徽标。"""
    image, _ = final_cover_frame(seed)
    if with_badge:
        x, y, width, height = FINAL_SONG_JACKET_ROI
        left = x + width - 46
        image[y:y + 30, left:x + width] = (70, 70, 70)
        cv2.rectangle(
            image, (left, y), (x + width - 1, y + 29),
            (240, 240, 240), 2,
        )
        cv2.putText(
            image, "FULL", (left + 5, y + 22),
            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1,
            cv2.LINE_AA,
        )
    # 指纹按含徽标的最终画面计算，模拟目录指纹与实时封面在容差内一致。
    x, y, width, height = FINAL_SONG_JACKET_ROI
    song_id = fingerprint_jacket(image[y:y + height, x:x + width]).song_id
    return image, song_id


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


def test_shared_jacket_level_unique_allows_broken_title_ocr():
    image, song_id = final_cover_frame()
    chart = selection(song_id)
    chart.shared_jacket = True
    chart.shared_jacket_level_unique = True
    gate = FinalCoverGate(
        chart,
        difficulty="Expert",
        observed_level=28,
        observed_title="E",
    )

    assert gate.observe(image) is not None


def test_shared_jacket_same_level_still_requires_title():
    image, song_id = final_cover_frame()
    chart = selection(song_id)
    chart.shared_jacket = True
    chart.shared_jacket_level_unique = False
    gate = FinalCoverGate(
        chart,
        difficulty="Expert",
        observed_level=28,
        observed_title="E",
    )

    assert gate.observe(image) is None
    assert "title" in gate.last_reason


def test_full_badge_detection_distinguishes_badged_cover():
    badged, _ = full_badged_frame(with_badge=True)
    plain, _ = full_badged_frame(with_badge=False)

    assert detect_full_badge(badged) is True
    assert detect_full_badge(plain) is False


def test_shared_jacket_full_song_uses_cover_badge_when_title_fails():
    image, song_id = full_badged_frame(with_badge=True)
    chart = selection(song_id)
    chart.shared_jacket = True
    chart.shared_jacket_level_unique = False
    chart.title = "[FULL]FIRE BIRD"
    chart.titles = ("[FULL]FIRE BIRD",)
    gate = FinalCoverGate(
        chart,
        difficulty="Expert",
        observed_level=28,
        observed_title="E",
    )

    assert gate.observe(image) is not None
    assert gate.last_reason == "confirmed"


def test_shared_jacket_full_song_without_badge_still_unconfirmed():
    image, song_id = full_badged_frame(with_badge=False)
    chart = selection(song_id)
    chart.shared_jacket = True
    chart.shared_jacket_level_unique = False
    chart.title = "[FULL]FIRE BIRD"
    chart.titles = ("[FULL]FIRE BIRD",)
    gate = FinalCoverGate(
        chart,
        difficulty="Expert",
        observed_level=28,
        observed_title="E",
    )

    assert gate.observe(image) is None
    assert "badge" in gate.last_reason


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
