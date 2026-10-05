from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from agent.realtime import profile_play_action
from agent.realtime import final_cover
from agent.realtime import chart_repository, runtime_flags
from agent.realtime.chart_repository import ChartResolution, LocalChartRepository
from agent.realtime.final_cover import (
    FinalCoverConfirmation,
    FinalCoverGate,
    FinalCoverResolution,
    FinalCoverResolver,
)
from agent.realtime.profile_play_action import wait_for_final_cover
from agent.realtime.song_identity import (
    FINAL_SONG_JACKET_ROI,
    detect_full_badge,
    fingerprint_jacket,
)
from agent.realtime.song_title_ocr import (
    FINAL_COVER_TITLE_ROI,
    TitleReading,
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


def test_final_cover_consumes_preconfirmed_cooperative_evidence_without_capture():
    cover, song_id = final_cover_frame()
    selected = selection(song_id)
    resolution = FinalCoverResolution(
        confirmation=FinalCoverConfirmation(
            song_id=song_id,
            song_id_method="song-jacket-phash-v2",
            bestdori_song_id=selected.bestdori_song_id,
        ),
        selection=selected,
    )
    observed = []

    class Controller:
        def post_screencap(self):
            raise AssertionError("已确认的封面证据不应再次截图等待")

    outcome = wait_for_final_cover(
        Controller(),
        SimpleNamespace(song_level=28, song_title="SAVIOR OF SONG"),
        selected,
        "Expert",
        lambda: False,
        initial_image=cover,
        initial_resolution=resolution,
        observer=lambda image, now, detail: observed.append(detail),
    )

    assert outcome.status == "confirmed"
    assert outcome.resolution is resolution
    assert observed[0]["status"] == "confirmed"
    assert observed[0]["source"] == "preconfirmed-transition"


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


def loading_frame(position=(20, 600)):
    image, song_id = final_cover_frame()
    template = final_cover.member_loading_icon()
    x, y = position
    image[y:y + template.shape[0], x:x + template.shape[1]] = template
    return image, song_id


@pytest.mark.parametrize("position", [(20, 600), (20, 280)])
def test_member_loading_icon_is_found_after_panel_moves(position):
    image, _ = loading_frame(position)
    assert final_cover.is_member_loading_screen(image)
    assert not final_cover.is_member_loading_screen(final_cover_frame()[0])


def test_missing_loading_guard_template_fails_closed(monkeypatch):
    final_cover.member_loading_icon.cache_clear()
    monkeypatch.setattr(final_cover, "imread_unicode", lambda *args: None)
    cover, song_id = final_cover_frame()
    resolver = FinalCoverResolver(
        difficulty="Expert", observed_level=28, observed_title="SAVIOR OF SONG",
        selection=selection(song_id),
    )
    assert resolver.observe(cover) is not None
    with pytest.raises(RuntimeError, match="模板缺失或损坏"):
        FinalCoverResolver(
            difficulty="Expert", observed_level=28, observed_title="SAVIOR OF SONG",
            selection=selection(song_id), reject_member_loading=True,
        )


def test_loading_guard_rejects_even_a_matching_selected_jacket():
    loading, song_id = loading_frame()
    resolver = FinalCoverResolver(
        difficulty="Expert", observed_level=28, observed_title="SAVIOR OF SONG",
        selection=selection(song_id), reject_member_loading=True,
    )
    assert resolver.observe(loading) is None
    assert resolver.last_reason == "member loading screen"
    assert resolver.observe(final_cover_frame()[0]) is not None


def test_loading_frame_interrupts_continuous_deferred_cover_candidate():
    loading, song_id = loading_frame()
    selected = selection(song_id)

    class Repository:
        def resolve(self, *args, **kwargs):
            return ChartResolution(selected, "confirmed")

    resolver = FinalCoverResolver(
        difficulty="Expert", observed_level=28, observed_title="SAVIOR OF SONG",
        repository=Repository(), reject_member_loading=True,
    )
    cover = final_cover_frame()[0]
    assert resolver.observe(cover) is None
    assert resolver.observe(loading) is None
    assert resolver.observe(cover) is None
    assert resolver.observe(cover) is not None


@pytest.mark.parametrize("mode", ["cooperative", "formal", "challenge", "medley", "continuous"])
def test_normal_final_cover_wait_applies_loading_guard_only_to_cooperative(monkeypatch, mode):
    monkeypatch.delenv("MAABANGDREAM_COOPERATIVE_MEMBER_LOADING_GUARD_TRIAL", raising=False)
    loading, song_id = loading_frame()
    images = iter([loading, final_cover_frame()[0]])
    captured = []

    class Controller:
        def post_screencap(self):
            image = next(images)
            captured.append(image)
            return SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: image))

    outcome = wait_for_final_cover(
        Controller(), SimpleNamespace(mode=mode, song_level=28, song_title="SAVIOR OF SONG"),
        selection(song_id), "Expert", lambda: False,
        timeout_seconds=1, poll_interval_seconds=0,
    )
    assert outcome.status == "confirmed"
    assert len(captured) == (2 if mode == "cooperative" else 1)


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("position", [(20, 600), (20, 280)])
def test_both_cooperative_cover_paths_consume_persisted_guard(monkeypatch, tmp_path, enabled, native, position):
    from agent.realtime import cooperative_action
    from agent.realtime.profile_store import RealtimeProfileStore
    monkeypatch.setenv("MAABANGDREAM_COOPERATIVE_MEMBER_LOADING_GUARD_TRIAL", "1")
    store = RealtimeProfileStore(tmp_path / "profiles")
    store.update_runtime_options({
        "cooperative_member_loading_guard_enabled": enabled, "native_realtime_enabled": native,
    })
    monkeypatch.setattr(cooperative_action, "PROJECT_ROOT", tmp_path)
    loading, song_id = loading_frame(position)
    selected = selection(song_id)

    class Repository:
        def resolve(self, *args, **kwargs):
            return ChartResolution(selected, "confirmed")

    monkeypatch.setattr(cooperative_action, "LocalChartRepository", lambda *args: Repository())
    run = SimpleNamespace(mode="cooperative", difficulty="Expert", song_level=28,
                          song_title="SAVIOR OF SONG", song_title_confidence=0.9)
    monkeypatch.setattr(cooperative_action, "current_live_run", lambda: run)
    flow = cooperative_action.CooperativeLiveFlow(
        SimpleNamespace(), dict(cooperative_action.DEFAULT_SETTINGS),
    )
    resolver = flow.make_final_cover_entry_resolver()
    assert resolver.reject_member_loading is enabled
    assert resolver.observe(loading) is None
    assert (resolver.observe(loading) is None) is enabled
    if enabled:
        assert resolver.last_reason == "member loading screen"
    resolver = flow.make_final_cover_entry_resolver()
    assert resolver.observe(final_cover_frame()[0]) is None
    assert resolver.observe(final_cover_frame()[0]) is not None

    # 在第二条门禁开始前修改文件，仍应沿用同一任务的快照。
    store.update_runtime_options({"cooperative_member_loading_guard_enabled": not enabled})
    params = cooperative_action.cooperative_play_params(flow.settings)
    images = iter([loading, final_cover_frame()[0]])
    captured = []

    class Controller:
        def post_screencap(self):
            image = next(images)
            captured.append(image)
            return SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: image))

    outcome = wait_for_final_cover(
        Controller(), run, selected, "Expert", lambda: False,
        timeout_seconds=1, poll_interval_seconds=0,
        cooperative_member_loading_guard_enabled=params["cooperative_member_loading_guard_enabled"],
    )
    assert outcome.status == "confirmed"
    assert len(captured) == (2 if enabled else 1)


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


