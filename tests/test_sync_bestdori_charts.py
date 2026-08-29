from __future__ import annotations

import json

import cv2
import numpy as np
import pytest

from agent.realtime.song_identity import SONG_ID_METHOD
from scripts.sync_bestdori_charts import (
    CHART_URL,
    SONGS_INDEX_URL,
    SONG_URL,
    sync_charts,
)


def _png_bytes() -> bytes:
    image = np.zeros((32, 32, 3), dtype=np.uint8)
    image[:, :16] = (255, 30, 10)
    image[8:24, 12:28] = (10, 230, 160)
    succeeded, encoded = cv2.imencode(".png", image)
    assert succeeded
    return encoded.tobytes()


def test_sync_downloads_every_available_difficulty_and_jacket_hash(tmp_path):
    song_id = 99
    chart = [
        {"type": "BPM", "beat": 0, "bpm": 120},
        {"type": "Single", "beat": 2, "lane": 3},
    ]
    chart_bytes = json.dumps(chart).encode("utf-8")
    metadata = {
        "musicTitle": ["Example"],
        "jacketImage": ["099_example"],
        "difficulty": {
            "0": {"playLevel": 5},
            "4": {"playLevel": 26},
        },
        "notes": {"0": 1, "4": 1},
    }

    def fetch_json(url):
        if url == SONGS_INDEX_URL:
            return {str(song_id): {"musicTitle": ["Example"]}}, b"{}"
        if url == SONG_URL.format(song_id=song_id):
            return metadata, json.dumps(metadata).encode("utf-8")
        if url in {
            CHART_URL.format(song_id=song_id, difficulty="easy"),
            CHART_URL.format(song_id=song_id, difficulty="special"),
        }:
            return chart, chart_bytes
        raise AssertionError(f"unexpected URL: {url}")

    jacket_urls = []

    def fetch_bytes(url):
        jacket_urls.append(url)
        return _png_bytes()

    manifest = sync_charts(
        [{
            "bestdori_song_id": song_id,
            "display_title": "Example",
            "category": "test",
            "fingerprints": [],
        }],
        tmp_path,
        fetch_json,
        fetch_bytes,
    )

    song = manifest["songs"][0]
    assert set(song["difficulties"]) == {"easy", "special"}
    assert len(song["fingerprints"]) == 1
    assert song["fingerprints"][0].startswith(f"{SONG_ID_METHOD}-")
    assert song["jackets"][0]["fingerprint"] == song["fingerprints"][0]
    assert "musicjacket100" in jacket_urls[0]
    assert json.loads((tmp_path / "bestdori/99/easy.json").read_text())["chart"] == chart


def test_sync_refuses_song_without_any_confirmed_fingerprint(tmp_path):
    metadata = {
        "musicTitle": ["Example"],
        "difficulty": {"0": {"playLevel": 5}},
        "notes": {"0": 1},
    }

    def fetch_json(url):
        if url == SONGS_INDEX_URL:
            return {"99": {}}, b"{}"
        if url == SONG_URL.format(song_id=99):
            return metadata, b"{}"
        raise AssertionError("chart fetch must not start without identity")

    with pytest.raises(ValueError, match="no fingerprints"):
        sync_charts(
            [{
                "bestdori_song_id": 99,
                "display_title": "Example",
                "fingerprints": [],
            }],
            tmp_path,
            fetch_json,
        )
