from __future__ import annotations

import json
import hashlib
from pathlib import Path

from agent.realtime.chart_repository import LocalChartRepository, _chart_sha256
from agent.realtime.song_identity import same_song


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHART_ROOT = PROJECT_ROOT / "resource" / "charts"
TARGET_DIFFICULTIES = {"hard", "expert", "special"}


def test_catalog_repository_contains_full_targeted_snapshot():
    manifest = json.loads(
        (CHART_ROOT / "manifest.json").read_text(encoding="utf-8")
    )

    assert len(manifest["songs"]) == 809
    assert sum(len(song["difficulties"]) for song in manifest["songs"]) == 1777
    assert sum(len(song["jackets"]) for song in manifest["songs"]) == 867
    assert manifest["summary"]["recoverable_errors"] == 0
    assert manifest["summary"]["fatal_errors"] == 0
    assert manifest["source"]["runtime_network_access"] is False
    assert set(manifest["source"]["difficulties"]) == TARGET_DIFFICULTIES
    assert all(song["jackets"] for song in manifest["songs"])
    assert all(
        set(song["difficulties"]).issubset(TARGET_DIFFICULTIES)
        for song in manifest["songs"]
    )


def test_every_manifest_file_exists_and_matches_its_hash():
    manifest = json.loads(
        (CHART_ROOT / "manifest.json").read_text(encoding="utf-8")
    )
    for song in manifest["songs"]:
        assert song["fingerprints"]
        for jacket in song["jackets"]:
            path = CHART_ROOT / jacket["path"]
            assert path.is_file()
            assert hashlib.sha256(path.read_bytes()).hexdigest() == jacket["raw_sha256"]
        for difficulty, entry in song["difficulties"].items():
            path = CHART_ROOT / entry["path"]
            payload = json.loads(path.read_text(encoding="utf-8"))
            assert payload["song"]["bestdori_id"] == song["bestdori_song_id"]
            assert payload["difficulty"]["name"] == difficulty
            assert _chart_sha256(payload["chart"]) == entry["chart_sha256"]
            assert entry["chart_sha256"] == payload["source"]["chart_sha256"]


def test_catalog_fingerprints_resolve_uniquely_or_fail_closed_as_ambiguous():
    manifest = json.loads(
        (CHART_ROOT / "manifest.json").read_text(encoding="utf-8")
    )
    repository = LocalChartRepository(CHART_ROOT)
    unique_example = next(
        song for song in manifest["songs"]
        if song["bestdori_song_id"] == 306
    )
    resolved = repository.resolve(unique_example["fingerprints"][0], "Hard")
    assert resolved.reason == "confirmed local chart"
    assert resolved.selection is not None
    assert resolved.selection.bestdori_song_id == 306

    duplicate_fingerprint = next(
        left
        for index, left_song in enumerate(manifest["songs"])
        for left in left_song["fingerprints"]
        for right_song in manifest["songs"][index + 1:]
        for right in right_song["fingerprints"]
        if same_song(left, right)
    )
    ambiguous = repository.resolve(duplicate_fingerprint, "Hard")
    assert ambiguous.selection is None
    assert ambiguous.reason == "song fingerprint mapping is ambiguous"


def test_catalog_resolves_fire_bird_live_fingerprint_only_with_level_guard():
    repository = LocalChartRepository(CHART_ROOT)
    # 选曲页实测到的 [FULL]FIRE BIRD 封面哈希：距目录内共享封面 12 bit，
    # 严格阈值 8 会误拒；最近的其他歌曲在 18 bit。只有读到等级时才允许
    # 更宽阈值重试，随后由等级硬约束在 243/187 之间唯一化。
    live = "song-jacket-phash-v2-d53d6b1e6b1850f2"

    strict = repository.resolve(live, "expert")
    assert strict.selection is None

    full = repository.resolve(live, "expert", level=28)
    assert "confirmed" in full.reason
    assert full.selection.bestdori_song_id == 243
    assert full.selection.shared_jacket is True
    assert full.selection.shared_jacket_level_unique is True

    regular = repository.resolve(live, "expert", level=27)
    assert "confirmed" in regular.reason
    assert regular.selection.bestdori_song_id == 187
    assert regular.selection.shared_jacket_level_unique is True

    garbage = repository.resolve(
        "song-jacket-phash-v2-0000000000000000", "expert", level=28
    )
    assert garbage.selection is None