def test_final_cover_accepts_small_jacket_flip_when_level_matches():
    # 最终封面裁切/缩放会让个别谱面稳定多翻转几 bit（Little Busters!
    # 实测 10 bit）；等级硬约束已通过时应与仓库宽阈值一致，而不是用
    # 8 bit 复核把同一首歌拒绝掉。
    image, song_id = final_cover_frame()

    def flipped_digest(bits: int) -> str:
        prefix, digest = song_id.rsplit("-", 1)
        value = int(digest, 16)
        for index in range(bits):
            value ^= 1 << index
        return f"{prefix}-{value:016x}"

    chart = selection(song_id)
    chart.fingerprints = (flipped_digest(10),)
    gate = FinalCoverGate(
        chart,
        difficulty="Expert",
        observed_level=28,
        observed_title="SAVIOR OF SONG",
    )

    confirmation = gate.observe(image)

    assert confirmation is not None
    assert gate.last_reason == "confirmed"


def test_final_cover_rejects_jacket_flip_beyond_loose_threshold():
    image, song_id = final_cover_frame()
    prefix, digest = song_id.rsplit("-", 1)
    value = int(digest, 16)
    for index in range(20):
        value ^= 1 << index
    chart = selection(song_id)
    chart.fingerprints = (f"{prefix}-{value:016x}",)
    gate = FinalCoverGate(
        chart,
        difficulty="Expert",
        observed_level=28,
        observed_title="SAVIOR OF SONG",
    )

    assert gate.observe(image) is None
    assert "does not match" in gate.last_reason


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


def test_refresh_observed_title_only_upgrades_validated_confidence(tmp_path):
    chart = [
        {"type": "BPM", "beat": 0, "bpm": 120},
        {"type": "Single", "beat": 1, "lane": 2},
    ]
    digest = hashlib.sha256(
        json.dumps(
            chart,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    (tmp_path / "bestdori" / "50").mkdir(parents=True)
    (tmp_path / "bestdori" / "50" / "expert.json").write_text(
        json.dumps({
            "schema_version": 1,
            "source": {"provider": "bestdori", "chart_sha256": digest},
            "song": {"bestdori_id": 50, "titles": ["FIRE BIRD"]},
            "difficulty": {"name": "expert", "level": 28},
            "chart": chart,
        }),
        encoding="utf-8",
    )
    (tmp_path / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "songs": [{
            "bestdori_song_id": 50,
            "display_title": "FIRE BIRD",
            "titles": ["FIRE BIRD"],
            "fingerprints": ["song-jacket-phash-v2-0123456789abcdef"],
            "difficulties": {
                "expert": {
                    "path": "bestdori/50/expert.json",
                    "level": 28,
                    "chart_sha256": digest,
                }
            },
        }],
    }), encoding="utf-8")
    resolver = FinalCoverResolver(
        difficulty="Expert",
        observed_level=28,
        observed_title="E",
        observed_title_confidence=0.3,
        repository=LocalChartRepository(tmp_path),
    )

    assert resolver.observed_title == "E"
    assert resolver.refresh_observed_title("FIRE BIRD", 0.9) is True
    assert resolver.observed_title == "FIRE BIRD"
    # 相同文本或更低置信度都不覆盖；无法唯一匹配曲目的垃圾读数也忽略。
    assert resolver.refresh_observed_title("FIRE BIRD", 0.99) is False
    assert resolver.refresh_observed_title("OTHER", 0.5) is False
    assert resolver.refresh_observed_title("目标得分", 0.99) is False
    assert resolver.observed_title == "FIRE BIRD"


