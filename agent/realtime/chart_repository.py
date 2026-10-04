"""Read-only local chart repository used by the realtime hot path."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .chart_timeline import ChartTimeline
from .regional_difficulty import verified_level_variants
from .song_identity import (
    LOOSE_SAME_SONG_DISTANCE,
    UNKNOWN_SONG_ID,
    same_song,
)
from .song_title_ocr import normalize_song_title, title_similarity


@dataclass(frozen=True, slots=True)
class ChartSelection:
    bestdori_song_id: int
    title: str
    difficulty: str
    path: Path
    timeline: ChartTimeline
    expected_notes: int | None = None
    level: int | None = None
    titles: tuple[str, ...] = ()
    fingerprints: tuple[str, ...] = ()
    shared_jacket: bool = False
    # 共享封面组内该难度等级是否唯一：唯一时等级即可区分 FULL/普通等
    # 同封面谱面，最终封面确认无需再依赖经常失败的标题 OCR。
    shared_jacket_level_unique: bool = False


@dataclass(frozen=True, slots=True)
class ChartResolution:
    selection: ChartSelection | None
    reason: str


@dataclass(frozen=True, slots=True)
class CatalogSongIdentity:
    bestdori_song_id: int
    title: str
    titles: tuple[str, ...]
    fingerprints: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CatalogSongResolution:
    identity: CatalogSongIdentity | None
    reason: str


class LocalChartRepository:
    """Resolve a confirmed song fingerprint and exact difficulty locally."""

    SCHEMA_VERSION = 1

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.manifest_path = self.root / "manifest.json"

    def identify_by_cover_title(
        self,
        song_fingerprint: str,
        title: str,
        *,
        full_badge: bool | None = None,
    ) -> CatalogSongResolution:
        """只用开场封面与标题确认歌曲，不要求该难度已有本地谱面。"""
        if song_fingerprint == UNKNOWN_SONG_ID:
            return CatalogSongResolution(None, "song fingerprint is unknown")
        if not str(title).strip():
            return CatalogSongResolution(None, "song title is not confirmed")
        songs = self._load_manifest()["songs"]
        songs = _coalesce_equivalent_songs(songs)
        cover_matches = [
            song for song in songs
            if any(
                same_song(song_fingerprint, confirmed)
                for confirmed in song["fingerprints"]
            )
        ]
        if not cover_matches:
            # 一键监听没有准备页等级可作第二重约束。只有标题先独立收窄出
            # 候选后，才允许用宽松封面阈值吸收开场页裁切和边框差异。
            title_scope = _unique_title_matches(songs, title)
            cover_matches = [
                song for song in title_scope
                if any(
                    same_song(
                        song_fingerprint,
                        confirmed,
                        max_distance=LOOSE_SAME_SONG_DISTANCE,
                    )
                    for confirmed in song["fingerprints"]
                )
            ]
        if not cover_matches:
            return CatalogSongResolution(
                None,
                "song fingerprint is not confirmed",
            )
        if full_badge is not None:
            badge_matches = [
                song for song in cover_matches
                if _catalog_song_is_full(song) is bool(full_badge)
            ]
            if not badge_matches:
                return CatalogSongResolution(
                    None,
                    "FULL badge conflicts with final cover candidates",
                )
            cover_matches = badge_matches
        title_matches = _unique_title_matches(cover_matches, title)
        if not title_matches:
            return CatalogSongResolution(
                None,
                "song title does not match final cover",
            )
        if len(title_matches) != 1:
            return CatalogSongResolution(
                None,
                "song cover and title mapping is ambiguous",
            )
        song = title_matches[0]
        titles = tuple(str(value) for value in song.get("titles", ()))
        return CatalogSongResolution(
            CatalogSongIdentity(
                bestdori_song_id=int(song["bestdori_song_id"]),
                title=str(song.get("display_title") or titles[0]),
                titles=titles,
                fingerprints=tuple(
                    str(value) for value in song.get("fingerprints", ())
                ),
            ),
            "confirmed song by final cover and title",
        )

    def resolve(
        self,
        song_fingerprint: str,
        difficulty: str,
        *,
        level: int | None = None,
        title: str | None = None,
        bestdori_song_id: int | None = None,
    ) -> ChartResolution:
        manifest = self._load_manifest()
        # 这张封面在整份目录里是否只对应一首曲目。判断"封面本身能否唯一确定
        # 身份"必须在按标题或 bestdori id 收窄之前完成：FIRE BIRD 187/243 共用
        # 封面，一旦按 [FULL] 标题收窄就会变成"唯一命中"，等级会被误当成冗余。
        unique_cover_in_catalog = (
            sum(
                1
                for song in _coalesce_equivalent_songs(manifest["songs"])
                if any(
                    same_song(song_fingerprint, confirmed)
                    for confirmed in song["fingerprints"]
                )
            )
            == 1
        )
        songs = manifest["songs"]
        if bestdori_song_id is not None:
            songs = [
                song for song in songs
                if int(song["bestdori_song_id"]) == int(bestdori_song_id)
            ]
            if not songs:
                return ChartResolution(
                    None,
                    "confirmed song id is not present in local catalog",
                )
        songs = _coalesce_equivalent_songs(songs)
        if title and _FULL_TITLE_PREFIX.match(re.sub(r"['\"‘’]", "", str(title))):
            # 明确读到 FULL 就是版本证据，不能被首尾噪声裁剪抹成普通版。
            songs = [song for song in songs if any(
                _FULL_TITLE_PREFIX.match(str(value)) for value in song.get("titles", ())
            )]
        normalized_difficulty = str(difficulty).strip().lower()
        fingerprint_matches = [
            song for song in songs
            if any(
                same_song(song_fingerprint, confirmed)
                for confirmed in song["fingerprints"]
            )
        ]
        matched_exact_fingerprint = bool(fingerprint_matches)
        if not fingerprint_matches and level is not None:
            # 选曲页封面裁切/边框会让个别谱面稳定多翻转几 bit；只有同时
            # 读到等级时才用更宽阈值重试，随后仍由等级硬约束唯一化。
            fingerprint_matches = [
                song for song in songs
                if any(
                    same_song(
                        song_fingerprint,
                        confirmed,
                        max_distance=LOOSE_SAME_SONG_DISTANCE,
                    )
                    for confirmed in song["fingerprints"]
                )
            ]
        # 封面在整份目录里唯一命中时身份已由封面确定，等级不参与判别；此时
        # ±1 级的差异只能是区服进度或元数据回填（见 regional_difficulty 模块
        # 注释），不能因此把整首曲目判死。封面被多首曲目共用（[FULL]、
        # English Version、SPECIAL 等与原版共用封面）或等级差超过 1 级时，
        # 等级仍是唯一判别信号，继续硬拒绝。
        tolerated_level_drift = bool(
            matched_exact_fingerprint
            and unique_cover_in_catalog
            and level is not None
            and _regional_level_drift_tolerated(
                fingerprint_matches[0], normalized_difficulty, int(level),
            )
        )
        matches = fingerprint_matches
        level_scope = songs
        matched_by_level = False
        if level is not None:
            expected_level = int(level)
            level_scope = [
                song for song in songs
                if _difficulty_level_matches(
                    song, normalized_difficulty, expected_level,
                )
            ]
            level_matches = [song for song in matches if song in level_scope]
            if (
                matched_exact_fingerprint
                and matches
                and not level_matches
                and not tolerated_level_drift
            ):
                return ChartResolution(
                    None,
                    "selected song level does not match local chart metadata",
                )
            matched_by_level = (
                len(level_matches) == 1 and len(matches) != 1
            )
            # Level is part of song identity.  In particular, an OCR crop can
            # lose a leading [FULL] marker and otherwise make the shorter
            # same-title chart look like the unique title winner.  Never let
            # title similarity restore a candidate from the wrong level.
            # 唯一封面已确定身份时保留该候选，由 tolerated_level_drift 放行。
            if level_matches or not tolerated_level_drift:
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
            and not tolerated_level_drift
            and not _difficulty_level_matches(
                song, normalized_difficulty, int(level),
            )
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
        selected_level = int(level) if level is not None else _difficulty_level(
            song, normalized_difficulty,
        )
        same_level_shared = sum(
            1
            for candidate in fingerprint_matches
            if (
                selected_level is not None
                and _difficulty_level_matches(
                    candidate, normalized_difficulty, selected_level,
                )
            )
        )
        if matched_by_title:
            reason = "confirmed local chart by song title"
        elif matched_by_level:
            reason = "confirmed local chart by song level"
        elif tolerated_level_drift:
            reason = "confirmed local chart with regional level drift"
        else:
            reason = "confirmed local chart"
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
                level=selected_level,
                titles=tuple(str(value) for value in song.get("titles", ())),
                fingerprints=tuple(
                    str(value) for value in song.get("fingerprints", ())
                ),
                shared_jacket=len(fingerprint_matches) > 1,
                shared_jacket_level_unique=same_level_shared == 1,
            ),
            reason,
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


def _coalesce_equivalent_songs(songs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # 同名、同封面不能证明谱面相同。只接受有完整难度超集且每个重叠难度
    # 的等级、判定数和内容 SHA 均相同的条目；保留最完整条目的真实 ID/文件。
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for song in songs:
        titles = tuple(sorted({
            normalize_song_title(value) for value in song.get("titles", ())
        }))
        fingerprints = tuple(sorted(set(song["fingerprints"])))
        key = (
            (titles, fingerprints)
            if titles and all(titles) and fingerprints
            else (song["bestdori_song_id"],)
        )
        groups.setdefault(key, []).append(song)
    result = []
    for group in groups.values():
        canonical = min(group, key=lambda song: (
            -len(song["difficulties"]), song["bestdori_song_id"],
        ))
        entries = canonical["difficulties"]
        compatible = bool(entries) and all(
            bool(song["difficulties"])
            and all(
                difficulty in entries
                and re.fullmatch(
                    r"[0-9a-f]{64}", str(entry.get("chart_sha256", "")),
                ) is not None
                and entry.get("level") is not None
                and all(
                    entry.get(field) == entries[difficulty].get(field)
                    for field in ("chart_sha256", "level", "expected_notes")
                )
                for difficulty, entry in song["difficulties"].items()
            )
            for song in group
        )
        result.extend([canonical] if compatible else group)
    return result


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


# 区服进度与元数据回填造成的 Expert 等级差异实测恒为 1 级（见
# regional_difficulty 模块注释与 _VERIFIED_CN_EXPERT_LEVELS）。超过 1 级说明
# 等级读数或本地元数据本身不可信，仍按身份冲突硬拒绝。
MAX_REGIONAL_LEVEL_DRIFT = 1


def _regional_level_drift_tolerated(
    song: dict[str, Any], difficulty: str, observed_level: int,
) -> bool:
    """严格等级判据失败后，等级差是否仍可由区服/元数据回填解释。

    已登记的区服差异（_VERIFIED_CN_EXPERT_LEVELS）由
    _difficulty_level_matches 直接放行，不算容忍；只有严格判据确实失败、
    且差距在 MAX_REGIONAL_LEVEL_DRIFT 以内时才返回 True。
    """
    if _difficulty_level_matches(song, difficulty, observed_level):
        return False
    expected_level = _difficulty_level(song, difficulty)
    if expected_level is None:
        return False
    drift = abs(expected_level - int(observed_level))
    return 0 < drift <= MAX_REGIONAL_LEVEL_DRIFT


def _difficulty_level_matches(
    song: dict[str, Any], difficulty: str, observed_level: int,
) -> bool:
    entry = song.get("difficulties", {}).get(difficulty)
    if not isinstance(entry, dict):
        return False
    expected_level = _difficulty_level(song, difficulty)
    if expected_level == int(observed_level):
        return True
    return int(observed_level) in verified_level_variants(
        int(song["bestdori_song_id"]),
        difficulty,
        entry.get("chart_sha256"),
    )


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


def _catalog_song_is_full(song: dict[str, Any]) -> bool:
    """曲库条目是否明确属于带 FULL 前缀的长谱面。"""
    return any(
        _FULL_TITLE_PREFIX.match(str(title))
        for title in song.get("titles", ())
    )
