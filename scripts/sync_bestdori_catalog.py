"""Synchronize the full Bestdori song catalog for offline realtime use.

Only Hard, Expert and Special charts are stored.  Jacket images are fetched
from the selected game server (CN by default), saved locally, and fingerprinted
for runtime song recognition.  Per-song failures are recorded in the manifest
without aborting the rest of the catalog.  Existing valid files are reused, so
an interrupted synchronization can be resumed safely.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import cv2
import numpy as np


if str(PROJECT_ROOT := Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.realtime.song_identity import fingerprint_jacket
from agent.realtime.regional_difficulty import catalog_level_metadata
from scripts.sync_bestdori_charts import (
    CHART_URL,
    SONGS_INDEX_URL,
    FetchBytes,
    FetchJson,
    _chart_sha256,
    _network_fetch_bytes,
    _network_fetcher,
    _validate_chart,
    _write_json_atomic,
)


DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "resource" / "charts"
TARGET_DIFFICULTIES = {
    "2": "hard",
    "3": "expert",
    "4": "special",
}
JACKET_URL = (
    "https://bestdori.com/assets/{server}/musicjacket/musicjacket{bundle_id}_rip/"
    "assets-star-forassetbundle-startapp-musicjacket-"
    "musicjacket{bundle_id}-{jacket_name}-jacket.png"
)
# Two legacy metadata names live in musicjacket30 instead of the bucket derived
# from their song IDs.  Bestdori's explorer confirms both locations.
JACKET_BUNDLE_OVERRIDES = {
    "miracle": 30,
    "kirayume": 30,
}
Progress = Callable[[str], None]


def sync_catalog(
    output_root: Path,
    fetch_json: FetchJson,
    fetch_bytes: FetchBytes,
    *,
    jacket_server: str = "cn",
    jacket_fallback_server: str | None = "jp,en",
    workers: int = 6,
    reuse_existing: bool = True,
    song_limit: int | None = None,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Synchronize all indexed songs and return the generated manifest."""

    if workers <= 0:
        raise ValueError("workers must be positive")
    valid_servers = {"jp", "en", "tw", "cn", "kr"}
    server = str(jacket_server).strip().lower()
    if server not in valid_servers:
        raise ValueError(f"unsupported jacket server: {jacket_server!r}")
    fallback_servers = tuple(
        item.strip().lower()
        for item in str(jacket_fallback_server or "").split(",")
        if item.strip()
    )
    unsupported = set(fallback_servers) - valid_servers
    if unsupported:
        raise ValueError(f"unsupported jacket fallback servers: {unsupported}")
    jacket_servers = tuple(
        dict.fromkeys((server, *fallback_servers))
    )

    report = progress or (lambda _message: None)
    output_root = output_root.resolve()
    report("fetching Bestdori song index")
    index, _ = fetch_json(SONGS_INDEX_URL)
    if not isinstance(index, dict) or not index:
        raise ValueError("Bestdori song index must be a non-empty JSON object")

    old_songs = _load_existing_manifest_songs(output_root / "manifest.json")
    indexed_songs: list[tuple[int, dict[str, Any]]] = []
    for raw_id, metadata in index.items():
        try:
            song_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if song_id <= 0 or not isinstance(metadata, dict):
            continue
        indexed_songs.append((song_id, metadata))
    indexed_songs.sort(key=lambda item: item[0])
    if not indexed_songs or len(indexed_songs) != len(index):
        raise ValueError("Bestdori song index contains invalid metadata")
    if song_limit is not None:
        if song_limit <= 0:
            raise ValueError("song_limit must be positive")
        indexed_songs = indexed_songs[:song_limit]

    results: list[dict[str, Any]] = []
    fatal_errors: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _sync_song,
                song_id,
                metadata,
                output_root,
                fetch_json,
                fetch_bytes,
                jacket_servers,
                server,
                reuse_existing,
                old_songs.get(song_id),
            ): song_id
            for song_id, metadata in indexed_songs
        }
        completed = 0
        for future in as_completed(futures):
            completed += 1
            song_id = futures[future]
            try:
                song = future.result()
                report(
                    f"[{completed}/{len(futures)}] song={song_id} "
                    f"charts={len(song['difficulties'])} "
                    f"jackets={len(song['jackets'])} errors={len(song['errors'])}"
                )
                results.append(song)
            except Exception as exc:  # keep the remainder of the catalog useful
                fatal_errors.append({"song_id": song_id, "error": str(exc)})
                if song_id in old_songs:
                    results.append(old_songs[song_id])
                report(
                    f"[{completed}/{len(futures)}] song={song_id} failed: {exc}"
                )

    # 索引暂时缺项或单曲元数据失败不能删除既有可用歌曲。
    stored_ids = {song["bestdori_song_id"] for song in results}
    results.extend(song for song_id, song in old_songs.items() if song_id not in stored_ids)
    results.sort(key=lambda item: item["bestdori_song_id"])
    chart_count = sum(len(song["difficulties"]) for song in results)
    jacket_count = sum(len(song["jackets"]) for song in results)
    recoverable_errors = sum(len(song["errors"]) for song in results)
    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "source": {
            "provider": "bestdori",
            "song_index_url": SONGS_INDEX_URL,
            "jacket_server": server,
            "jacket_fallback_servers": list(fallback_servers),
            "difficulties": list(TARGET_DIFFICULTIES.values()),
            "runtime_network_access": False,
        },
        "summary": {
            "indexed_songs": len(indexed_songs),
            "stored_songs": len(results),
            "songs_with_charts": sum(bool(song["difficulties"]) for song in results),
            "charts": chart_count,
            "jackets": jacket_count,
            "recoverable_errors": recoverable_errors,
            "fatal_errors": len(fatal_errors),
        },
        "fatal_errors": fatal_errors,
        "songs": results,
    }
    previous_success = None
    try:
        previous = json.loads((output_root / "manifest.json").read_text(encoding="utf-8-sig"))
        previous_success = previous.get("last_successful_check_at", previous.get("generated_at"))
    except (OSError, ValueError, TypeError):
        pass
    # 失败保留上次成功时间，定时器不能把部分失败当作成功后延迟整天。
    manifest["last_successful_check_at"] = (
        manifest["generated_at"] if not fatal_errors and not recoverable_errors else previous_success
    )
    _write_json_atomic(output_root / "manifest.json", manifest)
    report(
        f"wrote manifest songs={len(results)} charts={chart_count} "
        f"jackets={jacket_count} errors={recoverable_errors + len(fatal_errors)}"
    )
    return manifest