@pytest.mark.parametrize("second_page", ["title", "goal"])
@pytest.mark.parametrize("entry", ["repository", "agent"])
def test_deferred_resolver_confirms_unique_cover_with_regional_level_drift(
    tmp_path, monkeypatch, second_page, entry,
):
    """唯一封面和最终实读标题一致时允许 1 级差；默认曲库与无参数 Agent
    两条启动路径都必须取得相同确认结果。
    """
    monkeypatch.setattr(chart_repository, "_regional_level_drift_trial_configured", None)
    monkeypatch.delenv(chart_repository.REGIONAL_LEVEL_DRIFT_TRIAL_ENV, raising=False)
    if entry == "agent":
        monkeypatch.setattr(runtime_flags, "_native_timing_trial_enabled", runtime_flags._native_timing_trial_enabled)
        monkeypatch.setattr(runtime_flags, "cooperative_member_loading_guard_enabled", lambda: True)
        assert runtime_flags.configure_agent_runtime_flags([])["regional_level_drift_trial"] is True
    cover, song_id = final_cover_frame()
    second = cover.copy()
    if second_page == "goal":
        cv2.circle(second, (382, 515), 26, (90, 200, 240), -1)
        cv2.putText(second, "5082000", (738, 530), cv2.FONT_HERSHEY_SIMPLEX, .9,
                    (180, 140, 250), 2)
    chart = [
        {"type": "BPM", "beat": 0, "bpm": 120},
        {"type": "Single", "beat": 1, "lane": 2},
    ]
    digest = hashlib.sha256(
        json.dumps(
            chart,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    (tmp_path / "bestdori" / "571").mkdir(parents=True)
    (tmp_path / "bestdori" / "571" / "expert.json").write_text(
        json.dumps({
            "schema_version": 1,
            "source": {"provider": "bestdori", "chart_sha256": digest},
            "song": {"bestdori_id": 571, "titles": ["グッド・バイ"]},
            "difficulty": {"name": "expert", "level": 27},
            "chart": chart,
        }),
        encoding="utf-8",
    )
    (tmp_path / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "songs": [{
            "bestdori_song_id": 571,
            "display_title": "グッド・バイ",
            "titles": ["グッド・バイ"],
            "fingerprints": [song_id],
            "difficulties": {
                "expert": {
                    "path": "bestdori/571/expert.json",
                    "level": 27,
                    "chart_sha256": digest,
                }
            },
        }],
    }), encoding="utf-8")
    def reader(image, **_kwargs):
        if image is second:
            raise AssertionError("第二帧应消费本局首帧实读标题，不能把目标得分当歌名")
        return TitleReading("グッド・バイ", 0.99)
    monkeypatch.setattr(final_cover, "recognize_song_title", reader)
    resolver = FinalCoverResolver(
        difficulty="Expert",
        observed_level=26,
        observed_title="准备页乱码",
        repository=LocalChartRepository(tmp_path),
        require_observed_title=True,
    )

    assert resolver.observe(cover) is None
    resolution = resolver.observe(second)

    assert resolution is not None
    assert resolution.selection.bestdori_song_id == 571
    assert resolution.confirmation.bestdori_song_id == 571
    assert resolution.selection.shared_jacket is False
    assert resolution.final_title_confirmed is True
    assert resolution.observed_title == "グッド・バイ"
    assert resolution.selection.level_drift_tolerated is True


def test_deferred_resolver_still_rejects_shared_cover_level_conflict(tmp_path, monkeypatch):
    """共享封面（[FULL]/English/SPECIAL 与原版共用封面）等级仍是唯一判别
    信号：观测等级只差 1 级也不能按区服漂移放行。"""
    cover, song_id = final_cover_frame()
    chart = [
        {"type": "BPM", "beat": 0, "bpm": 120},
        {"type": "Single", "beat": 1, "lane": 2},
    ]
    digest = hashlib.sha256(
        json.dumps(
            chart,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    (tmp_path / "bestdori" / "187").mkdir(parents=True)
    (tmp_path / "bestdori" / "187" / "expert.json").write_text(
        json.dumps({
            "schema_version": 1,
            "source": {"provider": "bestdori", "chart_sha256": digest},
            "song": {"bestdori_id": 187, "titles": ["FIRE BIRD"]},
            "difficulty": {"name": "expert", "level": 27},
            "chart": chart,
        }),
        encoding="utf-8",
    )
    (tmp_path / "bestdori" / "243").mkdir(parents=True)
    (tmp_path / "bestdori" / "243" / "expert.json").write_text(
        json.dumps({
            "schema_version": 1,
            "source": {"provider": "bestdori", "chart_sha256": digest},
            "song": {"bestdori_id": 243, "titles": ["[FULL]FIRE BIRD"]},
            "difficulty": {"name": "expert", "level": 28},
            "chart": chart,
        }),
        encoding="utf-8",
    )
    (tmp_path / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "songs": [
            {
                "bestdori_song_id": 187,
                "display_title": "FIRE BIRD",
                "titles": ["FIRE BIRD"],
                "fingerprints": [song_id],
                "difficulties": {
                    "expert": {
                        "path": "bestdori/187/expert.json",
                        "level": 27,
                        "chart_sha256": digest,
                    }
                },
            },
            {
                "bestdori_song_id": 243,
                "display_title": "[FULL]FIRE BIRD",
                "titles": ["[FULL]FIRE BIRD"],
                "fingerprints": [song_id],
                "difficulties": {
                    "expert": {
                        "path": "bestdori/243/expert.json",
                        "level": 28,
                        "chart_sha256": digest,
                    }
                },
            },
        ],
    }), encoding="utf-8")
    monkeypatch.setattr(final_cover, "recognize_song_title", lambda *_args, **_kwargs: TitleReading("[FULL]FIRE BIRD", 0.99))
    resolver = FinalCoverResolver(
        difficulty="Expert",
        observed_level=26,
        observed_title="[FULL]FIRE BIRD",
        repository=LocalChartRepository(tmp_path, regional_level_drift_enabled=True),
    )

    assert resolver.observe(cover) is None
    assert resolver.observe(cover) is None
    assert resolver.last_reason == (
        "selected song level does not match local chart metadata"
    )


