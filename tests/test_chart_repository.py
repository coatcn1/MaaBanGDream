from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent.realtime.chart_repository import LocalChartRepository
from agent.realtime.song_identity import UNKNOWN_SONG_ID


def test_explicit_full_title_disambiguates_shared_fire_bird_jacket():
    repository = LocalChartRepository(Path(__file__).resolve().parents[1] / "resource/charts")
    fingerprint = "song-jacket-phash-v2-c52d4b1e6a1ab5e3"
    full = repository.resolve(fingerprint, "Expert", title="[FULL]FIRE BIRD")
    assert full.selection is not None
    assert full.selection.bestdori_song_id == 243
    assert repository.resolve(fingerprint, "Expert", level=27,
                              title="[FULL]FIRE BIRD").selection is None
    assert repository.resolve(fingerprint, "Expert", title="FIRE BIRD").selection is None
    ordinary_identity = repository.identify_by_cover_title(
        fingerprint,
        "FIRE BIRD",
        full_badge=False,
    )
    full_identity = repository.identify_by_cover_title(
        fingerprint,
        "FIRE BIRD",
        full_badge=True,
    )
    assert ordinary_identity.identity.bestdori_song_id == 187
    assert full_identity.identity.bestdori_song_id == 243
    confirmed_full = repository.resolve(
        fingerprint,
        "Expert",
        title="FIRE BIRD",
        bestdori_song_id=243,
    )
    assert confirmed_full.selection.bestdori_song_id == 243


def test_little_busters_continuous_cover_resolves_expert_chart():
    repository = LocalChartRepository(
        Path(__file__).resolve().parents[1] / "resource/charts"
    )
    identity = repository.identify_by_cover_title(
        "song-jacket-phash-v2-c7b9cb102fcfb04a",
        "Little Busters'!'",
    )

    assert identity.identity is not None
    assert identity.identity.bestdori_song_id == 46
    chart = repository.resolve(
        identity.identity.fingerprints[0],
        "Expert",
        title="Little Busters'!'",
        bestdori_song_id=identity.identity.bestdori_song_id,
    )
    assert chart.selection is not None
    assert chart.selection.bestdori_song_id == 46
    assert chart.selection.difficulty == "expert"
    assert chart.selection.level == 25


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


def test_repository_can_identify_song_without_requested_difficulty_chart(tmp_path):
    build_repository(tmp_path)

    resolution = LocalChartRepository(tmp_path).identify_by_cover_title(
        FINGERPRINT,
        "Song!",
    )

    assert resolution.identity is not None
    assert resolution.identity.bestdori_song_id == 99
    assert resolution.reason == "confirmed song by final cover and title"


def test_repository_identity_allows_loose_cover_after_unique_title_match(tmp_path):
    canonical = "song-jacket-phash-v2-c7bac9172dceb062"
    observed = "song-jacket-phash-v2-c7b9cb102fcfb04a"
    build_repository(tmp_path, fingerprints=[canonical])

    resolution = LocalChartRepository(tmp_path).identify_by_cover_title(
        observed,
        "Song!",
    )

    assert resolution.identity is not None
    assert resolution.identity.bestdori_song_id == 99


def test_repository_identity_rejects_loose_cover_without_matching_title(tmp_path):
    canonical = "song-jacket-phash-v2-c7bac9172dceb062"
    observed = "song-jacket-phash-v2-c7b9cb102fcfb04a"
    build_repository(tmp_path, fingerprints=[canonical])

    resolution = LocalChartRepository(tmp_path).identify_by_cover_title(
        observed,
        "Different Song",
    )

    assert resolution.identity is None


def test_repository_identity_requires_title_to_match_cover(tmp_path):
    build_repository(tmp_path)

    resolution = LocalChartRepository(tmp_path).identify_by_cover_title(
        FINGERPRINT,
        "Different Song",
    )

    assert resolution.identity is None
    assert resolution.reason == "song title does not match final cover"


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
