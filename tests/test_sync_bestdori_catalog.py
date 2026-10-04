from __future__ import annotations

import json

import cv2
import numpy as np
import pytest

from scripts.sync_bestdori_catalog import (
    JACKET_URL,
    sync_catalog,
)
from scripts.sync_bestdori_charts import CHART_URL, SONGS_INDEX_URL


def _incremental_fixture(tmp_path):
    chart = [{"type": "BPM", "beat": 0, "bpm": 120}]
    metadata = {"musicTitle": ["Old title"], "jacketImage": ["001_song"],
                "difficulty": {"3": {"playLevel": 25}}, "notes": {"3": 1}}
    index = {"1": metadata}
    calls = []

    def fetch_json(url):
        calls.append(url)
        return (index, b"{}") if url == SONGS_INDEX_URL else (chart, json.dumps(chart).encode())

    def fetch_bytes(url):
        calls.append(url)
        if "/cn/" in url:
            raise OSError("CN absent")
        return _png_bytes()

    manifest = sync_catalog(tmp_path, fetch_json, fetch_bytes, workers=1)
    calls.clear()
    return metadata, index, chart, calls, fetch_json, fetch_bytes, manifest


def test_incremental_complete_fallback_catalog_only_fetches_index(tmp_path):
    metadata, _, _, calls, fetch_json, fetch_bytes, _ = _incremental_fixture(tmp_path)
    metadata["musicTitle"] = ["Updated title"]
    metadata["difficulty"]["3"]["playLevel"] = 24
    manifest = sync_catalog(tmp_path, fetch_json, fetch_bytes, workers=1)
    assert calls == [SONGS_INDEX_URL]

    assert manifest["songs"][0]["titles"] == ["Updated title"]
    assert manifest["last_successful_check_at"] == manifest["generated_at"]


def test_incremental_new_song_and_difficulty_merge_with_old_resources(tmp_path):
    metadata, index, _, calls, fetch_json, fetch_bytes, old = _incremental_fixture(tmp_path)
    metadata["difficulty"]["4"] = {"playLevel": 27}
    metadata["notes"]["4"] = 2
    index["2"] = dict(metadata, jacketImage=["002_song"])
    manifest = sync_catalog(tmp_path, fetch_json, fetch_bytes, workers=1)
    assert len(manifest["songs"]) == 2
    assert set(manifest["songs"][0]["difficulties"]) == {"expert", "special"}
    assert CHART_URL.format(song_id=1, difficulty="expert") not in calls
    assert manifest["songs"][0]["difficulties"]["expert"]["chart_sha256"] == old["songs"][0]["difficulties"]["expert"]["chart_sha256"]


def test_notes_change_failure_keeps_old_entry_file_and_success_time(tmp_path):
    metadata, _, _, _, fetch_json, fetch_bytes, old = _incremental_fixture(tmp_path)
    old_entry = old["songs"][0]["difficulties"]["expert"]
    old_bytes = (tmp_path / old_entry["path"]).read_bytes()
    metadata["notes"]["3"] = 2

    def failed(url):
        if url != SONGS_INDEX_URL: raise OSError("offline chart")
        return fetch_json(url)

    manifest = sync_catalog(tmp_path, failed, fetch_bytes, workers=1)
    assert manifest["songs"][0]["difficulties"]["expert"] == old_entry
    assert (tmp_path / old_entry["path"]).read_bytes() == old_bytes
    assert manifest["last_successful_check_at"] == old["last_successful_check_at"]
    assert manifest["summary"]["recoverable_errors"] == 1


@pytest.mark.parametrize("index", [{}, [], {"1": None}, {"invalid": {}}])
def test_invalid_index_does_not_change_manifest_or_files(tmp_path, index):
    _incremental_fixture(tmp_path)
    previous = (tmp_path / "manifest.json").read_bytes()
    with pytest.raises(ValueError):
        sync_catalog(tmp_path, lambda url: (index, b"{}"), lambda url: b"", workers=1)
    assert (tmp_path / "manifest.json").read_bytes() == previous