def _regional_drift_test_repository(tmp_path, *, fingerprint_mask=0):
    """构造封面唯一、全局等级 27 的谱面，观测场景使用等级 26。"""
    image, fingerprint = final_cover_frame()
    prefix, digest = fingerprint.rsplit("-", 1)
    stored = f"{prefix}-{int(digest, 16) ^ fingerprint_mask:016x}"
    chart = [
        {"type": "BPM", "beat": 0, "bpm": 120},
        {"type": "Single", "beat": 1, "lane": 2},
    ]
    digest = hashlib.sha256(json.dumps(
        chart, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    chart_path = tmp_path / "bestdori/571/expert.json"
    chart_path.parent.mkdir(parents=True)
    chart_path.write_text(json.dumps({
        "schema_version": 1,
        "source": {"provider": "bestdori", "chart_sha256": digest},
        "song": {"bestdori_id": 571, "titles": ["グッド・バイ"]},
        "difficulty": {"name": "expert", "level": 27},
        "chart": chart,
    }), encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "songs": [{
            "bestdori_song_id": 571,
            "display_title": "グッド・バイ",
            "titles": ["グッド・バイ"],
            "fingerprints": [stored],
            "difficulties": {"expert": {
                "path": "bestdori/571/expert.json", "level": 27,
                "chart_sha256": digest,
            }},
        }],
    }), encoding="utf-8")
    return image, LocalChartRepository(tmp_path, regional_level_drift_enabled=True)


def test_final_title_refresh_ignores_level_and_preparation_confidence(tmp_path, monkeypatch):
    image, repository = _regional_drift_test_repository(tmp_path)
    resolver = FinalCoverResolver(
        difficulty="Expert", observed_level=26,
        observed_title="Wrong Preparation Title", observed_title_confidence=0.99,
        repository=repository,
    )
    monkeypatch.setattr(final_cover, "recognize_song_title", lambda *_args, **_kwargs: None)
    assert resolver.observe(image) is None
    assert resolver.refresh_observed_title("グッド・バイ", 0.8) is True
    assert resolver.observed_title_confidence == pytest.approx(0.8)
    resolution = resolver.observe(image)
    assert resolution is not None
    assert resolution.selection.bestdori_song_id == 571
    assert resolution.selection.level_drift_tolerated is True
    assert resolution.final_title_confirmed is True


@pytest.mark.parametrize("reading", [None, TitleReading("グッド・バイ", 0.69)])
def test_drift_needs_final_title_despite_trusted_preparation_title(tmp_path, monkeypatch, reading):
    image, repository = _regional_drift_test_repository(tmp_path)
    resolver = FinalCoverResolver(
        difficulty="Expert", observed_level=26,
        observed_title="グッド・バイ", observed_title_confidence=0.99,
        repository=repository,
    )
    monkeypatch.setattr(final_cover, "recognize_song_title", lambda *_args, **_kwargs: reading)
    assert resolver.observe(image) is None
    assert resolver.observe(image) is None
    assert resolver.last_reason == "final cover title is not confirmed"
    monkeypatch.setattr(final_cover, "recognize_song_title", lambda *_args, **_kwargs: TitleReading("グッド・バイ", 0.8))
    assert resolver.observe(image).final_title_confirmed is True


def test_approximate_cover_with_conflicting_final_title_is_hard_failure(tmp_path, monkeypatch):
    image, repository = _regional_drift_test_repository(tmp_path, fingerprint_mask=0xff)
    resolver = FinalCoverResolver(
        difficulty="Expert", observed_level=26,
        observed_title="グッド・バイ", observed_title_confidence=0.99,
        repository=repository,
    )
    monkeypatch.setattr(final_cover, "recognize_song_title", lambda *_args, **_kwargs: TitleReading("Completely Different Tune", 0.99))
    assert resolver.observe(image) is None
    with pytest.raises(RuntimeError, match="最终封面歌曲身份冲突"):
        resolver.observe(image)


