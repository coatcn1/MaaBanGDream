from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent.realtime.chart_repository import LocalChartRepository


FINGERPRINT = "song-jacket-phash-v2-0123456789abcdef"


def chart_hash(chart):
    canonical = json.dumps(
        chart, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_repository(root: Path, *, fingerprints=None, difficulty="hard"):
    chart = [
        {"type": "BPM", "beat": 0, "bpm": 120},
        {"type": "Single", "beat": 1, "lane": 2},
    ]
    digest = chart_hash(chart)
    path = root / "bestdori" / "99" / f"{difficulty}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema_version": 1,
        "source": {"provider": "bestdori", "chart_sha256": digest},
        "song": {"bestdori_id": 99, "titles": ["Song"]},
        "difficulty": {"name": difficulty, "level": 20},
        "chart": chart,
    }), encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "songs": [{
            "bestdori_song_id": 99,
            "display_title": "Song",
            "titles": ["Song"],
            "fingerprints": fingerprints or [FINGERPRINT],
            "difficulties": {
                difficulty: {
                    "path": f"bestdori/99/{difficulty}.json",
                    "chart_sha256": digest,
                }
            },
        }],
    }), encoding="utf-8")


def test_repository_requires_confirmed_song_and_exact_difficulty(tmp_path):
    build_repository(tmp_path)
    repository = LocalChartRepository(tmp_path)

    selected = repository.resolve(FINGERPRINT, "Hard")
    missing_song = repository.resolve(
        "song-jacket-phash-v2-fedcba9876543210", "Hard"
    )
    missing_difficulty = repository.resolve(FINGERPRINT, "Expert")

    assert selected.selection.bestdori_song_id == 99
    assert selected.selection.difficulty == "hard"
    assert selected.selection.timeline.next_judgement(2, 0).time_s == 0.5
    assert missing_song.selection is None
    assert missing_song.reason == "song fingerprint is not confirmed"
    assert missing_difficulty.selection is None
    assert "no local expert chart" in missing_difficulty.reason


def test_repository_rejects_corrupted_chart(tmp_path):
    build_repository(tmp_path)
    chart_path = tmp_path / "bestdori" / "99" / "hard.json"
    payload = json.loads(chart_path.read_text(encoding="utf-8"))
    payload["chart"][1]["lane"] = 5
    chart_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="hash mismatch"):
        LocalChartRepository(tmp_path).resolve(FINGERPRINT, "Hard")


def test_repository_fails_closed_on_ambiguous_fingerprint(tmp_path):
    build_repository(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    duplicate = dict(manifest["songs"][0])
    duplicate["bestdori_song_id"] = 100
    duplicate["difficulties"] = {}
    manifest["songs"].append(duplicate)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    resolution = LocalChartRepository(tmp_path).resolve(FINGERPRINT, "Hard")

    assert resolution.selection is None
    assert resolution.reason == "song fingerprint mapping is ambiguous"
