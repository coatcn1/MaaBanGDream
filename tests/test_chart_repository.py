from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent.realtime.chart_repository import LocalChartRepository
from agent.realtime.song_identity import UNKNOWN_SONG_ID


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
                    "level": 20,
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


def test_repository_uses_selected_song_level_to_disambiguate_shared_jacket(
    tmp_path,
):
    build_repository(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    duplicate = dict(manifest["songs"][0])
    duplicate["bestdori_song_id"] = 100
    duplicate["difficulties"] = {
        "hard": {
            "path": "bestdori/99/hard.json",
            "level": 21,
            "chart_sha256": manifest["songs"][0]["difficulties"]["hard"][
                "chart_sha256"
            ],
        }
    }
    manifest["songs"].append(duplicate)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    resolution = LocalChartRepository(tmp_path).resolve(
        FINGERPRINT,
        "Hard",
        level=20,
    )

    assert resolution.selection is not None
    assert resolution.selection.bestdori_song_id == 99
    assert resolution.reason == "confirmed local chart by song level"


def test_repository_uses_ocr_title_to_disambiguate_shared_jacket(tmp_path):
    build_repository(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    duplicate = dict(manifest["songs"][0])
    duplicate["bestdori_song_id"] = 100
    duplicate["titles"] = ["Another Song"]
    duplicate["display_title"] = "Another Song"
    duplicate["difficulties"] = {}
    manifest["songs"].append(duplicate)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    resolution = LocalChartRepository(tmp_path).resolve(
        FINGERPRINT,
        "Hard",
        title="Song",
    )

    assert resolution.selection is not None
    assert resolution.selection.bestdori_song_id == 99
    assert resolution.reason == "confirmed local chart by song title"


def test_repository_can_resolve_by_title_without_single_live_jacket(tmp_path):
    build_repository(tmp_path)

    resolution = LocalChartRepository(tmp_path).resolve(
        UNKNOWN_SONG_ID,
        "Hard",
        title="Song!",
    )

    assert resolution.selection is not None
    assert resolution.selection.bestdori_song_id == 99
    assert resolution.reason == "confirmed local chart by song title"


def test_repository_uses_level_before_title_when_full_marker_is_not_ocrd(
    tmp_path,
):
    """The FULL chart must not collapse onto the shorter same-title song.

    The live title crop can omit the leading ``[FULL]`` marker.  ON YOUR MARK
    then looks closer to the ordinary level-26 title even though the selected
    Expert button reports level 27.  Difficulty level is therefore a hard
    identity constraint, not a tie-breaker used only after title matching.
    """
    build_repository(tmp_path, difficulty="expert")
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ordinary = manifest["songs"][0]
    ordinary["display_title"] = "ON YOUR MARK"
    ordinary["titles"] = ["ON YOUR MARK"]
    ordinary["difficulties"]["expert"]["level"] = 26

    ordinary_chart_path = tmp_path / "bestdori" / "99" / "expert.json"
    full_payload = json.loads(ordinary_chart_path.read_text(encoding="utf-8"))
    full_payload["song"]["bestdori_id"] = 100
    full_payload["song"]["titles"] = ["[FULL] ON YOUR MARK"]
    full_payload["difficulty"]["level"] = 27
    full_chart_path = tmp_path / "bestdori" / "100" / "expert.json"
    full_chart_path.parent.mkdir(parents=True)
    full_chart_path.write_text(json.dumps(full_payload), encoding="utf-8")

    full = json.loads(json.dumps(ordinary))
    full["bestdori_song_id"] = 100
    full["display_title"] = "[FULL] ON YOUR MARK"
    full["titles"] = ["[FULL] ON YOUR MARK"]
    full["fingerprints"] = []
    full["difficulties"]["expert"]["path"] = "bestdori/100/expert.json"
    full["difficulties"]["expert"]["level"] = 27
    manifest["songs"].append(full)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    resolution = LocalChartRepository(tmp_path).resolve(
        UNKNOWN_SONG_ID,
        "Expert",
        level=27,
        title="回ONYOUR★",
    )

    assert resolution.selection is not None
    assert resolution.selection.bestdori_song_id == 100
    assert resolution.selection.title == "[FULL] ON YOUR MARK"

    without_level = LocalChartRepository(tmp_path).resolve(
        UNKNOWN_SONG_ID,
        "Expert",
        title="回ONYOUR★",
    )
    assert without_level.selection is None
    assert "ambiguous" in without_level.reason
