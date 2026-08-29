"""Read-only local chart repository used by the realtime hot path."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .chart_timeline import ChartTimeline
from .song_identity import UNKNOWN_SONG_ID, same_song


@dataclass(frozen=True, slots=True)
class ChartSelection:
    bestdori_song_id: int
    title: str
    difficulty: str
    path: Path
    timeline: ChartTimeline
    expected_notes: int | None = None


@dataclass(frozen=True, slots=True)
class ChartResolution:
    selection: ChartSelection | None
    reason: str


class LocalChartRepository:
    """Resolve a confirmed song fingerprint and exact difficulty locally."""

    SCHEMA_VERSION = 1

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.manifest_path = self.root / "manifest.json"

    def resolve(self, song_fingerprint: str, difficulty: str) -> ChartResolution:
        if song_fingerprint == UNKNOWN_SONG_ID:
            return ChartResolution(None, "song fingerprint is unknown")
        manifest = self._load_manifest()
        matches = [
            song for song in manifest["songs"]
            if any(
                same_song(song_fingerprint, confirmed)
                for confirmed in song["fingerprints"]
            )
        ]
        if not matches:
            return ChartResolution(None, "song fingerprint is not confirmed")
        if len(matches) != 1:
            return ChartResolution(None, "song fingerprint mapping is ambiguous")

        song = matches[0]
        normalized_difficulty = str(difficulty).strip().lower()
        entry = song["difficulties"].get(normalized_difficulty)
        if entry is None:
            return ChartResolution(
                None,
                f"no local {normalized_difficulty} chart for confirmed song",
            )
        path = self._safe_chart_path(entry["path"])
        payload = self._read_json(path)
        chart = payload.get("chart") if isinstance(payload, dict) else None
        if not isinstance(chart, list):
            raise ValueError(f"local chart wrapper is invalid: {path}")
        digest = _chart_sha256(chart)
        if digest != entry["chart_sha256"]:
            raise ValueError(f"local chart hash mismatch: {path}")
        if payload.get("source", {}).get("chart_sha256") != digest:
            raise ValueError(f"local chart source hash mismatch: {path}")
        if int(payload.get("song", {}).get("bestdori_id", -1)) != song["bestdori_song_id"]:
            raise ValueError(f"local chart song id mismatch: {path}")
        if payload.get("difficulty", {}).get("name") != normalized_difficulty:
            raise ValueError(f"local chart difficulty mismatch: {path}")
        title = str(song.get("display_title") or song["titles"][0])
        expected_notes = entry.get(
            "expected_notes",
            payload.get("difficulty", {}).get("expected_notes"),
        )
        return ChartResolution(
            ChartSelection(
                bestdori_song_id=song["bestdori_song_id"],
                title=title,
                difficulty=normalized_difficulty,
                path=path,
                timeline=ChartTimeline.from_json(path),
                expected_notes=(
                    int(expected_notes) if expected_notes is not None else None
                ),
            ),
            "confirmed local chart",
        )

    def _load_manifest(self) -> dict[str, Any]:
        payload = self._read_json(self.manifest_path)
        if not isinstance(payload, dict):
            raise ValueError("chart manifest must be a JSON object")
        if payload.get("schema_version") != self.SCHEMA_VERSION:
            raise ValueError(
                f"unsupported chart manifest schema: {payload.get('schema_version')!r}"
            )
        songs = payload.get("songs")
        if not isinstance(songs, list):
            raise ValueError("chart manifest songs must be a list")
        for song in songs:
            if not isinstance(song, dict):
                raise ValueError("chart manifest song entries must be objects")
            if not isinstance(song.get("bestdori_song_id"), int):
                raise ValueError("chart manifest song id must be an integer")
            if not isinstance(song.get("fingerprints"), list):
                raise ValueError("chart manifest fingerprints must be a list")
            if not isinstance(song.get("difficulties"), dict):
                raise ValueError("chart manifest difficulties must be an object")
        return payload

    def _safe_chart_path(self, relative: str) -> Path:
        candidate = (self.root / str(relative)).resolve()
        if self.root not in candidate.parents or candidate.suffix.lower() != ".json":
            raise ValueError(f"unsafe chart path in manifest: {relative!r}")
        if not candidate.is_file():
            raise ValueError(f"local chart file is missing: {candidate}")
        return candidate

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read local chart data {path}: {exc}") from exc


def _chart_sha256(chart: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        chart,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