def test_bad_song_metadata_keeps_old_song_and_does_not_mark_success(tmp_path):
    _, index, _, _, fetch_json, fetch_bytes, old = _incremental_fixture(tmp_path)
    index["1"] = {"musicTitle": None}
    manifest = sync_catalog(tmp_path, fetch_json, fetch_bytes, workers=1)
    assert manifest["songs"] == old["songs"]
    assert manifest["fatal_errors"]
    assert manifest["last_successful_check_at"] == old["last_successful_check_at"]


def test_changed_chart_uses_versioned_path_and_cancel_before_manifest_preserves_old(tmp_path, monkeypatch):
    from scripts import sync_bestdori_catalog as catalog
    metadata, _, chart, _, fetch_json, fetch_bytes, old = _incremental_fixture(tmp_path)
    old_entry = old["songs"][0]["difficulties"]["expert"]
    old_bytes = (tmp_path / old_entry["path"]).read_bytes()
    previous = (tmp_path / "manifest.json").read_bytes()
    metadata["notes"]["3"] = 2
    chart.append({"type": "Single", "beat": 2, "lane": 3})
    write = catalog._write_json_atomic

    def stop_before_commit(path, payload):
        if path.name == "manifest.json": raise InterruptedError("cancel before manifest commit")
        write(path, payload)

    monkeypatch.setattr(catalog, "_write_json_atomic", stop_before_commit)
    with pytest.raises(InterruptedError):
        sync_catalog(tmp_path, fetch_json, fetch_bytes, workers=1)
    assert (tmp_path / "manifest.json").read_bytes() == previous
    assert (tmp_path / old_entry["path"]).read_bytes() == old_bytes
    monkeypatch.setattr(catalog, "_write_json_atomic", write)
    updated = sync_catalog(tmp_path, fetch_json, fetch_bytes, workers=1)
    assert updated["songs"][0]["difficulties"]["expert"]["path"] != old_entry["path"]
    calls = []
    sync_catalog(tmp_path, lambda url: (calls.append(url) or fetch_json(url)), fetch_bytes, workers=1)
    assert calls == [SONGS_INDEX_URL]

    # 第二次仅元数据变化时，已有版本路径也必须保持原字节。
    entry = updated["songs"][0]["difficulties"]["expert"]
    version_bytes = (tmp_path / entry["path"]).read_bytes()
    previous = (tmp_path / "manifest.json").read_bytes()
    metadata["notes"]["3"] = 3
    monkeypatch.setattr(catalog, "_write_json_atomic", stop_before_commit)
    with pytest.raises(InterruptedError):
        sync_catalog(tmp_path, fetch_json, fetch_bytes, workers=1)
    assert (tmp_path / "manifest.json").read_bytes() == previous
    assert (tmp_path / entry["path"]).read_bytes() == version_bytes


def test_self_consistent_local_edit_still_requires_manifest_hash(tmp_path):
    from scripts.sync_bestdori_charts import _chart_sha256
    _, _, _, calls, fetch_json, fetch_bytes, old = _incremental_fixture(tmp_path)
    path = tmp_path / old["songs"][0]["difficulties"]["expert"]["path"]
    payload = json.loads(path.read_text())
    payload["chart"].append({"type": "Single", "beat": 5, "lane": 2})
    payload["source"]["chart_sha256"] = _chart_sha256(payload["chart"])
    path.write_text(json.dumps(payload))
    updated = sync_catalog(tmp_path, fetch_json, fetch_bytes, workers=1)
    assert CHART_URL.format(song_id=1, difficulty="expert") in calls
    assert updated["songs"][0]["difficulties"]["expert"]["chart_sha256"] == old["songs"][0]["difficulties"]["expert"]["chart_sha256"]


def test_linked_song_directory_cannot_write_outside_catalog(tmp_path):
    import os
    from scripts.sync_bestdori_catalog import _chart_relative, _safe_resource_path
    output = tmp_path / "catalog"
    outside = tmp_path / "outside"
    (output / "bestdori").mkdir(parents=True)
    outside.mkdir()
    try: os.symlink(outside, output / "bestdori" / "1", target_is_directory=True)
    except OSError:
        if os.name != "nt": pytest.skip("symbolic link permission unavailable")
        import subprocess
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(output / "bestdori" / "1"), str(outside)], capture_output=True)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
    with pytest.raises(ValueError, match="outside"):
        _chart_relative(output, 1, "expert", None)
    with pytest.raises(ValueError, match="outside"):
        _safe_resource_path(output, __import__("pathlib").Path("bestdori/1/jacket-cn-1.png"))


