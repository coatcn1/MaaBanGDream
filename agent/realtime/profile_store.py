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
                dpi=int(value["dpi"]), game_fps=int(value["game_fps"]),
                render_quality=str(value["render_quality"]), note_speed=float(value["note_speed"]),
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
    """Store local, environment-bound realtime calibration profiles."""

    SCHEMA_VERSION = 1
    DIFFICULTIES = frozenset({"Easy", "Normal", "Hard", "Expert", "Special"})
    MAIN_DIFFICULTIES = ("Easy", "Normal", "Hard", "Expert")
    SELECTION_FILE = "selection.json"
    DEFAULT_RUNTIME_OPTIONS = {"life_safety_enabled": True, "life_exit_threshold": 200}

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, value: str | Path) -> Path:
        path = Path(value)
        if path.is_absolute():
            raise ValueError("Profile 只能使用 profiles 目录内的相对文件名")
        path = (self.root / path).resolve()
        if path.parent != self.root.resolve() or path.suffix.lower() != ".json":
            raise ValueError("Profile 必须是 profiles 目录中的 JSON 文件")
        if path.name == self.SELECTION_FILE:
            raise ValueError("选择状态文件不是 Profile")
        return path

    @classmethod
    def compatible_difficulties(cls, difficulty: str) -> tuple[str, ...]:
        if difficulty not in cls.DIFFICULTIES:
            raise ValueError(f"不支持的难度: {difficulty}")
        if difficulty == "Special":
            return ("Special",)
        return cls.MAIN_DIFFICULTIES[cls.MAIN_DIFFICULTIES.index(difficulty):]

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

    def list_profiles(self, *, accepted_only: bool = False) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        profiles = []
        for path in sorted(self.root.glob("*.json"), reverse=True):
            if path.name == self.SELECTION_FILE:
                continue
            try:
                profile = self.load(path.name)
            except (OSError, ValueError):
                continue
            if not accepted_only or profile.get("accepted") is True:
                profiles.append(profile)
        return profiles

    def _read_selection(self) -> dict[str, str]:
        return self._read_state()["pinned"]

    def _read_state(self) -> dict[str, Any]:
        path = self.root / self.SELECTION_FILE
        if not path.exists():
            return {"version": 1, "pinned": {}, "runtime_options": dict(self.DEFAULT_RUNTIME_OPTIONS)}
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取 Profile 选择状态: {exc}") from exc
        if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("pinned"), dict):
            raise ValueError("Profile 选择状态格式无效")
        return {
            "version": 1,
            "pinned": {str(key): str(value) for key, value in state["pinned"].items()},
            "runtime_options": self._validated_runtime_options(
                state.get("runtime_options", self.DEFAULT_RUNTIME_OPTIONS)
            ),
        }

    def _write_selection(self, pinned: dict[str, str]) -> None:
        state = self._read_state()
        state["pinned"] = pinned
        self._write_state(state)

    def _write_state(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / self.SELECTION_FILE
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    @classmethod
    def _validated_runtime_options(cls, options: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(options, dict):
            raise ValueError("runtime_options 必须是 JSON 对象")
        enabled = options.get("life_safety_enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("life_safety_enabled 必须是布尔值")
        try:
            threshold = int(options.get("life_exit_threshold", 200))
        except (TypeError, ValueError) as exc:
            raise ValueError("life_exit_threshold 必须是整数") from exc
        if not 10 <= threshold <= 990:
            raise ValueError("life_exit_threshold 必须在 10..990 之间")
        return {"life_safety_enabled": enabled, "life_exit_threshold": threshold}

    def runtime_options(self) -> dict[str, Any]:
        return dict(self._read_state()["runtime_options"])

    def update_runtime_options(self, options: dict[str, Any]) -> dict[str, Any]:
        validated = self._validated_runtime_options(options)
        state = self._read_state()
        state["runtime_options"] = validated
        self._write_state(state)
        return dict(validated)

    def pinned_profile(self, difficulty: str) -> str | None:
        self.compatible_difficulties(difficulty)
        return self._read_selection().get(difficulty)

    def pin(self, difficulty: str, value: str | Path) -> dict[str, str]:
        compatible = self.compatible_difficulties(difficulty)
        profile = self.load(value)
        if profile.get("difficulty") not in compatible:
            raise ValueError(f"Profile 难度 {profile.get('difficulty')!r} 不兼容任务难度 {difficulty!r}")
        pinned = self._read_selection()
        pinned[difficulty] = profile["_path"].name
        self._write_selection(pinned)
        return pinned

    def unpin(self, difficulty: str) -> dict[str, str]:
        self.compatible_difficulties(difficulty)
        pinned = self._read_selection()
        pinned.pop(difficulty, None)
        self._write_selection(pinned)
        return pinned

    @staticmethod
    def _validated_settings(settings: dict[str, Any]) -> dict[str, int]:
        try:
            values = {key: int(settings[key]) for key in (
                "target_fps", "timing_offset_ms", "frame_timeout_ms", "playfield_timeout_ms"
            )}
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Profile 运行参数不完整: {exc}") from exc
        if not 15 <= values["target_fps"] <= 240:
            raise ValueError("target_fps 必须在 15..240 之间")
        if not -250 <= values["timing_offset_ms"] <= 250:
            raise ValueError("timing_offset_ms 必须在 -250..250 之间")
        if not 50 <= values["frame_timeout_ms"] <= 5000:
            raise ValueError("frame_timeout_ms 必须在 50..5000 之间")
        if not values["frame_timeout_ms"] <= values["playfield_timeout_ms"] <= 10000:
            raise ValueError("playfield_timeout_ms 必须不小于 frame_timeout_ms 且不超过 10000")
        return values

    def resolve(self, value: str | Path, *, difficulty: str, current_signature: EnvironmentSignature) -> RuntimeSettings:
        profile = self.load(value)
        if profile.get("accepted") is not True:
            raise ValueError("Profile 尚未通过用户真机验收")
        if profile.get("difficulty") not in self.compatible_difficulties(difficulty):
            raise ValueError(f"Profile 难度为 {profile.get('difficulty')!r}，不兼容任务难度 {difficulty!r}")
        saved = EnvironmentSignature.from_mapping(profile.get("environment", {}))
        if saved != current_signature:
            before, now = saved.to_mapping(), current_signature.to_mapping()
            mismatches = [key for key in now if before[key] != now[key]]
            details = ", ".join(f"{key}={before[key]!r}（当前 {now[key]!r}）" for key in mismatches)
            raise ValueError(f"Profile 与当前环境不匹配: {details}")
        settings = self._validated_settings(profile.get("settings", {}))
        return RuntimeSettings(**settings, profile_path=profile["_path"])

    def resolve_latest(self, *, difficulty: str, current_signature: EnvironmentSignature) -> RuntimeSettings:
        pinned = self.pinned_profile(difficulty)
        if pinned:
            try:
                return self.resolve(pinned, difficulty=difficulty, current_signature=current_signature)
            except ValueError as exc:
                raise ValueError(f"钉选 Profile 无效，禁止自动回退: {exc}") from exc
        for source in self.compatible_difficulties(difficulty):
            candidates = [p for p in self.list_profiles(accepted_only=True) if p.get("difficulty") == source]
            for profile in candidates:
                try:
                    return self.resolve(profile["_path"].name, difficulty=difficulty, current_signature=current_signature)
                except ValueError:
                    continue
        raise ValueError(f"没有已验收且环境匹配的 {difficulty} Profile")

    def update_settings(self, value: str | Path, *, target_fps: int, timing_offset_ms: int,
                        frame_timeout_ms: int, playfield_timeout_ms: int,
                        modified_at: str | None = None) -> dict[str, Any]:
        profile = self.load(value)
        path = profile.pop("_path")
        profile["settings"] = self._validated_settings(locals())
        profile["accepted"] = False
        profile["modified_at"] = modified_at or datetime.now().isoformat(timespec="seconds")
        profile["invalidated_reason"] = "manual_edit"
        self._atomic_write(path, profile)
        profile["_path"] = path
        return profile

    def accept_latest(self, *, difficulty: str, current_signature: EnvironmentSignature,
                      accepted_at: str | None = None) -> Path:
        self.compatible_difficulties(difficulty)
        candidates = [p for p in self.list_profiles() if p.get("difficulty") == difficulty and p.get("accepted") is not True]
        if not candidates:
            raise ValueError(f"没有可验收的 {difficulty} Profile 草稿")
        profile = candidates[0]
        if EnvironmentSignature.from_mapping(profile.get("environment", {})) != current_signature:
            raise ValueError("Profile 草稿与当前环境不匹配，不能验收")
        path = profile.pop("_path")
        profile["accepted"] = True
        profile["accepted_at"] = accepted_at or datetime.now().isoformat(timespec="seconds")
        profile.pop("invalidated_reason", None)
        self._atomic_write(path, profile)
        return path

    def _atomic_write(self, path: Path, data: dict[str, Any]) -> None:
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def write(self, payload: dict[str, Any]) -> Path:
        difficulty = str(payload.get("difficulty", ""))
        self.compatible_difficulties(difficulty)
        created_at = str(payload.get("created_at") or datetime.now().isoformat(timespec="seconds"))
        stamp = re.sub(r"[^0-9]", "", created_at)[:14]
        if len(stamp) != 14:
            raise ValueError("created_at 必须包含完整日期和时间")
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._path(f"{difficulty.lower()}-{stamp}.json")
        suffix = 1
        while target.exists():
            target = self._path(f"{difficulty.lower()}-{stamp}-{suffix}.json")
            suffix += 1
        self._atomic_write(target, {"schema_version": self.SCHEMA_VERSION, **payload})
        return target
