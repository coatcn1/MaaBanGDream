"""Synchronize an explicit local song set from Bestdori.

This is a deployment-time tool.  Realtime code only reads the generated local
manifest and chart files and never imports this module or performs networking.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np


if str(PROJECT_ROOT := Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.realtime.song_identity import fingerprint_jacket


DEFAULT_SONG_LIST = PROJECT_ROOT / "resource" / "charts" / "representative-songs.json"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "resource" / "charts"
SONGS_INDEX_URL = "https://bestdori.com/api/songs/all.7.json"
SONG_URL = "https://bestdori.com/api/songs/{song_id}.json"
CHART_URL = "https://bestdori.com/api/charts/{song_id}/{difficulty}.json"
JACKET_URL = (
    "https://bestdori.com/assets/jp/musicjacket/musicjacket{bundle_id}_rip/"
    "assets-star-forassetbundle-startapp-musicjacket-"
    "musicjacket{bundle_id}-{jacket_name}-jacket.png"
)
DIFFICULTY_NAMES = {
    "0": "easy",
    "1": "normal",
    "2": "hard",
    "3": "expert",
    "4": "special",
}
FetchJson = Callable[[str], tuple[Any, bytes]]
FetchBytes = Callable[[str], bytes]


def sync_charts(
    song_specs: list[dict[str, Any]],
    output_root: Path,
    fetch_json: FetchJson,
    fetch_bytes: FetchBytes | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    report = progress or (lambda _message: None)
    output_root = output_root.resolve()
    report("fetching Bestdori song index")
    index, _ = fetch_json(SONGS_INDEX_URL)
    if not isinstance(index, dict):
        raise ValueError("Bestdori song index must be a JSON object")
    manifest_songs: list[dict[str, Any]] = []

    for spec in song_specs:
        song_id = _positive_int(spec.get("bestdori_song_id"), "bestdori_song_id")
        display_title = str(spec.get("display_title", "")).strip()
        if not display_title:
            raise ValueError(f"song {song_id} has no display_title")
        indexed = index.get(str(song_id))
        if not isinstance(indexed, dict):
            raise ValueError(f"song {song_id} is missing from Bestdori index")
        song, _ = fetch_json(SONG_URL.format(song_id=song_id))
        if not isinstance(song, dict):
            raise ValueError(f"song {song_id} metadata must be a JSON object")
        titles = [str(value) for value in song.get("musicTitle", []) if value]
        if not titles:
            raise ValueError(f"song {song_id} metadata has no titles")
        difficulties = song.get("difficulty")
        if not isinstance(difficulties, dict):
            raise ValueError(f"song {song_id} metadata has no difficulty map")

        fingerprints = {
            str(value) for value in spec.get("fingerprints", []) if value
        }
        jackets: list[dict[str, str]] = []
        if fetch_bytes is not None:
            jacket_names = song.get("jacketImage")
            if not isinstance(jacket_names, list) or not jacket_names:
                raise ValueError(f"song {song_id} metadata has no jacket images")
            bundle_id = ((song_id + 9) // 10) * 10
            for raw_name in jacket_names:
                jacket_name = str(raw_name).strip()
                if not jacket_name:
                    continue
                jacket_url = JACKET_URL.format(
                    bundle_id=bundle_id,
                    jacket_name=jacket_name,
                )
                report(f"fetching song={song_id} jacket={jacket_name}")
                jacket_bytes = fetch_bytes(jacket_url)
                image = cv2.imdecode(
                    np.frombuffer(jacket_bytes, dtype=np.uint8),
                    cv2.IMREAD_COLOR,
                )
                identity = fingerprint_jacket(image)
                if identity.method == "unknown":
                    raise ValueError(f"song {song_id} jacket image is invalid")
                fingerprints.add(identity.song_id)
                jackets.append({
                    "name": jacket_name,
                    "source_url": jacket_url,
                    "raw_sha256": hashlib.sha256(jacket_bytes).hexdigest(),
                    "fingerprint": identity.song_id,
                })
        if not fingerprints:
            raise ValueError(
                f"song {song_id} has no fingerprints; enable jacket download "
                "or provide a verified capture"
            )

        manifest_difficulties: dict[str, Any] = {}
        for key, difficulty in DIFFICULTY_NAMES.items():
            details = difficulties.get(key)
            if not isinstance(details, dict):
                continue
            source_url = CHART_URL.format(song_id=song_id, difficulty=difficulty)
            report(f"fetching song={song_id} difficulty={difficulty}")
            chart, raw_bytes = fetch_json(source_url)
            if not isinstance(chart, list):
                raise ValueError(f"chart {song_id}/{difficulty} must be a JSON list")
            _validate_chart(chart, song_id=song_id, difficulty=difficulty)
            raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            chart_sha256 = _chart_sha256(chart)
            relative = Path("bestdori") / str(song_id) / f"{difficulty}.json"
            wrapper = {
                "schema_version": 1,
                "source": {
                    "provider": "bestdori",
                    "url": source_url,
                    "raw_sha256": raw_sha256,
                    "chart_sha256": chart_sha256,
                },
                "song": {
                    "bestdori_id": song_id,
                    "titles": titles,
                },
                "difficulty": {
                    "name": difficulty,
                    "level": int(details["playLevel"]),
                    "expected_notes": _metadata_note_count(song, key),
                },
                "chart": chart,
            }
            _write_json_atomic(output_root / relative, wrapper)
            manifest_difficulties[difficulty] = {
                "path": relative.as_posix(),
                "level": int(details["playLevel"]),
                "expected_notes": _metadata_note_count(song, key),
                "source_url": source_url,
                "raw_sha256": raw_sha256,
                "chart_sha256": chart_sha256,
            }

        if not manifest_difficulties:
            raise ValueError(f"song {song_id} has no downloadable difficulties")
        manifest_songs.append({
            "bestdori_song_id": song_id,
            "display_title": display_title,
            "category": str(spec.get("category", "unknown")),
            "titles": titles,
            "fingerprints": sorted(fingerprints),
            "jackets": jackets,
            "difficulties": manifest_difficulties,
        })

    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": {
            "provider": "bestdori",
            "song_index_url": SONGS_INDEX_URL,
            "runtime_network_access": False,
        },
        "songs": sorted(
            manifest_songs,
            key=lambda item: item["bestdori_song_id"],
        ),
    }
    _write_json_atomic(output_root / "manifest.json", manifest)
    report(f"wrote manifest songs={len(manifest_songs)}")
    return manifest


def _metadata_note_count(song: dict[str, Any], key: str) -> int | None:
    notes = song.get("notes")
    if not isinstance(notes, dict) or key not in notes:
        return None
    return int(notes[key])


def _validate_chart(
    chart: list[Any],
    *,
    song_id: int,
    difficulty: str,
) -> None:
    if not any(
        isinstance(item, dict)
        and item.get("type") == "BPM"
        and item.get("beat") is not None
        and item.get("bpm") is not None
        for item in chart
    ):
        raise ValueError(f"chart {song_id}/{difficulty} has no BPM event")
    supported = {"BPM", "System", "Single", "Directional", "Long", "Slide"}
    for item in chart:
        if not isinstance(item, dict) or item.get("type") not in supported:
            raise ValueError(
                f"chart {song_id}/{difficulty} contains unsupported entry"
            )


def _chart_sha256(chart: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        chart,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if result <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return result


def _network_fetcher(timeout: float, retries: int) -> FetchJson:
    def fetch(url: str) -> tuple[Any, bytes]:
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                request = urllib.request.Request(
                    url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "MaaBanGDream-chart-sync/1",
                    },
                )
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    raw = response.read()
                return json.loads(raw.decode("utf-8-sig")), raw
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(min(2.0, 0.25 * (2 ** (attempt - 1))))
        raise RuntimeError(f"failed to fetch {url}: {last_error}") from last_error

    return fetch


def _network_fetch_bytes(timeout: float, retries: int) -> FetchBytes:
    def fetch(url: str) -> bytes:
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                request = urllib.request.Request(
                    url,
                    headers={
                        "Accept": "image/png",
                        "User-Agent": "MaaBanGDream-chart-sync/1",
                    },
                )
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    content_type = str(response.headers.get("Content-Type", ""))
                    raw = response.read()
                if "image/" not in content_type:
                    raise ValueError(
                        f"Bestdori jacket returned {content_type or 'no content type'}"
                    )
                return raw
            except (OSError, ValueError) as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(min(2.0, 0.25 * (2 ** (attempt - 1))))
        raise RuntimeError(f"failed to fetch {url}: {last_error}") from last_error

    return fetch


def _load_song_specs(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("representative song list must use schema_version 1")
    songs = payload.get("songs")
    if not isinstance(songs, list) or not songs:
        raise ValueError("representative song list must contain songs")
    return songs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--song-list", type=Path, default=DEFAULT_SONG_LIST)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=3)
    args = parser.parse_args()
    if args.timeout <= 0 or args.retries <= 0:
        parser.error("--timeout and --retries must be positive")
    specs = _load_song_specs(args.song_list)
    manifest = sync_charts(
        specs,
        args.output_root,
        _network_fetcher(args.timeout, args.retries),
        _network_fetch_bytes(args.timeout, args.retries),
        progress=lambda message: print(message, flush=True),
    )
    difficulty_count = sum(
        len(song["difficulties"]) for song in manifest["songs"]
    )
    print(
        f"synced songs={len(manifest['songs'])} "
        f"difficulties={difficulty_count} manifest={args.output_root / 'manifest.json'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