def test_wait_for_final_cover_cannot_degrade_after_title_conflict(tmp_path, monkeypatch):
    image, repository = _regional_drift_test_repository(tmp_path, fingerprint_mask=0xff)
    calls = []
    clock = [0.0]

    class Controller:
        def post_screencap(self):
            calls.append("capture")
            clock[0] += 0.1
            return SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: image))

    monkeypatch.setattr(profile_play_action.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(profile_play_action, "PlayfieldDetector", lambda: (lambda _image: True))
    monkeypatch.setattr(final_cover, "recognize_song_title", lambda *_args, **_kwargs: TitleReading("Completely Different Tune", 0.99))
    with pytest.raises(RuntimeError, match="最终封面歌曲身份冲突"):
        wait_for_final_cover(
            Controller(), SimpleNamespace(song_level=26, song_title="グッド・バイ", song_title_confidence=0.99),
            None, "Expert", lambda: False, repository=repository,
            timeout_seconds=1, poll_interval_seconds=0,
            fallback_selection_available=True,
        )
    assert calls == ["capture", "capture"]


def test_drift_gate_rejects_loose_cover_after_level_was_tolerated(tmp_path):
    image, repository = _regional_drift_test_repository(tmp_path, fingerprint_mask=0x3ff)
    fingerprint = repository._load_manifest()["songs"][0]["fingerprints"][0]
    resolution = repository.resolve(fingerprint, "Expert", level=26, title="グッド・バイ")
    assert resolution.selection is not None
    gate = FinalCoverGate(
        resolution.selection, difficulty="Expert", observed_level=26,
        observed_title="グッド・バイ",
    )
    assert gate.observe(image) is None
    assert gate.last_reason == "final cover jacket does not match selected chart"


def test_drift_preconfirmed_result_without_final_title_is_not_reused(tmp_path, monkeypatch):
    image, repository = _regional_drift_test_repository(tmp_path)
    fingerprint = repository._load_manifest()["songs"][0]["fingerprints"][0]
    chart = repository.resolve(fingerprint, "Expert", level=26, title="グッド・バイ").selection
    initial = FinalCoverResolution(
        FinalCoverConfirmation(fingerprint, "song-jacket-phash-v2", 571), chart,
        observed_title="グッド・バイ", observed_title_confidence=0.99,
    )
    calls = []
    clock = [0.0]

    class Controller:
        def post_screencap(self):
            calls.append("capture")
            clock[0] += 0.1
            return SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: image))

    monkeypatch.setattr(profile_play_action.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(profile_play_action, "PlayfieldDetector", lambda: (lambda _image: False))
    monkeypatch.setattr(final_cover, "recognize_song_title", lambda *_args, **_kwargs: TitleReading("グッド・バイ", 0.8))
    outcome = wait_for_final_cover(
        Controller(), SimpleNamespace(song_level=26, song_title="グッド・バイ", song_title_confidence=0.99),
        chart, "Expert", lambda: False, repository=repository,
        initial_image=image, initial_resolution=initial,
        timeout_seconds=1, poll_interval_seconds=0,
    )
    assert calls
    assert outcome.status == "confirmed"
    assert outcome.resolution.final_title_confirmed is True
    assert outcome.resolution.observed_title_confidence == pytest.approx(0.8)


def test_drift_final_cover_stop_does_not_capture_or_ocr(tmp_path, monkeypatch):
    _image, repository = _regional_drift_test_repository(tmp_path)
    monkeypatch.setattr(final_cover, "recognize_song_title", lambda *_args, **_kwargs: pytest.fail("stopped task must not OCR"))
    with pytest.raises(InterruptedError):
        wait_for_final_cover(
            None, SimpleNamespace(song_level=26, song_title="グッド・バイ"),
            None, "Expert", lambda: True, repository=repository,
        )


def test_final_cover_resolver_can_require_title_after_early_ocr_failed():
    cover, song_id = final_cover_frame()
    selection = SimpleNamespace(
        difficulty="expert",
        level=28,
        shared_jacket=False,
        fingerprints=(song_id,),
        bestdori_song_id=50,
        title="FIRE BIRD",
        titles=("FIRE BIRD",),
    )

    class Repository:
        def resolve(self, _song_id, _difficulty, *, level, title):
            assert level == 28
            return ChartResolution(
                selection if title == "FIRE BIRD" else None,
                "confirmed" if title == "FIRE BIRD" else "title missing",
            )

    resolver = FinalCoverResolver(
        difficulty="Expert",
        observed_level=28,
        observed_title="FIRE BIRD",
        observed_title_confidence=0.0,
        repository=Repository(),
        require_observed_title=True,
    )

    assert resolver.observed_title is None
    assert resolver.observe(cover) is None
    assert resolver.observe(cover) is None
    assert resolver.last_reason == "final cover title is not confirmed"
    assert resolver.refresh_observed_title("FIRE BIRD", 0.93) is True
    resolution = resolver.observe(cover)
    assert resolution is not None
    assert resolution.confirmation.bestdori_song_id == 50


def test_final_cover_resolver_can_reconfirm_pending_identity_without_old_level():
    cover, song_id = final_cover_frame()
    selected = SimpleNamespace(
        difficulty="expert",
        level=28,
        shared_jacket=False,
        fingerprints=(song_id,),
        bestdori_song_id=50,
        title="FIRE BIRD",
        titles=("FIRE BIRD",),
    )

    class Repository:
        def resolve(self, _song_id, _difficulty, *, level, title):
            assert level is None
            return ChartResolution(
                selected if title == "FIRE BIRD" else None,
                "confirmed" if title == "FIRE BIRD" else "title missing",
            )

    resolver = FinalCoverResolver(
        difficulty="Expert",
        observed_level=None,
        observed_title=None,
        repository=Repository(),
        require_observed_title=True,
        allow_missing_level=True,
    )

    assert resolver.evidence_reason() is None
    assert resolver.refresh_observed_title("FIRE BIRD", 0.93) is True
    assert resolver.observe(cover) is None
    assert resolver.observe(cover) is not None


def test_pending_final_cover_does_not_reuse_a_trusted_preparation_title():
    cover, _song_id = final_cover_frame()

    class Job:
        def wait(self):
            return self

        def get(self):
            return cover

    class Controller:
        def post_screencap(self):
            return Job()

    class Repository:
        def resolve(self, *_args, **_kwargs):
            raise AssertionError("最终标题缺失时不得复用旧标题解析")

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            profile_play_action, "recognize_song_title", lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(
            profile_play_action, "PlayfieldDetector", lambda: (lambda _image: True),
        )
        with pytest.raises(RuntimeError, match="最终封面页标题未确认"):
            wait_for_final_cover(
                Controller(),
                SimpleNamespace(
                    song_level=27,
                    song_title="旧准备页标题",
                    song_title_confidence=0.99,
                ),
                None,
                "Expert",
                lambda: False,
                repository=Repository(),
                timeout_seconds=1,
                poll_interval_seconds=0,
                require_observed_title=True,
                ignore_preparation_level=True,
            )


def test_required_final_cover_title_does_not_degrade_at_playfield(monkeypatch):
    cover, _song_id = final_cover_frame()

    class Job:
        def wait(self):
            return self

        def get(self):
            return cover

    class Controller:
        def post_screencap(self):
            return Job()

    class Repository:
        def resolve(self, *_args, **_kwargs):
            raise AssertionError("标题缺失时不应解析谱面")

    monkeypatch.setattr(
        profile_play_action,
        "recognize_song_title",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        profile_play_action,
        "PlayfieldDetector",
        lambda: (lambda _image: True),
    )

    with pytest.raises(RuntimeError, match="最终封面页标题未确认"):
        wait_for_final_cover(
            Controller(),
            SimpleNamespace(
                song_level=28,
                song_title="FIRE BIRD",
                song_title_confidence=0.0,
            ),
            None,
            "Expert",
            lambda: False,
            repository=Repository(),
            timeout_seconds=1,
            poll_interval_seconds=0,
            require_observed_title=True,
        )


def test_cooperative_loading_and_false_playfield_keep_scanning_until_cover(monkeypatch):
    loading, song_id = loading_frame()
    blank = np.full_like(loading, 80)
    cover, _ = final_cover_frame()
    frames = iter([loading, loading, loading, blank, blank, cover])
    observations = []

    class Controller:
        def post_screencap(self):
            image = next(frames)
            return SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: image))

    monkeypatch.setattr(profile_play_action, "PlayfieldDetector", lambda: lambda _: True)
    outcome = wait_for_final_cover(
        Controller(), SimpleNamespace(mode="cooperative", song_level=28,
                                      song_title="SAVIOR OF SONG"),
        selection(song_id), "Expert", lambda: False,
        timeout_seconds=2, poll_interval_seconds=0,
        observer=lambda _image, _time, detail: observations.append(detail),
    )
    assert outcome.status == "confirmed"
    assert len(observations) == 6