def test_damaged_jacket_downloads_again_instead_of_reusing_bad_bytes(tmp_path):
    _, _, _, calls, fetch_json, fetch_bytes, old = _incremental_fixture(tmp_path)
    jacket = old["songs"][0]["jackets"][0]
    (tmp_path / jacket["path"]).write_bytes(b"invalid png")
    updated = sync_catalog(tmp_path, fetch_json, fetch_bytes, workers=1)
    assert any("musicjacket" in url for url in calls)
    assert updated["songs"][0]["jackets"][0]["raw_sha256"] == jacket["raw_sha256"]
    assert (tmp_path / jacket["path"]).read_bytes() == _png_bytes()


def test_new_jacket_name_same_number_downloads_and_preserves_old_before_commit(tmp_path, monkeypatch):
    from scripts import sync_bestdori_catalog as catalog
    metadata, _, _, calls, fetch_json, _, old = _incremental_fixture(tmp_path)
    jacket = old["songs"][0]["jackets"][0]
    old_bytes = (tmp_path / jacket["path"]).read_bytes()
    previous = (tmp_path / "manifest.json").read_bytes()
    metadata["jacketImage"] = ["001_new"]
    image = np.full((32, 32, 3), 120, dtype=np.uint8)
    image[:20, :10] = (255, 0, 0)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    fresh = encoded.tobytes()

    def download(url):
        calls.append(url)
        if "/cn/" in url: raise OSError("CN absent")
        assert "001_new" in url
        return fresh

    write = catalog._write_json_atomic
    def stop(path, payload):
        if path.name == "manifest.json": raise InterruptedError("before commit")
        write(path, payload)
    monkeypatch.setattr(catalog, "_write_json_atomic", stop)
    with pytest.raises(InterruptedError):
        sync_catalog(tmp_path, fetch_json, download, workers=1)
    assert (tmp_path / "manifest.json").read_bytes() == previous
    assert (tmp_path / jacket["path"]).read_bytes() == old_bytes
    monkeypatch.setattr(catalog, "_write_json_atomic", write)
    updated = sync_catalog(tmp_path, fetch_json, download, workers=1)
    new = next(item for item in updated["songs"][0]["jackets"] if item["name"] == "001_new")
    assert new["path"] != jacket["path"]
    assert (tmp_path / new["path"]).read_bytes() == fresh
    assert (tmp_path / jacket["path"]).read_bytes() == old_bytes


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


def test_catalog_preserves_cn_level_and_records_new_global_source_level(
    tmp_path,
):
    chart = [{"type": "BPM", "beat": 0, "bpm": 120}]
    raw_chart = json.dumps(chart).encode()

    def metadata(level):
        return {
            "musicTitle": ["JP", "EN", "TW", "CN"],
            "jacketImage": ["581_sokyu_trail"],
            "difficulty": {"3": {"playLevel": level}},
            "notes": {"3": 1},
        }

    def first_fetch(url):
        if url == SONGS_INDEX_URL:
            return {"581": metadata(27)}, b"{}"
        return chart, raw_chart

    sync_catalog(tmp_path, first_fetch, lambda _url: _png_bytes(), workers=1)
    manifest_path = tmp_path / "manifest.json"
    existing = json.loads(manifest_path.read_text(encoding="utf-8"))
    existing["songs"][0]["difficulties"]["expert"].update({
        "source_level": 27,
        "regional_levels": {"cn": 27},
    })
    manifest_path.write_text(json.dumps(existing), encoding="utf-8")

    def current_global_fetch(url):
        if url == SONGS_INDEX_URL:
            return {"581": metadata(26)}, b"{}"
        raise AssertionError("existing chart must be reused")

    manifest = sync_catalog(
        tmp_path, current_global_fetch, lambda _url: _png_bytes(), workers=1,
    )
    entry = manifest["songs"][0]["difficulties"]["expert"]

    assert entry["level"] == 27
    assert entry["source_level"] == 26
    assert entry["regional_levels"] == {"cn": 27}


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
