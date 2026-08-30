from __future__ import annotations

import json

import cv2
import numpy as np

from scripts.sync_bestdori_catalog import (
    JACKET_URL,
    sync_catalog,
)
from scripts.sync_bestdori_charts import CHART_URL, SONGS_INDEX_URL


def _png_bytes() -> bytes:
    image = np.zeros((32, 32, 3), dtype=np.uint8)
    image[:, :16] = (240, 30, 20)
    image[8:24, 12:28] = (20, 220, 150)
    succeeded, encoded = cv2.imencode(".png", image)
    assert succeeded
    return encoded.tobytes()


def test_catalog_keeps_only_target_difficulties_and_saves_cn_jacket(tmp_path):
    chart = [
        {"type": "BPM", "beat": 0, "bpm": 120},
        {"type": "Single", "beat": 2, "lane": 3},
    ]
    raw_chart = json.dumps(chart).encode()
    metadata = {
        "tag": "anime",
        "musicTitle": ["JP", "EN", "TW", "简中"],
        "jacketImage": ["099_example"],
        "difficulty": {
            "0": {"playLevel": 5},
            "2": {"playLevel": 18},
            "3": {"playLevel": 25},
            "4": {"playLevel": 27},
        },
        "notes": {"0": 1, "2": 1, "3": 1, "4": 1},
    }
    requested = []

    def fetch_json(url):
        requested.append(url)
        if url == SONGS_INDEX_URL:
            return {"99": metadata}, b"{}"
        if url.startswith("https://bestdori.com/api/charts/99/"):
            return chart, raw_chart
        raise AssertionError(url)

    manifest = sync_catalog(
        tmp_path,
        fetch_json,
        lambda _url: _png_bytes(),
        workers=1,
    )

    song = manifest["songs"][0]
    assert song["display_title"] == "简中"
    assert set(song["difficulties"]) == {"hard", "expert", "special"}
    assert CHART_URL.format(song_id=99, difficulty="easy") not in requested
    assert song["jackets"][0]["server"] == "cn"
    assert song["jackets"][0]["source_url"] == JACKET_URL.format(
        server="cn", bundle_id=100, jacket_name="099_example"
    )
    assert (tmp_path / song["jackets"][0]["path"]).read_bytes() == _png_bytes()


def test_catalog_records_missing_jacket_and_chart_without_stopping(tmp_path):
    valid_chart = [{"type": "BPM", "beat": 0, "bpm": 120}]
    metadata = {
        "musicTitle": ["Song"],
        "jacketImage": ["001_song"],
        "difficulty": {
            "2": {"playLevel": 18},
            "3": {"playLevel": 25},
        },
        "notes": {"2": 0, "3": 0},
    }

    def fetch_json(url):
        if url == SONGS_INDEX_URL:
            return {"1": metadata}, b"{}"
        if url.endswith("/hard.json"):
            return valid_chart, json.dumps(valid_chart).encode()
        raise OSError("chart unavailable")

    manifest = sync_catalog(
        tmp_path,
        fetch_json,
        lambda _url: (_ for _ in ()).throw(OSError("CN jacket unavailable")),
        workers=1,
    )

    song = manifest["songs"][0]
    assert set(song["difficulties"]) == {"hard"}
    assert song["jackets"] == []
    assert {error["kind"] for error in song["errors"]} == {"chart", "jacket"}
    assert manifest["summary"]["recoverable_errors"] == 2


def test_catalog_falls_back_to_jp_when_cn_jacket_is_missing(tmp_path):
    chart = [{"type": "BPM", "beat": 0, "bpm": 120}]
    metadata = {
        "musicTitle": ["Song"],
        "jacketImage": ["001_song"],
        "difficulty": {"2": {"playLevel": 18}},
        "notes": {"2": 0},
    }

    def fetch_json(url):
        if url == SONGS_INDEX_URL:
            return {"1": metadata}, b"{}"
        return chart, json.dumps(chart).encode()

    requested = []

    def fetch_bytes(url):
        requested.append(url)
        if "/cn/" in url:
            raise OSError("missing CN jacket")
        return _png_bytes()

    manifest = sync_catalog(tmp_path, fetch_json, fetch_bytes, workers=1)

    jacket = manifest["songs"][0]["jackets"][0]
    assert jacket["server"] == "jp"
    assert "/cn/" in requested[0]
    assert "/jp/" in requested[1]
    assert manifest["summary"]["recoverable_errors"] == 0


def test_catalog_quotes_spaces_and_retries_lowercase_asset_name(tmp_path):
    chart = [{"type": "BPM", "beat": 0, "bpm": 120}]
    metadata = {
        "musicTitle": ["Song"],
        "jacketImage": ["001_Mixed Name"],
        "difficulty": {"2": {"playLevel": 18}},
        "notes": {"2": 0},
    }

    def fetch_json(url):
        if url == SONGS_INDEX_URL:
            return {"1": metadata}, b"{}"
        return chart, json.dumps(chart).encode()

    requested = []

    def fetch_bytes(url):
        requested.append(url)
        if "Mixed%20Name" in url:
            raise OSError("case mismatch")
        assert "001_mixed%20name" in url
        return _png_bytes()

    manifest = sync_catalog(tmp_path, fetch_json, fetch_bytes, workers=1)

    jacket = manifest["songs"][0]["jackets"][0]
    assert jacket["asset_name"] == "001_mixed name"
    assert requested[0].endswith("001_Mixed%20Name-jacket.png")
    assert requested[1].endswith("001_mixed%20name-jacket.png")