def test_cooperative_missing_final_title_waits_to_timeout_without_fallback(monkeypatch):
    cover, _ = final_cover_frame()
    clock = [0.0]
    observed = []

    class Controller:
        def post_screencap(self):
            clock[0] += 0.1
            return SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: cover))

    class Repository:
        regional_level_drift_enabled = True

        def resolve(self, *_a, **_k):
            raise AssertionError("缺少实读标题不能解析谱面")

    monkeypatch.setattr(profile_play_action.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(profile_play_action, "PlayfieldDetector", lambda: lambda _: True)
    monkeypatch.setattr(final_cover, "recognize_song_title", lambda *_a, **_k: None)
    with pytest.raises(RuntimeError, match="最终封面确认超时"):
        wait_for_final_cover(
            Controller(), SimpleNamespace(mode="cooperative", song_level=28,
                                          song_title="SAVIOR OF SONG", song_title_confidence=.99),
            None, "Expert", lambda: False, repository=Repository(),
            timeout_seconds=1, poll_interval_seconds=0,
            observer=lambda _i, _t, detail: observed.append(detail),
            fallback_selection_available=True,
        )
    assert len(observed) >= 9
    assert observed[-1]["reason"] == "final cover title is not confirmed"


def test_cooperative_preconfirmed_loading_frame_cannot_bypass_guard(monkeypatch):
    loading, song_id = loading_frame()
    cover, _ = final_cover_frame()
    selected = selection(song_id)
    resolution = FinalCoverResolution(
        FinalCoverConfirmation(song_id, "song-jacket-phash-v2", selected.bestdori_song_id),
        selected,
    )
    observed = []
    class Controller:
        def post_screencap(self):
            return SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: cover))

    outcome = wait_for_final_cover(
        Controller(), SimpleNamespace(mode="cooperative", song_level=28,
                                      song_title="SAVIOR OF SONG"),
        selected, "Expert", lambda: False, initial_image=loading,
        initial_resolution=resolution, poll_interval_seconds=0,
        observer=lambda _i, _t, detail: observed.append(detail),
    )
    assert outcome.status == "confirmed"
    assert observed[0]["member_loading"] is True
    assert observed[0]["playfield_streak"] == 0
    assert len(observed) == 2


def test_loading_guard_runs_before_nontrial_final_title_ocr(monkeypatch):
    loading, _ = loading_frame()
    readings = []
    resolver = FinalCoverResolver(
        difficulty="Expert", observed_level=28, observed_title=None,
        repository=SimpleNamespace(), reject_member_loading=True,
    )
    assert resolver.observe(loading, refresh_title=True,
                            title_reader=lambda *_a, **_k: readings.append(True)) is None
    assert not readings
    assert resolver.last_member_loading_detected is True
    assert resolver.last_member_loading_score >= final_cover.MEMBER_LOADING_ICON_THRESHOLD


def test_first_cover_title_survives_second_frame_without_title(monkeypatch):
    cover, song_id = final_cover_frame()
    selected = selection(song_id)
    second_cover = cover.copy()

    class Repository:
        regional_level_drift_enabled = True
        def resolve(self, *_args, **_kwargs):
            return ChartResolution(selected, "confirmed")

    monkeypatch.setattr(final_cover, "recognize_song_title", lambda image, **_k:
                        TitleReading("SAVIOR OF SONG", .95) if image is cover else None)
    resolver = FinalCoverResolver(difficulty="Expert", observed_level=28,
                                 observed_title=None, repository=Repository(),
                                 require_observed_title=True)
    assert resolver.observe(cover) is None
    outcome = resolver.observe(second_cover)
    assert outcome is not None
    assert outcome.observed_title == "SAVIOR OF SONG"
    assert outcome.final_title_confirmed is True


