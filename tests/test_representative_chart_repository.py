from __future__ import annotations

import json
from pathlib import Path

from agent.realtime.chart_repository import LocalChartRepository
from agent.realtime.song_identity import same_song


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHART_ROOT = PROJECT_ROOT / "resource" / "charts"
EXPECTED_SONG_IDS = {
    85, 125, 170, 306, 325, 489,
    499, 522, 532, 540, 595, 697,
}


def test_representative_repository_contains_12_songs_and_53_charts():
    manifest = json.loads(
        (CHART_ROOT / "manifest.json").read_text(encoding="utf-8")
    )

    assert {song["bestdori_song_id"] for song in manifest["songs"]} == EXPECTED_SONG_IDS
    assert sum(len(song["difficulties"]) for song in manifest["songs"]) == 53
    assert manifest["source"]["runtime_network_access"] is False


def test_every_manifest_chart_resolves_and_cross_song_hashes_are_unambiguous():
    manifest = json.loads(
        (CHART_ROOT / "manifest.json").read_text(encoding="utf-8")
    )
    repository = LocalChartRepository(CHART_ROOT)
    all_fingerprints = [
        (song["bestdori_song_id"], fingerprint)
        for song in manifest["songs"]
        for fingerprint in song["fingerprints"]
    ]

    for index, (left_song, left) in enumerate(all_fingerprints):
        for right_song, right in all_fingerprints[index + 1:]:
            if left_song != right_song:
                assert not same_song(left, right)

    for song in manifest["songs"]:
        assert song["fingerprints"]
        for fingerprint in song["fingerprints"]:
            for difficulty in song["difficulties"]:
                resolved = repository.resolve(fingerprint, difficulty.upper())
                assert resolved.reason == "confirmed local chart"
                assert resolved.selection is not None
                assert resolved.selection.bestdori_song_id == song["bestdori_song_id"]
                assert resolved.selection.difficulty == difficulty
                assert resolved.selection.timeline.judgements
