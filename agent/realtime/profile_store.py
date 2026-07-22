from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EnvironmentSignature:
    resolution: tuple[int, int]
    dpi: int
    game_fps: int
    render_quality: str
    note_speed: float

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "EnvironmentSignature":
        try:
            resolution = value["resolution"]
            signature = cls(
                resolution=(int(resolution[0]), int(resolution[1])),
                dpi=int(value["dpi"]),
                game_fps=int(value["game_fps"]),
                render_quality=str(value["render_quality"]),
                note_speed=float(value["note_speed"]),
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError(f"环境签名不完整: {exc}") from exc
        signature.validate()
        return signature

    def validate(self) -> None:
        width, height = self.resolution
        if width <= 0 or height <= 0:
            raise ValueError("分辨率必须为正整数")
        if not 80 <= self.dpi <= 1000:
            raise ValueError("DPI 必须在 80..1000 之间")
        if not 15 <= self.game_fps <= 240:
            raise ValueError("游戏帧率必须在 15..240 之间")
        if not self.render_quality.strip():
            raise ValueError("演出画质不能为空")
        if not 0.1 <= self.note_speed <= 20:
            raise ValueError("音符流速必须在 0.1..20 之间")

    def to_mapping(self) -> dict[str, Any]:
        data = asdict(self)
        data["resolution"] = list(self.resolution)
        return data


@dataclass(frozen=True)
class RuntimeSettings:
    target_fps: int
    timing_offset_ms: int
    frame_timeout_ms: int
    playfield_timeout_ms: int
    profile_path: Path


class RealtimeProfileStore:
    """Read and validate local realtime calibration profiles.

    Profile JSON files are local runtime data and must never be committed. A
    profile becomes usable only after explicit acceptance and an exact match of
    every environment-signature field.
    """

    SCHEMA_VERSION = 1
    DIFFICULTIES = frozenset({"Easy", "Normal", "Hard", "Expert", "Special"})

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, value: str | Path) -> Path:
        path = Path(value)
        if path.is_absolute():
            raise ValueError("Profile 只能使用 profiles 目录内的相对文件名")
        path = (self.root / path).resolve()
        root = self.root.resolve()
        if path.parent != root or path.suffix.lower() != ".json":
            raise ValueError("Profile 必须是 profiles 目录中的 JSON 文件")
        return path

    def list_profiles(self, *, accepted_only: bool = False) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        profiles: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.json"), reverse=True):
            try:
                profile = self.load(path.name)
            except (OSError, ValueError):
                continue
            if accepted_only and not profile["accepted"]:
                continue
            profiles.append(profile)
        return profiles

    def load(self, value: str | Path) -> dict[str, Any]:
        path = self._path(value)
        try:
            profile = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取 Profile: {exc}") from exc
        if not isinstance(profile, dict):
            raise ValueError("Profile 顶层必须是 JSON 对象")
        if profile.get("schema_version") != self.SCHEMA_VERSION:
            raise ValueError(f"不支持的 Profile 版本: {profile.get('schema_version')!r}")
        profile["_path"] = path
        return profile

    def resolve(
        self,
        value: str | Path,
        *,
        difficulty: str,
        current_signature: EnvironmentSignature,
    ) -> RuntimeSettings:
        profile = self.load(value)
        if profile.get("accepted") is not True:
            raise ValueError("Profile 尚未通过用户真机验收")
        if difficulty not in self.DIFFICULTIES:
            raise ValueError(f"不支持的难度: {difficulty}")
        if profile.get("difficulty") != difficulty:
            raise ValueError(
                f"Profile 难度为 {profile.get('difficulty')!r}，任务难度为 {difficulty!r}"
            )
        saved = EnvironmentSignature.from_mapping(profile.get("environment", {}))
        if saved != current_signature:
            before = saved.to_mapping()
            now = current_signature.to_mapping()
            mismatches = [key for key in now if before[key] != now[key]]
            details = ", ".join(
                f"{key}={before[key]!r}（当前 {now[key]!r}）" for key in mismatches
            )
            raise ValueError(f"Profile 与当前环境不匹配: {details}")
        settings = profile.get("settings")
        if not isinstance(settings, dict):
            raise ValueError("Profile 缺少 settings")
        try:
            target_fps = int(settings["target_fps"])
            timing_offset_ms = int(settings["timing_offset_ms"])
            frame_timeout_ms = int(settings["frame_timeout_ms"])
            playfield_timeout_ms = int(settings["playfield_timeout_ms"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Profile 运行参数不完整: {exc}") from exc
        if not 15 <= target_fps <= 240:
            raise ValueError("target_fps 必须在 15..240 之间")
        if not -250 <= timing_offset_ms <= 250:
            raise ValueError("timing_offset_ms 必须在 -250..250 之间")
        if not 50 <= frame_timeout_ms <= 5000:
            raise ValueError("frame_timeout_ms 必须在 50..5000 之间")
        if not frame_timeout_ms <= playfield_timeout_ms <= 10000:
            raise ValueError("playfield_timeout_ms 必须不小于帧超时且不超过 10000")
        return RuntimeSettings(
            target_fps=target_fps,
            timing_offset_ms=timing_offset_ms,
            frame_timeout_ms=frame_timeout_ms,
            playfield_timeout_ms=playfield_timeout_ms,
            profile_path=profile["_path"],
        )

    def write(self, payload: dict[str, Any]) -> Path:
        difficulty = str(payload.get("difficulty", ""))
        if difficulty not in self.DIFFICULTIES:
            raise ValueError(f"不支持的难度: {difficulty}")
        created_at = str(payload.get("created_at") or datetime.now().isoformat(timespec="seconds"))
        stamp = re.sub(r"[^0-9]", "", created_at)[:14]
        if len(stamp) != 14:
            raise ValueError("created_at 必须包含完整日期和时间")
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._path(f"{difficulty.lower()}-{stamp}.json")
        temporary = target.with_suffix(".json.tmp")
        data = {"schema_version": self.SCHEMA_VERSION, **payload}
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, target)
        return target