def test_score_text_cannot_be_promoted_to_conflicting_song_title(monkeypatch):
    cover, song_id = final_cover_frame()
    selected = selection(song_id)

    class Repository:
        regional_level_drift_enabled = True
        def resolve(self, fingerprint, _difficulty, **_kwargs):
            return ChartResolution(selected if fingerprint != "unknown" else None, "confirmed")

    monkeypatch.setattr(final_cover, "recognize_song_title", lambda *_a, **_k: TitleReading("标得分'5''0''8'", .98))
    resolver = FinalCoverResolver(difficulty="Expert", observed_level=28,
                                 observed_title=None, repository=Repository(),
                                 require_observed_title=True)
    assert resolver.observe(cover) is None
    assert resolver.observe(cover) is None
    assert resolver._final_title_confirmed is False
    assert resolver.observed_title is None


@pytest.mark.parametrize("interruption", ["loading", "unknown", "different-cover", "goal-unknown", "goal-different-cover"])
def test_first_cover_title_cache_is_reset_by_page_or_identity_change(monkeypatch, interruption):
    cover, song_id = final_cover_frame()
    selected = selection(song_id)
    after = cover.copy()
    if interruption == "loading":
        interrupted, _ = loading_frame()
    elif interruption in {"unknown", "goal-unknown"}:
        interrupted = np.zeros_like(cover)
    else:
        interrupted, _ = final_cover_frame(seed=83)
    if interruption.startswith("goal-"):
        cv2.circle(interrupted, (382, 515), 26, (90, 200, 240), -1)
        cv2.putText(interrupted, "5082000", (738, 530), cv2.FONT_HERSHEY_SIMPLEX, .9,
                    (180, 140, 250), 2)

    class Repository:
        regional_level_drift_enabled = True
        def resolve(self, *_a, **_k):
            return ChartResolution(selected, "confirmed")

    monkeypatch.setattr(final_cover, "recognize_song_title", lambda image, **_k:
                        TitleReading("SAVIOR OF SONG", .95) if image is cover else None)
    resolver = FinalCoverResolver(difficulty="Expert", observed_level=28,
                                 observed_title=None, repository=Repository(),
                                 require_observed_title=True, reject_member_loading=True)
    assert resolver.observe(cover) is None
    assert resolver._final_title_confirmed is True
    assert resolver.observe(interrupted) is None
    assert resolver._final_title_confirmed is False
    assert resolver.observed_title is None
    assert resolver.observe(after) is None
    assert resolver.observe(after) is None


@pytest.mark.parametrize("preparation_title", [None, "SAVIOR OF SONG"])
def test_score_layout_only_never_reads_or_confirms_a_song_title(monkeypatch, tmp_path, preparation_title):
    monkeypatch.setattr(chart_repository, "_regional_level_drift_trial_configured", None)
    monkeypatch.delenv(chart_repository.REGIONAL_LEVEL_DRIFT_TRIAL_ENV, raising=False)
    cover, _ = final_cover_frame()
    cv2.circle(cover, (382, 515), 26, (90, 200, 240), -1)
    cv2.putText(cover, "5082000", (738, 530), cv2.FONT_HERSHEY_SIMPLEX, .9,
                (180, 140, 250), 2)
    class Repository(LocalChartRepository):
        def resolve(self, *_a, **_k):
            raise AssertionError("得分页面不应解析曲名")

    monkeypatch.setattr(final_cover, "recognize_song_title", lambda *_a, **_k: (
        _ for _ in ()
    ).throw(AssertionError("目标得分页不应被 OCR 成曲名")))
    resolver = FinalCoverResolver(difficulty="Expert", observed_level=28,
                                 observed_title=preparation_title, observed_title_confidence=.99,
                                 repository=Repository(tmp_path),
                                 require_observed_title=True)
    for _ in range(4):
        assert resolver.observe(cover) is None
    assert resolver.last_reason == "goal score page is not a song title"
    assert resolver.last_title_diagnostic["status"] == "excluded-score-layout"


def test_same_cover_score_layout_preserves_only_its_actual_title_cache(monkeypatch):
    cover, song_id = final_cover_frame()
    selected = selection(song_id)
    goal = cover.copy()
    cv2.circle(goal, (382, 515), 26, (90, 200, 240), -1)
    cv2.putText(goal, "5082000", (738, 530), cv2.FONT_HERSHEY_SIMPLEX, .9,
                (180, 140, 250), 2)
    class Repository:
        regional_level_drift_enabled = True
        def resolve(self, *_a, **_k):
            return ChartResolution(selected, "confirmed")

    def reader(image, **_kwargs):
        if image is goal:
            raise AssertionError("得分页必须复用本局实读标题，不能 OCR 得分")
        return TitleReading("SAVIOR OF SONG", .95) if image is cover else None
    monkeypatch.setattr(final_cover, "recognize_song_title", reader)
    resolver = FinalCoverResolver(difficulty="Expert", observed_level=28,
                                 observed_title=None, repository=Repository(),
                                 require_observed_title=True)
    assert resolver.observe(cover) is None
    outcome = resolver.observe(goal)
    assert outcome is not None
    assert outcome.final_title_confirmed is True
    assert outcome.observed_title == "SAVIOR OF SONG"
    assert resolver._final_title_confirmed is True
    assert resolver._final_title_observed_frame == 1


def test_wrong_cached_title_still_conflicts_when_second_cover_is_goal(monkeypatch):
    cover, song_id = final_cover_frame()
    selected = selection(song_id)
    goal = cover.copy()
    cv2.circle(goal, (382, 515), 26, (90, 200, 240), -1)
    cv2.putText(goal, "5082000", (738, 530), cv2.FONT_HERSHEY_SIMPLEX, .9,
                (180, 140, 250), 2)
    class Repository:
        regional_level_drift_enabled = True
        def resolve(self, *_a, **_k):
            return ChartResolution(selected, "confirmed")

    def reader(image, **_kwargs):
        if image is goal:
            raise AssertionError("目标得分页不能重读标题")
        return TitleReading("FIRE BIRD", .99)
    monkeypatch.setattr(final_cover, "recognize_song_title", reader)
    resolver = FinalCoverResolver(difficulty="Expert", observed_level=28,
                                 observed_title=None, repository=Repository(),
                                 require_observed_title=True)
    assert resolver.observe(cover) is None
    with pytest.raises(RuntimeError, match="歌曲身份冲突"):
        resolver.observe(goal)


