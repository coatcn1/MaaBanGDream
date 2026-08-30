"""Read-only local chart repository used by the realtime hot path."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .chart_timeline import ChartTimeline
from .song_identity import UNKNOWN_SONG_ID, same_song
from .song_title_ocr import title_similarity


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

    def resolve(
        self,
        song_fingerprint: str,
        difficulty: str,
        *,
        level: int | None = None,
        title: str | None = None,
    ) -> ChartResolution:
        manifest = self._load_manifest()
        songs = manifest["songs"]
        normalized_difficulty = str(difficulty).strip().lower()
        fingerprint_matches = [
            song for song in songs
            if any(
                same_song(song_fingerprint, confirmed)
                for confirmed in song["fingerprints"]
            )
        ]
        matches = fingerprint_matches
        level_scope = songs
        matched_by_level = False
        if level is not None:
            expected_level = int(level)
            level_scope = [
                song for song in songs
                if _difficulty_level(song, normalized_difficulty)
                == expected_level
            ]
            if not level_scope:
                return ChartResolution(
                    None,
                    "selected song level does not match local chart metadata",
                )
            level_matches = [song for song in matches if song in level_scope]
            matched_by_level = (
                len(level_matches) == 1 and len(matches) != 1
            )
            # Level is part of song identity.  In particular, an OCR crop can
            # lose a leading [FULL] marker and otherwise make the shorter
            # same-title chart look like the unique title winner.  Never let
            # title similarity restore a candidate from the wrong level.
            matches = level_matches
        matched_by_title = False
        if len(matches) != 1 and title:
            title_scope = matches or level_scope
            title_matches = _unique_title_matches(title_scope, title)
            if len(title_matches) == 1:
                matches = title_matches
                matched_by_title = True
            elif title_matches:
                matches = title_matches
        if not matches:
            if song_fingerprint == UNKNOWN_SONG_ID:
                return ChartResolution(
                    None,
                    (
                        "song title is not confirmed"
                        if title
                        else "song fingerprint is unknown"
                    ),
                )
            return ChartResolution(None, "song fingerprint is not confirmed")
        if len(matches) != 1:
            return ChartResolution(None, "song fingerprint mapping is ambiguous")

        song = matches[0]
        if (
            level is not None
            and _difficulty_level(song, normalized_difficulty) != int(level)
        ):
            return ChartResolution(
                None,
                "selected song level does not match local chart metadata",
            )
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
            (
                "confirmed local chart by song title"
                if matched_by_title
                else (
                    "confirmed local chart by song level"
                    if matched_by_level
                    else "confirmed local chart"
                )
            ),
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


def _difficulty_level(song: dict[str, Any], difficulty: str) -> int | None:
    entry = song.get("difficulties", {}).get(difficulty)
    if not isinstance(entry, dict) or entry.get("level") is None:
        return None
    try:
        return int(entry["level"])
    except (TypeError, ValueError):
        return None


def _unique_title_matches(
    songs: list[dict[str, Any]],
    observed: str,
) -> list[dict[str, Any]]:
    ranked: list[tuple[float, dict[str, Any]]] = []
    for song in songs:
        titles = song.get("titles", [])
        if not isinstance(titles, list):
            continue
        score = max(
            (
                title_similarity(observed, candidate)
                for title in titles
                for candidate in _local_title_match_forms(title)
            ),
            default=0.0,
        )
        ranked.append((score, song))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked or ranked[0][0] < 0.68:
        return []
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08:
        best = ranked[0][0]
        return [song for score, song in ranked if best - score < 0.08]
    return [ranked[0][1]]


_FULL_TITLE_PREFIX = re.compile(
    r"^\s*[\[［【(（]\s*FULL\s*[\]］】)）]\s*",
    re.IGNORECASE,
)


def _local_title_match_forms(title: Any) -> tuple[str, ...]:
    """Return safe OCR aliases for a local title.

    The title crop occasionally starts to the right of the decorative FULL
    badge.  The alias is only applied to local titles: when OCR *does* contain
    the marker it still uniquely favours the FULL chart.  If OCR omits it and
    level is unavailable, the ordinary and FULL songs tie and resolution
    correctly fails closed.
    """
    value = str(title)
    without_full = _FULL_TITLE_PREFIX.sub("", value)
    if without_full and without_full != value:
        return value, without_full
    return (value,)