def _sync_song(
    song_id: int,
    metadata: dict[str, Any],
    output_root: Path,
    fetch_json: FetchJson,
    fetch_bytes: FetchBytes,
    jacket_servers: tuple[str, ...],
    default_jacket_server: str,
    reuse_existing: bool,
    old_song: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(metadata.get("musicTitle"), list):
        raise ValueError("metadata titles must be a list")
    titles = [str(value) for value in metadata.get("musicTitle", []) if value]
    if not titles:
        raise ValueError("metadata has no titles")
    difficulty_metadata = metadata.get("difficulty")
    if not isinstance(difficulty_metadata, dict) or not difficulty_metadata:
        raise ValueError("metadata has no difficulty map")

    fingerprints = {
        str(value) for value in (old_song or {}).get("fingerprints", []) if value
    }
    errors: list[dict[str, str]] = []
    jackets: list[dict[str, str]] = []
    jacket_names = metadata.get("jacketImage")
    if not isinstance(jacket_names, list):
        jacket_names = []
    default_bundle_id = ((song_id + 9) // 10) * 10
    for number, raw_name in enumerate(jacket_names, start=1):
        jacket_name = str(raw_name).strip()
        if not jacket_name:
            continue
        bundle_id = JACKET_BUNDLE_OVERRIDES.get(
            jacket_name.lower(), default_bundle_id
        )
        asset_names = tuple(dict.fromkeys((jacket_name, jacket_name.lower())))
        if reuse_existing:
            cached = next((entry for entry in (old_song or {}).get("jackets", [])
                           if entry.get("name") == jacket_name
                           and entry.get("server") in jacket_servers
                           and _valid_cached_jacket(output_root, entry)), None)
            if cached is not None:
                # 已校验的 fallback 封面直接复用，避免再次请求不存在的 CN 资源。
                jackets.append(dict(cached))
                fingerprints.add(cached["fingerprint"])
                continue
        attempt_errors: list[str] = []
        for jacket_server in jacket_servers:
            relative = Path("bestdori") / str(song_id) / (
                f"jacket-{jacket_server}-{number}.png"
            )
            path = _safe_resource_path(output_root, relative)
            for asset_name in asset_names:
                url = JACKET_URL.format(
                    server=jacket_server,
                    bundle_id=bundle_id,
                    jacket_name=quote(asset_name, safe="_-"),
                )
                try:
                    # 只有上方可信 manifest 快速路径可复用；坏图或名称变更必须重新下载。
                    raw = fetch_bytes(url)
                    image = cv2.imdecode(
                        np.frombuffer(raw, dtype=np.uint8),
                        cv2.IMREAD_COLOR,
                    )
                    identity = fingerprint_jacket(image)
                    if identity.method == "unknown":
                        raise ValueError("downloaded jacket is not a valid image")
                    if any(old.get("path") == relative.as_posix() and _valid_cached_jacket(output_root, old)
                           for old in (old_song or {}).get("jackets", [])):
                        # 已可用资源不能原位替换，退出/强杀时旧 manifest 仍能引用旧文件。
                        relative = relative.with_name(f"jacket-{jacket_server}-{number}-{hashlib.sha256(raw).hexdigest()}.png")
                        path = _safe_resource_path(output_root, relative)
                    if not path.is_file() or path.read_bytes() != raw:
                        _write_bytes_atomic(path, raw)
                    fingerprints.add(identity.song_id)
                    jackets.append({
                        "server": jacket_server,
                        "name": jacket_name,
                        "asset_name": asset_name,
                        "path": relative.as_posix(),
                        "source_url": url,
                        "raw_sha256": hashlib.sha256(raw).hexdigest(),
                        "fingerprint": identity.song_id,
                    })
                    break
                except Exception as exc:
                    attempt_errors.append(
                        f"{jacket_server}/{asset_name}: {exc}"
                    )
            else:
                continue
            break
        else:
            errors.append({
                "kind": "jacket",
                "item": jacket_name,
                "error": "; ".join(attempt_errors),
            })

    difficulties: dict[str, Any] = {}
    for key, difficulty in TARGET_DIFFICULTIES.items():
        details = difficulty_metadata.get(key)
        if not isinstance(details, dict):
            continue
        old_entry = (old_song or {}).get("difficulties", {}).get(difficulty)
        relative = _chart_relative(output_root, song_id, difficulty, old_entry)
        path = output_root / relative
        try:
            entry = None
            valid_old = _existing_chart_entry(
                path, song_id=song_id, difficulty=difficulty, relative=relative,
                level=(old_entry or {}).get("level", int(details["playLevel"])),
                expected_notes=(old_entry or {}).get("expected_notes"),
                expected_sha256=(old_entry or {}).get("chart_sha256"),
            ) if path.is_file() else None
            if reuse_existing and path.is_file():
                entry = _existing_chart_entry(
                    path,
                    song_id=song_id,
                    difficulty=difficulty,
                    relative=relative,
                    level=int(details["playLevel"]),
                    expected_notes=_metadata_note_count(metadata, key),
                    expected_sha256=(old_entry or {}).get("chart_sha256"),
                )
                previous_notes = (old_entry or {}).get("expected_notes")
                if (previous_notes is not None
                        and _metadata_note_count(metadata, key) is not None
                        and previous_notes != _metadata_note_count(metadata, key)):
                    entry = None
            if entry is None:
                source_url = CHART_URL.format(
                    song_id=song_id,
                    difficulty=difficulty,
                )
                chart, raw = fetch_json(source_url)
                if not isinstance(chart, list):
                    raise ValueError("chart response is not a JSON list")
                _validate_chart(chart, song_id=song_id, difficulty=difficulty)
                raw_sha256 = hashlib.sha256(raw).hexdigest()
                chart_sha256 = _chart_sha256(chart)
                wrapper = {
                    "schema_version": 1,
                    "source": {
                        "provider": "bestdori",
                        "url": source_url,
                        "raw_sha256": raw_sha256,
                        "chart_sha256": chart_sha256,
                    },
                    "song": {"bestdori_id": song_id, "titles": titles},
                    "difficulty": {
                        "name": difficulty,
                        "level": int(details["playLevel"]),
                        "expected_notes": _metadata_note_count(metadata, key),
                    },
                    "chart": chart,
                }
                if valid_old is not None:
                    # 包含元数据的完整内容寻址，重复的音符数变更也不能覆盖旧版本 wrapper。
                    version = hashlib.sha256(json.dumps(wrapper, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
                    relative = relative.with_name(f"{difficulty}-{version}.json")
                    path = _safe_resource_path(output_root, relative)
                _write_json_atomic(path, wrapper)
                entry = {
                    "path": relative.as_posix(),
                    "level": int(details["playLevel"]),
                    "expected_notes": _metadata_note_count(metadata, key),
                    "source_url": source_url,
                    "raw_sha256": raw_sha256,
                    "chart_sha256": chart_sha256,
                }
            difficulties[difficulty] = entry
            old_entry = (
                (old_song or {}).get("difficulties", {}).get(difficulty)
            )
            entry.update(catalog_level_metadata(
                bestdori_song_id=song_id,
                difficulty=difficulty,
                chart_sha256=entry.get("chart_sha256"),
                source_level=int(details["playLevel"]),
                jacket_server=default_jacket_server,
                old_entry=old_entry,
            ))
        except Exception as exc:
            errors.append({
                "kind": "chart",
                "item": difficulty,
                "error": str(exc),
            })
            if old_entry is not None and _existing_chart_entry(
                output_root / _chart_relative(output_root, song_id, difficulty, old_entry),
                song_id=song_id, difficulty=difficulty,
                relative=_chart_relative(output_root, song_id, difficulty, old_entry),
                level=old_entry["level"], expected_notes=old_entry.get("expected_notes"),
                expected_sha256=old_entry.get("chart_sha256"),
            ) is not None:
                difficulties[difficulty] = dict(old_entry)

    for difficulty, old_entry in (old_song or {}).get("difficulties", {}).items():
        if difficulty not in difficulties:
            relative = _chart_relative(output_root, song_id, difficulty, old_entry)
            if _existing_chart_entry(output_root / relative, song_id=song_id, difficulty=difficulty,
                                     relative=relative, level=old_entry["level"],
                                     expected_notes=old_entry.get("expected_notes"),
                                     expected_sha256=old_entry.get("chart_sha256")) is not None:
                difficulties[difficulty] = dict(old_entry)
    for old_jacket in (old_song or {}).get("jackets", []):
        if old_jacket not in jackets and _valid_cached_jacket(output_root, old_jacket):
            jackets.append(dict(old_jacket))

    return {
        "bestdori_song_id": song_id,
        "display_title": _localized_title(metadata.get("musicTitle")),
        "category": str(metadata.get("tag") or "unknown"),
        "titles": titles,
        "fingerprints": sorted(fingerprints),
        "jackets": jackets,
        "difficulties": difficulties,
        "errors": errors,
    }


def _chart_relative(output_root: Path, song_id: int, difficulty: str, entry: dict[str, Any] | None) -> Path:
    relative = Path((entry or {}).get("path", Path("bestdori") / str(song_id) / f"{difficulty}.json"))
    resolved = _safe_resource_path(output_root, relative)
    if relative.is_absolute() or resolved.parent != (output_root / "bestdori" / str(song_id)).resolve():
        raise ValueError("existing chart path is outside its song directory")
    return relative


def _safe_resource_path(output_root: Path, relative: Path) -> Path:
    resolved = (output_root / relative).resolve()
    if relative.is_absolute() or not resolved.is_relative_to(output_root):
        raise ValueError("catalog resource path is outside output root")
    return resolved


def _valid_cached_jacket(output_root: Path, entry: dict[str, Any]) -> bool:
    try:
        path = (output_root / entry["path"]).resolve()
        if not path.is_relative_to(output_root) or not path.is_file():
            return False
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry["raw_sha256"]:
            return False
        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        return fingerprint_jacket(image).song_id == entry["fingerprint"]
    except (KeyError, OSError, ValueError, TypeError, cv2.error):
        return False


def _existing_chart_entry(
    path: Path,
    *,
    song_id: int,
    difficulty: str,
    relative: Path,
    level: int,
    expected_notes: int | None,
    expected_sha256: str | None = None,
) -> dict[str, Any] | None:
    try:
        wrapper = json.loads(path.read_text(encoding="utf-8-sig"))
        chart = wrapper["chart"]
        source = wrapper["source"]
        if wrapper.get("schema_version") != 1 or not isinstance(chart, list):
            return None
        if int(wrapper["song"]["bestdori_id"]) != song_id:
            return None
        if wrapper["difficulty"]["name"] != difficulty:
            return None
        _validate_chart(chart, song_id=song_id, difficulty=difficulty)
        digest = _chart_sha256(chart)
        if digest != source.get("chart_sha256") or (expected_sha256 is not None and digest != expected_sha256):
            return None
        return {
            "path": relative.as_posix(),
            "level": level,
            "expected_notes": expected_notes,
            "source_url": str(source["url"]),
            "raw_sha256": str(source["raw_sha256"]),
            "chart_sha256": digest,
        }
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return None


def _localized_title(raw_titles: Any) -> str:
    titles = raw_titles if isinstance(raw_titles, list) else []
    for index in (3, 0, 1, 2, 4):
        if len(titles) > index and titles[index]:
            return str(titles[index])
    return "Unknown"


def _metadata_note_count(metadata: dict[str, Any], key: str) -> int | None:
    notes = metadata.get("notes")
    if not isinstance(notes, dict) or key not in notes:
        return None
    return int(notes[key])


def _load_existing_manifest_songs(path: Path) -> dict[int, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        songs = payload.get("songs", [])
        return {
            int(song["bestdori_song_id"]): song
            for song in songs
            if isinstance(song, dict) and "bestdori_song_id" in song
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def prune_other_difficulties(output_root: Path) -> int:
    """Remove only Easy/Normal chart wrappers from numeric Bestdori folders."""

    root = (output_root / "bestdori").resolve()
    removed = 0
    if not root.is_dir():
        return removed
    for song_dir in root.iterdir():
        if not song_dir.is_dir() or not song_dir.name.isdigit():
            continue
        if root not in song_dir.resolve().parents:
            continue
        for name in ("easy.json", "normal.json"):
            target = song_dir / name
            if target.is_file():
                target.unlink()
                removed += 1
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--jacket-server", default="cn")
    parser.add_argument("--jacket-fallback-server", default="jp,en")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-reuse", action="store_true")
    parser.add_argument("--prune-other-difficulties", action="store_true")
    args = parser.parse_args()
    if args.timeout <= 0 or args.retries <= 0 or args.workers <= 0:
        parser.error("--timeout, --retries and --workers must be positive")

    manifest = sync_catalog(
        args.output_root,
        _network_fetcher(args.timeout, args.retries),
        _network_fetch_bytes(args.timeout, args.retries),
        jacket_server=args.jacket_server,
        jacket_fallback_server=args.jacket_fallback_server,
        workers=args.workers,
        reuse_existing=not args.no_reuse,
        song_limit=args.limit,
        progress=lambda message: print(message, flush=True),
    )
    if args.prune_other_difficulties:
        print(
            f"pruned non-target charts={prune_other_difficulties(args.output_root)}",
            flush=True,
        )
    return 0 if not manifest["fatal_errors"] and not manifest["summary"]["recoverable_errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