def test_cooperative_final_cover_wait_preserves_diagnostics_stream(tmp_path):
    from agent.realtime.startup_diagnostics import CooperativeStartupRecorder
    cover, song_id = final_cover_frame()
    recorder = CooperativeStartupRecorder(tmp_path, "diag-run", close_timeout=1)
    class Controller:
        def post_screencap(self):
            return SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: cover))

    try:
        outcome = wait_for_final_cover(
            Controller(), SimpleNamespace(mode="cooperative", run_id="diag-run", song_level=28,
                                          song_title="SAVIOR OF SONG"),
            selection(song_id), "Expert", lambda: False, poll_interval_seconds=0,
        )
        assert outcome.status == "confirmed"
    finally:
        recorder.close("confirmed")
    rows = (recorder.output_dir / "observations.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(rows[0])["phase"] == "final-cover-wait"
    assert json.loads(rows[0])["member_loading"] is False


def test_cooperative_stop_after_loading_does_not_capture_again(monkeypatch):
    loading, song_id = loading_frame()
    captures = []
    stopping = [False]
    class Controller:
        def post_screencap(self):
            captures.append(True)
            if len(captures) > 1:
                raise AssertionError("停止后不能再次截图")
            return SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: loading))

    def observed(_image, _time, detail):
        assert detail["member_loading"] is True
        stopping[0] = True

    monkeypatch.setattr(profile_play_action, "PlayfieldDetector", lambda: lambda _: True)
    with pytest.raises(InterruptedError):
        wait_for_final_cover(
            Controller(), SimpleNamespace(mode="cooperative", song_level=28,
                                          song_title="SAVIOR OF SONG"),
            selection(song_id), "Expert", lambda: stopping[0],
            poll_interval_seconds=0, observer=observed,
        )
    assert len(captures) == 1


def test_cooperative_diagnostic_playfield_error_keeps_cover_scan(monkeypatch, capsys):
    cover, song_id = final_cover_frame()
    images = iter([np.full_like(cover, 80), cover])
    class Controller:
        def post_screencap(self):
            image = next(images)
            return SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: image))

    monkeypatch.setattr(profile_play_action, "PlayfieldDetector", lambda: lambda _: (
        _ for _ in ()
    ).throw(ValueError("diagnostic failure")))
    outcome = wait_for_final_cover(
        Controller(), SimpleNamespace(mode="cooperative", song_level=28,
                                      song_title="SAVIOR OF SONG"),
        selection(song_id), "Expert", lambda: False, poll_interval_seconds=0,
    )
    assert outcome.status == "confirmed"
    assert "diagnostics_warning=ValueError" in capsys.readouterr().out


def test_wait_for_final_cover_refreshes_title_from_final_page(
    monkeypatch,
    tmp_path,
):
    # 仓库里两首歌共享同一封面指纹和同一等级，只有标题能消歧；准备页
    # 标题是乱码，最终封面页 OCR 出正确标题后必须重新解析并确认。
    cover, song_id = final_cover_frame()
    chart = [
        {"type": "BPM", "beat": 0, "bpm": 120},
        {"type": "Single", "beat": 1, "lane": 2},
    ]
    digest = hashlib.sha256(
        json.dumps(
            chart,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    songs = []
    for bestdori_id, title in ((101, "Alpha Song"), (102, "Beta Song")):
        (tmp_path / "bestdori" / str(bestdori_id)).mkdir(parents=True)
        (tmp_path / "bestdori" / str(bestdori_id) / "expert.json").write_text(
            json.dumps({
                "schema_version": 1,
                "source": {"provider": "bestdori", "chart_sha256": digest},
                "song": {"bestdori_id": bestdori_id, "titles": [title]},
                "difficulty": {"name": "expert", "level": 20},
                "chart": chart,
            }),
            encoding="utf-8",
        )
        songs.append({
            "bestdori_song_id": bestdori_id,
            "display_title": title,
            "titles": [title],
            "fingerprints": [song_id],
            "difficulties": {
                "expert": {
                    "path": f"bestdori/{bestdori_id}/expert.json",
                    "level": 20,
                    "chart_sha256": digest,
                }
            },
        })
    (tmp_path / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "songs": songs,
    }), encoding="utf-8")
    repository = LocalChartRepository(tmp_path)

    class Job:
        def wait(self):
            return self

        def get(self):
            return cover

    class Controller:
        def post_screencap(self):
            return Job()

    def fake_recognize(image, roi=None):
        calls.append(roi)
        return TitleReading("Beta Song", 0.95)

    calls = []
    monkeypatch.setattr(
        profile_play_action,
        "recognize_song_title",
        fake_recognize,
    )
    monkeypatch.setattr(final_cover, "recognize_song_title", fake_recognize)

    outcome = wait_for_final_cover(
        Controller(),
        SimpleNamespace(
            song_level=20,
            song_title="zzz",
            song_title_confidence=0.1,
        ),
        None,
        "Expert",
        lambda: False,
        repository=repository,
        timeout_seconds=1,
        poll_interval_seconds=0,
    )

    assert outcome.status == "confirmed"
    assert outcome.resolution.confirmation.bestdori_song_id == 102
    assert outcome.resolution.observed_title == "Beta Song"
    assert outcome.resolution.observed_title_confidence == pytest.approx(0.95)
    assert len(calls) == 1
    assert calls[0] == FINAL_COVER_TITLE_ROI


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
