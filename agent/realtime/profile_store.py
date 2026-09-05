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
    note_skin_type: int = 1
    tap_effect: int = 1
    judgement_assist_effect: bool = True

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "EnvironmentSignature":
        try:
            resolution = value["resolution"]
            note_skin_type = value.get("note_skin_type", 1)
            tap_effect = value.get("tap_effect", 1)
            judgement_assist_effect = value.get(
                "judgement_assist_effect", True
            )
            if isinstance(note_skin_type, bool) or isinstance(tap_effect, bool):
                raise TypeError("visual setting numbers cannot be boolean")
            if (
                note_skin_type != int(note_skin_type)
                or tap_effect != int(tap_effect)
            ):
                raise ValueError("visual setting numbers must be integers")
            if not isinstance(judgement_assist_effect, bool):
                raise TypeError("judgement_assist_effect must be boolean")
            signature = cls(
                resolution=(int(resolution[0]), int(resolution[1])),
                dpi=int(value["dpi"]), game_fps=int(value["game_fps"]),
                render_quality=str(value["render_quality"]), note_speed=float(value["note_speed"]),
                note_skin_type=int(note_skin_type),
                tap_effect=int(tap_effect),
                judgement_assist_effect=judgement_assist_effect,
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
        if (
            isinstance(self.note_skin_type, bool)
            or not isinstance(self.note_skin_type, int)
            or not 1 <= self.note_skin_type <= 7
        ):
            raise ValueError("note_skin_type 必须是 1..7 的整数")
        if (
            isinstance(self.tap_effect, bool)
            or not isinstance(self.tap_effect, int)
            or not 1 <= self.tap_effect <= 5
        ):
            raise ValueError("tap_effect 必须是 1..5 的整数")
        if not isinstance(self.judgement_assist_effect, bool):
            raise ValueError("judgement_assist_effect 必须是布尔值")

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
    note_speed: float


class RealtimeProfileStore:
    """Store local, environment-bound realtime calibration profiles."""

    SCHEMA_VERSION = 1
    DIFFICULTIES = frozenset({"Easy", "Normal", "Hard", "Expert", "Special"})
    MAIN_DIFFICULTIES = ("Easy", "Normal", "Hard", "Expert")
    SELECTION_FILE = "selection.json"
    DEFAULT_RUNTIME_OPTIONS = {
        "life_safety_enabled": True,
        "life_exit_threshold": 200,
        "rehearsal_ignore_life_safety": True,
        "skip_process_conflict_cleanup": False,
        "game_effect_settings_enabled": True,
        "note_skin_type": 1,
        "judgement_assist_effect": True,
        "tap_effect": 1,
        "chart_prediction_enabled": True,
        "chart_predict_presses": True,
        "native_realtime_enabled": False,
        "cooperative_jitter_enabled": True,
        "play_failure_retry_count": 1,
        "calibration_note_speeds": {
            "Easy": 2.0,
            "Normal": 2.0,
            "Hard": 2.0,
            "Expert": 5.0,
            "Special": 5.0,
        },
    }

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
        rehearsal_ignore = options.get("rehearsal_ignore_life_safety", True)
        if not isinstance(rehearsal_ignore, bool):
            raise ValueError("rehearsal_ignore_life_safety 必须是布尔值")
        skip_conflict_cleanup = options.get("skip_process_conflict_cleanup", False)
        if not isinstance(skip_conflict_cleanup, bool):
            raise ValueError("skip_process_conflict_cleanup must be boolean")
        effect_settings_enabled = options.get("game_effect_settings_enabled", True)
        if not isinstance(effect_settings_enabled, bool):
            raise ValueError("game_effect_settings_enabled 必须是布尔值")
        judgement_assist = options.get("judgement_assist_effect", True)
        if not isinstance(judgement_assist, bool):
            raise ValueError("judgement_assist_effect 必须是布尔值")
        note_skin_raw = options.get("note_skin_type", 1)
        if isinstance(note_skin_raw, bool):
            raise ValueError("note_skin_type 必须是 1..7 的整数")
        try:
            note_skin_type = int(note_skin_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("note_skin_type 必须是 1..7 的整数") from exc
        if note_skin_raw != note_skin_type or not 1 <= note_skin_type <= 7:
            raise ValueError("note_skin_type 必须是 1..7 的整数")
        tap_effect_raw = options.get("tap_effect", 1)
        if isinstance(tap_effect_raw, bool):
            raise ValueError("tap_effect 必须是 1..5 的整数")
        try:
            tap_effect = int(tap_effect_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("tap_effect 必须是 1..5 的整数") from exc
        if tap_effect_raw != tap_effect or not 1 <= tap_effect <= 5:
            raise ValueError("tap_effect 必须是 1..5 的整数")
        chart_prediction_enabled = options.get("chart_prediction_enabled", True)
        if not isinstance(chart_prediction_enabled, bool):
            raise ValueError("chart_prediction_enabled 必须是布尔值")
        chart_predict_presses = options.get("chart_predict_presses", True)
        if not isinstance(chart_predict_presses, bool):
            raise ValueError("chart_predict_presses 必须是布尔值")
        native_realtime_enabled = options.get(
            "native_realtime_enabled", False
        )
        if not isinstance(native_realtime_enabled, bool):
            raise ValueError("native_realtime_enabled 必须是布尔值")
        cooperative_jitter_enabled = options.get(
            "cooperative_jitter_enabled", True
        )
        if not isinstance(cooperative_jitter_enabled, bool):
            raise ValueError("cooperative_jitter_enabled 必须是布尔值")
        retry_count_raw = options.get("play_failure_retry_count", 1)
        if isinstance(retry_count_raw, bool):
            raise ValueError("play_failure_retry_count 必须是 0..3 的整数")
        try:
            play_failure_retry_count = int(retry_count_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "play_failure_retry_count 必须是 0..3 的整数"
            ) from exc
        if (
            retry_count_raw != play_failure_retry_count
            or not 0 <= play_failure_retry_count <= 3
        ):
            raise ValueError("play_failure_retry_count 必须是 0..3 的整数")
        try:
            threshold = int(options.get("life_exit_threshold", 200))
        except (TypeError, ValueError) as exc:
            raise ValueError("life_exit_threshold 必须是整数") from exc
        if not 10 <= threshold <= 990:
            raise ValueError("life_exit_threshold 必须在 10..990 之间")
        configured_speeds = options.get(
            "calibration_note_speeds",
            cls.DEFAULT_RUNTIME_OPTIONS["calibration_note_speeds"],
        )
        if not isinstance(configured_speeds, dict):
            raise ValueError("calibration_note_speeds 必须是 JSON 对象")
        speeds: dict[str, float] = {}
        for difficulty in ("Easy", "Normal", "Hard", "Expert", "Special"):
            try:
                speed = round(float(configured_speeds[difficulty]), 2)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"calibration_note_speeds.{difficulty} 必须是数字"
                ) from exc
            if not 1.0 <= speed <= 12.0:
                raise ValueError(
                    f"calibration_note_speeds.{difficulty} 必须在 1.00..12.00 之间"
                )
            speeds[difficulty] = speed
        return {
            "life_safety_enabled": enabled,
            "life_exit_threshold": threshold,
            "rehearsal_ignore_life_safety": rehearsal_ignore,
            "skip_process_conflict_cleanup": skip_conflict_cleanup,
            "game_effect_settings_enabled": effect_settings_enabled,
            "note_skin_type": note_skin_type,
            "judgement_assist_effect": judgement_assist,
            "tap_effect": tap_effect,
            "chart_prediction_enabled": chart_prediction_enabled,
            "chart_predict_presses": chart_predict_presses,
            "native_realtime_enabled": native_realtime_enabled,
            "cooperative_jitter_enabled": cooperative_jitter_enabled,
            "play_failure_retry_count": play_failure_retry_count,
            "calibration_note_speeds": speeds,
        }

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
        return RuntimeSettings(
            **settings,
            profile_path=profile["_path"],
            note_speed=saved.note_speed,
        )

    @staticmethod
    def _same_visual_evaluation_environment(
        saved: EnvironmentSignature,
        current: EnvironmentSignature,
    ) -> bool:
        """Match every gameplay invariant except the three visual factors."""
        return (
            saved.resolution == current.resolution
            and saved.dpi == current.dpi
            and saved.game_fps == current.game_fps
            and saved.render_quality == current.render_quality
            and saved.note_speed == current.note_speed
        )

    def resolve_for_visual_evaluation(
        self,
        value: str | Path,
        *,
        difficulty: str,
        current_signature: EnvironmentSignature,
    ) -> RuntimeSettings:
        """Resolve accepted geometry for an isolated visual-factor experiment."""
        current_signature.validate()
        profile = self.load(value)
        if profile.get("accepted") is not True:
            raise ValueError("Profile 尚未通过用户真机验收")
        if profile.get("difficulty") not in self.compatible_difficulties(difficulty):
            raise ValueError(
                f"Profile 难度为 {profile.get('difficulty')!r}，"
                f"不兼容任务难度 {difficulty!r}"
            )
        saved = EnvironmentSignature.from_mapping(profile.get("environment", {}))
        if not self._same_visual_evaluation_environment(saved, current_signature):
            before, now = saved.to_mapping(), current_signature.to_mapping()
            visual_keys = {
                "note_skin_type", "tap_effect", "judgement_assist_effect"
            }
            mismatches = [
                key
                for key in now
                if key not in visual_keys and before[key] != now[key]
            ]
            details = ", ".join(
                f"{key}={before[key]!r} (当前 {now[key]!r})"
                for key in mismatches
            )
            raise ValueError(
                "Profile 与视觉评估核心环境不匹配: " + details
            )
        settings = self._validated_settings(profile.get("settings", {}))
        return RuntimeSettings(
            **settings,
            profile_path=profile["_path"],
            note_speed=saved.note_speed,
        )

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

    def resolve_latest_for_visual_evaluation(
        self,
        *,
        difficulty: str,
        current_signature: EnvironmentSignature,
    ) -> RuntimeSettings:
        current_signature.validate()
        pinned = self.pinned_profile(difficulty)
        if pinned:
            try:
                return self.resolve_for_visual_evaluation(
                    pinned,
                    difficulty=difficulty,
                    current_signature=current_signature,
                )
            except ValueError as exc:
                raise ValueError(
                    f"钉选 Profile 不可用于视觉评估，禁止自动回退: {exc}"
                ) from exc
        for source in self.compatible_difficulties(difficulty):
            candidates = [
                profile
                for profile in self.list_profiles(accepted_only=True)
                if profile.get("difficulty") == source
            ]
            for profile in candidates:
                try:
                    return self.resolve_for_visual_evaluation(
                        profile["_path"].name,
                        difficulty=difficulty,
                        current_signature=current_signature,
                    )
                except ValueError:
                    continue
        raise ValueError(
            f"没有已验收且核心环境匹配的 {difficulty} Profile"
        )

    @staticmethod
    def _same_visual_evaluation_precheck_environment(
        saved: EnvironmentSignature,
        current: EnvironmentSignature,
    ) -> bool:
        return (
            saved.resolution == current.resolution
            and saved.dpi == current.dpi
            and saved.game_fps == current.game_fps
            and saved.render_quality == current.render_quality
        )

    def resolve_latest_for_visual_evaluation_environment(
        self,
        *,
        difficulty: str,
        current_signature: EnvironmentSignature,
    ) -> RuntimeSettings:
        """Precheck an experiment before the accepted speed is read.

        Only the speed and visual factors are deferred.  Play must later call
        ``resolve_latest_for_visual_evaluation`` with the verified speed.
        """
        current_signature.validate()
        pinned = self.pinned_profile(difficulty)
        if pinned:
            profile = self.load(pinned)
            if profile.get("accepted") is not True:
                raise ValueError("钉选 Profile 尚未通过用户真机验收")
            if profile.get("difficulty") not in self.compatible_difficulties(difficulty):
                raise ValueError("钉选 Profile 难度不兼容")
            saved = EnvironmentSignature.from_mapping(profile.get("environment", {}))
            if not self._same_visual_evaluation_precheck_environment(
                saved, current_signature
            ):
                raise ValueError("钉选 Profile 与视觉评估核心环境不匹配")
            return self.resolve_for_visual_evaluation(
                pinned,
                difficulty=difficulty,
                current_signature=saved,
            )
        for source in self.compatible_difficulties(difficulty):
            candidates = [
                profile
                for profile in self.list_profiles(accepted_only=True)
                if profile.get("difficulty") == source
            ]
            for profile in candidates:
                saved = EnvironmentSignature.from_mapping(
                    profile.get("environment", {})
                )
                if self._same_visual_evaluation_precheck_environment(
                    saved, current_signature
                ):
                    return self.resolve_for_visual_evaluation(
                        profile["_path"].name,
                        difficulty=difficulty,
                        current_signature=saved,
                    )
        raise ValueError(
            f"没有已验收且核心环境匹配的 {difficulty} Profile"
        )

    @staticmethod
    def _same_non_speed_environment(
        saved: EnvironmentSignature,
        current: EnvironmentSignature,
    ) -> bool:
        return (
            saved.resolution == current.resolution
            and saved.dpi == current.dpi
            and saved.game_fps == current.game_fps
            and saved.render_quality == current.render_quality
            and saved.note_skin_type == current.note_skin_type
            and saved.tap_effect == current.tap_effect
            and saved.judgement_assist_effect
            == current.judgement_assist_effect
        )

    def resolve_latest_for_environment(
        self,
        *,
        difficulty: str,
        current_signature: EnvironmentSignature,
    ) -> RuntimeSettings:
        """Select a profile before the in-game speed has been read and corrected."""
        pinned = self.pinned_profile(difficulty)
        if pinned:
            profile = self.load(pinned)
            if profile.get("accepted") is not True:
                raise ValueError("钉选 Profile 尚未通过用户真机验收")
            if profile.get("difficulty") not in self.compatible_difficulties(difficulty):
                raise ValueError("钉选 Profile 难度不兼容")
            saved = EnvironmentSignature.from_mapping(profile.get("environment", {}))
            if not self._same_non_speed_environment(saved, current_signature):
                raise ValueError("钉选 Profile 与当前非流速环境不匹配")
            return self.resolve(
                pinned,
                difficulty=difficulty,
                current_signature=saved,
            )
        for source in self.compatible_difficulties(difficulty):
            candidates = [
                profile
                for profile in self.list_profiles(accepted_only=True)
                if profile.get("difficulty") == source
            ]
            for profile in candidates:
                saved = EnvironmentSignature.from_mapping(
                    profile.get("environment", {})
                )
                if self._same_non_speed_environment(saved, current_signature):
                    return self.resolve(
                        profile["_path"].name,
                        difficulty=difficulty,
                        current_signature=saved,
                    )
        raise ValueError(
            f"没有已验收且非流速环境匹配的 {difficulty} Profile"
        )

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

    def replace(self, value: str | Path, payload: dict[str, Any]) -> Path:
        """Atomically replace one existing Profile without changing its name."""
        path = self._path(value)
        if not path.exists():
            raise ValueError(f"Profile 不存在: {path.name}")
        difficulty = str(payload.get("difficulty", ""))
        self.compatible_difficulties(difficulty)
        clean = {key: item for key, item in payload.items() if key != "_path"}
        self._atomic_write(path, {"schema_version": self.SCHEMA_VERSION, **clean})
        return path

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
