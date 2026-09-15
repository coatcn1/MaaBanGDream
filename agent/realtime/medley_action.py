from __future__ import annotations

import json
import os
import threading
import time
import traceback
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import cv2
import numpy as np
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    from ..common_recover import CommonRecover
    from ..foreground_guard import GAME_PACKAGE, require_game_foreground
    from ..live_select import LiveSelectFind, _find_tour_live_card
    from ..screen_refresh import ScreenRefreshCancelled, capture_image
    from ..task_reporting import (
        TaskProgress,
        latest_failure_reason,
        log_task,
        record_failure_reason,
    )
except ImportError:
    from common_recover import CommonRecover
    from foreground_guard import GAME_PACKAGE, require_game_foreground
    from live_select import LiveSelectFind, _find_tour_live_card
    from screen_refresh import ScreenRefreshCancelled, capture_image
    from task_reporting import (
        TaskProgress,
        latest_failure_reason,
        log_task,
        record_failure_reason,
    )

from .chart_repository import LocalChartRepository
from .difficulty_action import (
    RealtimeDifficultySelect,
    read_song_level,
    selected_difficulty,
)
from .formal_preflight import RealtimeFormalPreflight
from .game_effect_settings_action import RealtimeGameSpeedSettingsGate
from .live_session import (
    append_current_run_event,
    current_live_run,
    reset_live_run,
    update_live_run,
)
from .native_prearm import discard_prearmed_backend
from .performance_settings_action import (
    RealtimePerformanceSettingsGate,
    _speed_settings_target,
    activate_speed_settings_target,
    publish_verified_performance_settings,
    verified_settings,
)
from .profile_action import PROJECT_ROOT
from .profile_play_action import (
    RealtimeProfileCheck,
    RealtimeProfilePlay,
    finalize_deferred_result,
)
from .profile_store import (
    EnvironmentSignature,
    RealtimeProfileStore,
    engine_from_native_flag,
)
from .rehearsal_action import frame_resolution
from .result_navigation import (
    RESULT_ANIMATION_SKIP_POINT,
    STORY_NODES,
    advance_result_cadence,
    accelerated_back,
    handle_story_page,
)
from .result_parser import LiveResult, ResultParser
from .song_identity import (
    LOOSE_SAME_SONG_DISTANCE,
    UNKNOWN_SONG_ID,
    fingerprint_jacket,
    same_song,
)
from .song_title_ocr import (
    TitleReading,
    recognize_song_title,
    title_similarity,
)
from .vision_io import imread_unicode, imwrite_unicode


MEDLEY_DPI = 240
MEDLEY_GAME_FPS = 60
MEDLEY_RENDER_QUALITY = "standard"
HOME_LIVE_POINT = (1175, 645)
TOUR_TYPE_POINTS = {"task": (430, 445), "free": (840, 445)}
FREE_SLOT_POINTS = ((325, 255), (735, 255), (1145, 255))
FREE_RANDOM_POINT = (687, 642)
SONG_CONFIRM_POINT = (1065, 650)
OVERVIEW_NEXT_POINT = (1125, 640)
MEDLEY_START_POINT = (1125, 640)
TRUSTED_TITLE_CONFIDENCE = 0.7

TASK_SLOT_LAYOUTS = (
    {
        "cover": (40, 181, 158, 158),
        "title": (40, 340, 370, 52),
        "level": (352, 304, 65, 35),
        "targets": {
            "Easy": (67, 415), "Normal": (122, 415),
            "Hard": (180, 415), "Expert": (295, 415),
            "Special": (374, 415),
        },
    },
    {
        "cover": (454, 181, 153, 158),
        "title": (450, 340, 370, 52),
        "level": (765, 304, 65, 35),
        "targets": {
            "Easy": (480, 415), "Normal": (535, 415),
            "Hard": (594, 415), "Expert": (708, 415),
            "Special": (787, 415),
        },
    },
    {
        "cover": (867, 181, 158, 158),
        "title": (865, 340, 370, 52),
        "level": (1177, 304, 65, 35),
        "targets": {
            "Easy": (893, 415), "Normal": (949, 415),
            "Hard": (1007, 415), "Expert": (1121, 415),
            "Special": (1200, 415),
        },
    },
)

PREPARATION_COVER_ROIS = (
    (139, 166, 158, 158),
    (554, 166, 158, 158),
    (956, 166, 158, 158),
)
PREPARATION_DIFFICULTY_TARGETS = (
    {
        "Easy": (68, 379), "Normal": (143, 379),
        "Hard": (218, 379), "Expert": (293, 379),
        "Special": (371, 379),
    },
    {
        "Easy": (486, 379), "Normal": (557, 379),
        "Hard": (634, 379), "Expert": (705, 379),
        "Special": (791, 379),
    },
    {
        "Easy": (883, 379), "Normal": (960, 379),
        "Hard": (1034, 379), "Expert": (1110, 379),
        "Special": (1193, 379),
    },
)
PREPARATION_TITLE_ROI = (105, 530, 650, 55)
PREPARATION_LEVEL_ROI = (125, 575, 70, 40)
STAGE_POINTS = ((176, 128), (590, 128), (1000, 128))

RESULT_TITLE_ROI = (245, 15, 760, 55)
RESULT_DIFFICULTY_ROI = (140, 15, 110, 50)
RESULT_LEVEL_ROI = (1000, 15, 70, 40)
RESULT_TEMPLATE = PROJECT_ROOT / "resource" / "image" / "result_judgement_details.png"
ESC_ONLY_REWARD_TEMPLATES = (
    PROJECT_ROOT / "resource" / "image" / "medley_achievement_reward_overview.png",
)
REWARD_TEMPLATES = (
    PROJECT_ROOT / "resource" / "image" / "result_reward_confirm.png",
    PROJECT_ROOT / "resource" / "image" / "result_reward_ok.png",
)
RESULT_NAVIGATION_MAX_CYCLES = 60


class MedleyResultsLeftBeforeCollection(RuntimeError):
    """旧会话的结算未读完，但页面已经回到主页或巡演入口。"""

    def __init__(self, results_completed: int) -> None:
        self.results_completed = int(results_completed)
        super().__init__(
            "组曲结算尚未完整保存就已离开结算页面："
            f"仅保存 {self.results_completed}/3 张 PGGBM"
        )


class MedleyPlayFailure(RuntimeError):
    """单曲演奏已启动后的结构化失败，供组曲决定是否整组重开。"""

    def __init__(
        self,
        *,
        song_index: int,
        report_path: str,
        reason: str,
        result_status: str,
        retryable: bool,
    ) -> None:
        self.song_index = int(song_index)
        self.report_path = str(report_path)
        self.reason = str(reason)
        self.result_status = str(result_status)
        self.retryable = bool(retryable)
        super().__init__(
            f"第{self.song_index}曲实时演奏未完成：{self.reason}"
        )


def _failure_text(payload: dict[str, Any], latest_reason: str) -> str:
    """从延迟报告和运行时失败原因收集同一局的终态证据。"""
    native = payload.get("native")
    values: list[object] = [
        (
            native.get("game_terminal_reason")
            if isinstance(native, dict)
            else None
        ),
        payload.get("terminal_reason"),
        payload.get("reason"),
        latest_reason,
    ]
    return next(
        (
            str(value).strip()
            for value in values
            if isinstance(value, str) and value.strip()
        ),
        "RealtimeProfilePlay 返回失败，但未留下可解析的终态原因",
    )


def _retryable_play_failure(result_status: str, reason: str) -> bool:
    """只重试已开演后的生命失败或瞬时引擎失败，保留硬冲突的 fail-closed。"""
    status = str(result_status).strip().casefold()
    text = str(reason).strip().casefold()
    if status == "stopped" or any(
        marker in text for marker in ("用户已停止", "task is stopping")
    ):
        return False
    if status == "preflight_error" or any(
        marker in text
        for marker in (
            "profile", "配置", "身份", "谱面", "难度", "流速",
            "final cover", "开演前",
        )
    ):
        return False
    if any(marker in text for marker in ("生命值归零", "生命归零")):
        return True
    return status in {
        "engine_error", "engine_incomplete", "playfield_start_timeout",
    }


def read_medley_play_failure(
    report_path: str | Path,
    latest_reason: str = "",
    *,
    run_id: str | None = None,
) -> MedleyPlayFailure:
    """读取失败报告；延迟报告缺失时仅按本局 run ID 查找即时报告。"""
    requested = Path(report_path)
    path = requested if requested.is_absolute() else PROJECT_ROOT / requested
    payload: dict[str, Any] = {}
    try:
        candidate = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(candidate, dict):
            payload = candidate
    except (OSError, json.JSONDecodeError):
        pass
    if not payload and run_id:
        output = PROJECT_ROOT / "screencap"
        suffix = f"-{str(run_id)[:8]}.json"
        candidates = sorted(
            output.glob(f"realtime-result-*{suffix}"),
            key=lambda value: value.stat().st_mtime,
            reverse=True,
        )
        for candidate_path in candidates:
            try:
                candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(candidate, dict):
                payload = candidate
                path = candidate_path
                break
    result_status = str(payload.get("result_status", "unknown"))
    reason = _failure_text(payload, latest_reason)
    return MedleyPlayFailure(
        song_index=0,
        report_path=str(path),
        reason=reason,
        result_status=result_status,
        retryable=_retryable_play_failure(result_status, reason),
    )

DEFAULT_SETTINGS: dict[str, object] = {
    "tour_type": "free",
    "song_mode": "random",
    "difficulty": "Expert",
    "count": 3,
    "debug_recording": False,
    "diagnostic_trace": True,
}
_SETTINGS = dict(DEFAULT_SETTINGS)
_SETTINGS_LOCK = threading.Lock()


def parse_custom_action_params(raw: object) -> dict[str, object]:
    decoded = json.loads(raw) if isinstance(raw, str) and raw else raw
    if decoded is None:
        return {}
    if not isinstance(decoded, dict):
        raise ValueError("组曲 Custom Action 参数必须是 JSON 对象")
    return dict(decoded)


def configure_medley_settings(params: dict[str, object]) -> dict[str, object]:
    """逐项合并任务选项，避免 Pipeline override 整块替换。"""
    with _SETTINGS_LOCK:
        candidate = (
            dict(DEFAULT_SETTINGS)
            if bool(params.get("reset", False))
            else dict(_SETTINGS)
        )
        for key in DEFAULT_SETTINGS:
            if key in params:
                candidate[key] = params[key]
        if candidate["tour_type"] not in {"free", "task"}:
            raise ValueError("巡演类型必须是 free 或 task")
        if candidate["song_mode"] not in {"current", "random"}:
            raise ValueError("自由巡演歌曲模式必须是 current 或 random")
        if candidate["difficulty"] not in RealtimeProfileStore.DIFFICULTIES:
            raise ValueError(f"不支持的组曲难度：{candidate['difficulty']}")
        try:
            count = int(candidate["count"])
        except (TypeError, ValueError) as exc:
            raise ValueError("组曲次数必须是 3 到 99 的整数且为 3 的倍数") from exc
        if not 3 <= count <= 99 or count % 3 != 0:
            raise ValueError("组曲次数必须是 3 到 99 的整数且为 3 的倍数")
        candidate["count"] = count
        for key in ("debug_recording", "diagnostic_trace"):
            if not isinstance(candidate[key], bool):
                raise ValueError(f"{key} 必须是布尔值")
        _SETTINGS.clear()
        _SETTINGS.update(candidate)
        return dict(_SETTINGS)


def current_medley_settings() -> dict[str, object]:
    with _SETTINGS_LOCK:
        return dict(_SETTINGS)


@dataclass(frozen=True, slots=True)
class MedleySong:
    index: int
    requested_difficulty: str
    difficulty: str
    song_id: str
    song_id_method: str
    bestdori_song_id: int | None
    title: str
    title_confidence: float
    level: int
    expected_notes: int | None
    profile: str
    note_speed: float
    observed_title: str | None = None
    title_source: str = "unknown"
    report_path: str | None = None

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "MedleySong":
        return cls(
            index=int(value["index"]),
            requested_difficulty=str(value["requested_difficulty"]),
            difficulty=str(value["difficulty"]),
            song_id=str(value["song_id"]),
            song_id_method=str(value.get("song_id_method", "unknown")),
            bestdori_song_id=(
                None
                if value.get("bestdori_song_id") is None
                else int(value["bestdori_song_id"])
            ),
            title=str(value["title"]),
            title_confidence=float(value.get("title_confidence", 0.0)),
            level=int(value["level"]),
            expected_notes=(
                None
                if value.get("expected_notes") is None
                else int(value["expected_notes"])
            ),
            profile=str(value["profile"]),
            note_speed=float(value["note_speed"]),
            observed_title=(
                None
                if value.get("observed_title") is None
                else str(value["observed_title"])
            ),
            title_source=str(value.get("title_source", "unknown")),
            report_path=(
                None
                if value.get("report_path") is None
                else str(value["report_path"])
            ),
        )


def detect_medley_stage(image: np.ndarray) -> int | None:
    """读取顶部红色三角，判断当前等待开始的是第几曲。"""
    if not isinstance(image, np.ndarray) or image.shape[:2] != (720, 1280):
        return None
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    bar = hsv[110:149, 20:1258]
    bar_neutral = (
        (bar[:, :, 1] < 35)
        & (bar[:, :, 2] >= 45)
        & (bar[:, :, 2] <= 130)
    )
    # 只在贯穿三槽的深灰阶段栏上读取红色三角；歌曲列表、巡演首页和
    # 活动点数页在相同坐标也有红色装饰或数字，不能据此误判为续跑页。
    if float(bar_neutral.mean()) < 0.85:
        return None
    scores: list[int] = []
    for x, y in STAGE_POINTS:
        roi = hsv[y - 16:y + 17, x - 25:x + 25]
        hue, saturation, value = cv2.split(roi)
        scores.append(int(np.count_nonzero(
            ((hue <= 12) | (hue >= 165))
            & (saturation >= 120)
            & (value >= 150)
        )))
    winner = int(np.argmax(scores))
    return winner + 1 if scores[winner] >= 80 else None


def _crop(image: np.ndarray, roi: tuple[int, int, int, int]) -> np.ndarray:
    x, y, width, height = roi
    crop = image[y:y + height, x:x + width]
    if crop.shape[:2] != (height, width):
        raise RuntimeError(f"组曲识别区域越界：{roi}")
    return crop


def _recognize_tight_title(
    image: np.ndarray,
    roi: tuple[int, int, int, int],
) -> TitleReading | None:
    """裁掉巡演准备页标题周围的大块空白后再送入单行 OCR。"""
    x, y, width, height = roi
    crop = _crop(image, roi)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    ys, xs = np.where(gray < 180)
    if len(xs) < 20:
        return None
    left = max(0, int(xs.min()) - 3)
    top = max(0, int(ys.min()) - 3)
    right = min(width, int(xs.max()) + 4)
    bottom = min(height, int(ys.max()) + 4)
    return recognize_song_title(
        image,
        (x + left, y + top, right - left, bottom - top),
    )


def _canonical_song_identity(
    identity,
    difficulty: str,
    level: int,
    title_reading: TitleReading | None,
    *,
    repository: LocalChartRepository,
) -> tuple[str, str, int | None, str, int | None]:
    resolution = repository.resolve(
        identity.song_id,
        difficulty,
        level=level,
        title=(title_reading.text if title_reading is not None else None),
    )
    if resolution.selection is not None:
        selection = resolution.selection
        canonical = (
            selection.fingerprints[0]
            if selection.fingerprints else identity.song_id
        )
        return (
            canonical,
            identity.method,
            selection.bestdori_song_id,
            selection.title,
            selection.expected_notes,
        )
    if title_reading is None:
        # 巡演选曲页的标题位置与单人页面不同。封面、难度和等级尚不能
        # 唯一解析时，先保留有界的候选身份，随后在准备页和最终封面页
        # 继续补读标题；这里不能因为一次 OCR 为空直接终止整轮组曲。
        return identity.song_id, identity.method, None, "", None
    catalog = repository.identify_by_cover_title(
        identity.song_id,
        title_reading.text,
    )
    if catalog.identity is not None:
        canonical = (
            catalog.identity.fingerprints[0]
            if catalog.identity.fingerprints else identity.song_id
        )
        if difficulty == "Special":
            raise RuntimeError(
                "Special 组曲缺少可信本地谱面，禁止按视觉回退演奏"
            )
        return (
            canonical,
            identity.method,
            catalog.identity.bestdori_song_id,
            catalog.identity.title,
            None,
        )
    raise RuntimeError(f"组曲歌曲身份未确认：{resolution.reason}；{catalog.reason}")


def _resolve_profile(
    difficulty: str,
    image: np.ndarray,
    *,
    store: RealtimeProfileStore,
) -> tuple[str, float]:
    options = store.runtime_options()
    signature = EnvironmentSignature(
        frame_resolution(image),
        MEDLEY_DPI,
        MEDLEY_GAME_FPS,
        MEDLEY_RENDER_QUALITY,
        1.0,
        engine=engine_from_native_flag(
            options.get("native_realtime_enabled", False)
        ),
    )
    settings = store.resolve_latest_for_environment(
        difficulty=difficulty,
        current_signature=signature,
    )
    return settings.profile_path.name, settings.note_speed


def build_medley_song(
    *,
    index: int,
    requested_difficulty: str,
    difficulty: str,
    identity,
    level: int | None,
    title_reading: TitleReading | None,
    image: np.ndarray,
    repository: LocalChartRepository,
    profile_store: RealtimeProfileStore,
    allow_deferred_identity: bool = False,
    title_source: str = "unknown",
) -> MedleySong:
    if identity.song_id == UNKNOWN_SONG_ID and not allow_deferred_identity:
        raise RuntimeError(f"第{index}首封面无法识别")
    if level is None:
        raise RuntimeError(f"第{index}首等级无法识别")
    song_id, method, bestdori_id, title, expected_notes = (
        _canonical_song_identity(
            identity,
            difficulty,
            level,
            title_reading,
            repository=repository,
        )
    )
    profile, note_speed = _resolve_profile(
        difficulty,
        image,
        store=profile_store,
    )
    return MedleySong(
        index=index,
        requested_difficulty=requested_difficulty,
        difficulty=difficulty,
        song_id=song_id,
        song_id_method=method,
        bestdori_song_id=bestdori_id,
        title=title,
        title_confidence=(
            title_reading.confidence if title_reading is not None else 0.0
        ),
        level=level,
        expected_notes=expected_notes,
        profile=profile,
        note_speed=note_speed,
        observed_title=(
            title_reading.text if title_reading is not None else None
        ),
        title_source=(
            title_source if title_reading is not None else "deferred-to-final-cover"
        ),
    )


def build_medley_slot(
    *,
    index: int,
    requested_difficulty: str,
    difficulty: str,
    image: np.ndarray,
    profile_store: RealtimeProfileStore,
) -> MedleySong:
    """建立只含难度与 Profile 的自由巡演槽位，身份留待每曲准备页。"""
    profile, note_speed = _resolve_profile(
        difficulty,
        image,
        store=profile_store,
    )
    return MedleySong(
        index=index,
        requested_difficulty=requested_difficulty,
        difficulty=difficulty,
        song_id=UNKNOWN_SONG_ID,
        song_id_method="unknown",
        bestdori_song_id=None,
        title="",
        title_confidence=0.0,
        level=0,
        expected_notes=None,
        profile=profile,
        note_speed=note_speed,
        observed_title=None,
        title_source="deferred-to-preparation",
    )


def read_task_tour_songs(
    image: np.ndarray,
    *,
    repository: LocalChartRepository | None = None,
    profile_store: RealtimeProfileStore | None = None,
) -> tuple[MedleySong, ...]:
    """从课题巡演总览只读三首预设歌曲，不点击难度。"""
    repository = repository or LocalChartRepository(
        PROJECT_ROOT / "resource" / "charts"
    )
    profile_store = profile_store or RealtimeProfileStore(
        PROJECT_ROOT / "profiles"
    )
    songs: list[MedleySong] = []
    for index, layout in enumerate(TASK_SLOT_LAYOUTS, start=1):
        difficulty = selected_difficulty(image, layout["targets"])
        if difficulty is None:
            raise RuntimeError(f"第{index}首预设难度无法识别")
        cover = _crop(image, layout["cover"])
        identity = fingerprint_jacket(cover)
        level = read_song_level(image, layout["level"])
        title = recognize_song_title(image, layout["title"])
        songs.append(build_medley_song(
            index=index,
            requested_difficulty=difficulty,
            difficulty=difficulty,
            identity=identity,
            level=level,
            title_reading=title,
            image=image,
            repository=repository,
            profile_store=profile_store,
        ))
    return tuple(songs)


def song_identity_matches(expected: MedleySong, observed: MedleySong) -> bool:
    if expected.difficulty != observed.difficulty or expected.level != observed.level:
        return False
    if (
        expected.bestdori_song_id is not None
        and observed.bestdori_song_id is not None
    ):
        return expected.bestdori_song_id == observed.bestdori_song_id
    cover_matches = same_song(
        expected.song_id,
        observed.song_id,
        max_distance=LOOSE_SAME_SONG_DISTANCE,
    )
    if not cover_matches:
        return False
    if not expected.title or not observed.title:
        # 标题待后补时，只把封面、难度和等级用于同一页面链路的续跑
        # 核对；真正开演前仍会要求准备页标题或最终封面页标题。
        return True
    return title_similarity(expected.title, observed.title) >= 0.65


def song_identity_confirmed(song: MedleySong) -> bool:
    return bool(
        song.song_id != UNKNOWN_SONG_ID
        and song.bestdori_song_id is not None
        and song.level > 0
    )


def lineup_matches(
    expected: tuple[MedleySong, ...],
    observed: tuple[MedleySong, ...],
) -> bool:
    return bool(
        len(expected) == len(observed) == 3
        and all(
            song_identity_matches(before, after)
            for before, after in zip(expected, observed, strict=True)
        )
    )


class MedleySessionStore:
    SCHEMA_VERSION = 1

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    @staticmethod
    def _clean(session: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in session.items() if key != "_path"}

    def _save(self, session: dict[str, Any]) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        session["updated_at"] = datetime.now().isoformat(timespec="milliseconds")
        path = Path(session["_path"])
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self._clean(session), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
        return session

    def _load(self, path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            return None
        value["_path"] = path
        return value

    def latest(self, tour_type: str) -> dict[str, Any] | None:
        if not self.root.exists():
            return None
        candidates = [
            value
            for value in (self._load(path) for path in self.root.glob("*.json"))
            if value is not None
            and value.get("tour_type") == tour_type
            and value.get("status") in {"active", "paused"}
        ]
        return max(
            candidates,
            key=lambda value: str(value.get("updated_at", "")),
            default=None,
        )

    def start(
        self,
        *,
        settings: dict[str, object],
        songs: tuple[MedleySong, ...],
        outer_task_id: int,
        speed_verified: bool = False,
        note_speed: float | None = None,
        completed_before_round: int = 0,
    ) -> dict[str, Any]:
        previous = self.latest(str(settings["tour_type"]))
        if previous is not None:
            previous["status"] = "superseded"
            previous["terminal_reason"] = "new_medley_started"
            self._save(previous)
        session_id = uuid4().hex
        created_at = datetime.now().isoformat(timespec="milliseconds")
        session: dict[str, Any] = {
            "schema_version": self.SCHEMA_VERSION,
            "session_id": session_id,
            "created_at": created_at,
            "updated_at": created_at,
            "status": "active",
            "terminal_reason": None,
            "outer_task_id": int(outer_task_id),
            "tour_type": str(settings["tour_type"]),
            "song_mode": str(settings["song_mode"]),
            "requested_difficulty": str(settings["difficulty"]),
            "target_count": int(settings.get("count", 3)),
            "round_index": int(completed_before_round) // 3 + 1,
            "completed_before_round": int(completed_before_round),
            "completed_total": int(completed_before_round),
            "stage": "ready-1",
            "completed_songs": 0,
            "results_completed": 0,
            "speed_verified": bool(speed_verified),
            "note_speed": (
                None if note_speed is None else round(float(note_speed), 2)
            ),
            "songs": [asdict(song) for song in songs],
        }
        path = self.root / f"medley-{session_id}.json"
        session["_path"] = path
        return self._save(session)

    def update(self, session: dict[str, Any], **changes: Any) -> dict[str, Any]:
        session.update(changes)
        return self._save(session)

    def update_song(
        self,
        session: dict[str, Any],
        song: MedleySong,
    ) -> dict[str, Any]:
        songs = list(session["songs"])
        songs[song.index - 1] = asdict(song)
        session["songs"] = songs
        return self._save(session)


def _same_speed(songs: tuple[MedleySong, ...]) -> float:
    speeds = {round(song.note_speed, 2) for song in songs}
    if len(speeds) != 1:
        detail = "、".join(
            f"第{song.index}首 {song.difficulty}={song.note_speed:.2f}"
            for song in songs
        )
        raise RuntimeError(
            "课题巡演三首 Profile 的流速不一致，禁止在组曲中途修改设置："
            + detail
        )
    return next(iter(speeds))


def session_matches_settings(
    session: dict[str, Any],
    settings: dict[str, object],
) -> bool:
    """只让当前任务选项消费兼容的未完成会话。"""
    if session.get("tour_type") != settings.get("tour_type"):
        return False
    if int(session.get("target_count", 3)) != int(settings.get("count", 3)):
        return False
    if settings.get("tour_type") == "task":
        return True
    return bool(
        session.get("song_mode") == settings.get("song_mode")
        and session.get("requested_difficulty") == settings.get("difficulty")
    )


def outer_task_id_from_argv(argv: CustomAction.RunArg) -> int:
    """只使用 Maa 回调携带的 task_id，禁止回退到临时 RemoteTasker 指针。"""
    detail = getattr(argv, "task_detail", None)
    value = getattr(detail, "task_id", None)
    if value is None or isinstance(value, bool):
        raise RuntimeError("组曲任务缺少稳定的 task_id，禁止复用旧会话")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("组曲任务 task_id 无效，禁止复用旧会话") from exc


def session_matches_outer_task(
    session: dict[str, Any],
    outer_task_id: int,
) -> bool:
    """旧 schema 没有任务身份时一律不续跑，避免新任务继承旧进度。"""
    value = session.get("outer_task_id")
    if value is None or isinstance(value, bool):
        return False
    try:
        return int(value) == int(outer_task_id)
    except (TypeError, ValueError):
        return False


def _template_score(image: np.ndarray, path: Path) -> tuple[float, tuple[int, int]]:
    template = imread_unicode(path)
    if template is None or any(
        left < right
        for left, right in zip(image.shape[:2], template.shape[:2])
    ):
        return 0.0, (0, 0)
    matched = cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, point = cv2.minMaxLoc(matched)
    return float(score), (int(point[0]), int(point[1]))


def judgement_details_visible(image: np.ndarray) -> bool:
    return _template_score(image, RESULT_TEMPLATE)[0] >= 0.9


class MedleyFlow:
    def __init__(
        self,
        context: Context,
        argv: CustomAction.RunArg,
        settings: dict[str, object],
    ) -> None:
        self.context = context
        self.argv = argv
        self.settings = settings
        self.outer_task_id = outer_task_id_from_argv(argv)
        self.repository = LocalChartRepository(PROJECT_ROOT / "resource" / "charts")
        self.profile_store = RealtimeProfileStore(PROJECT_ROOT / "profiles")
        self.sessions = MedleySessionStore(PROJECT_ROOT / "profiles" / "medley-sessions")
        self.home_verified_speed: float | None = None
        self._progress_initialised = False
        self._next_round_completed = 0
        self._home_ready = False
        self._play_failure_retries = 0
        self._play_failure_retry_limit: int | None = None

    @property
    def controller(self):
        return self.context.tasker.controller

    @staticmethod
    def action_argv(params: dict[str, object], *, task_detail=None):
        return SimpleNamespace(
            custom_action_param=json.dumps(params, ensure_ascii=False),
            task_detail=task_detail,
            node_name="MedleyFlow",
        )

    def wait(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            if self.context.tasker.stopping:
                raise ScreenRefreshCancelled("task is stopping")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.05, remaining))

    def capture(self) -> np.ndarray:
        return capture_image(self.context)

    def click(self, point: tuple[int, int]) -> None:
        if self.context.tasker.stopping:
            raise ScreenRefreshCancelled("task is stopping")
        require_game_foreground(self.controller)
        self.controller.post_click(*point).wait()

    def back(self) -> None:
        if self.context.tasker.stopping:
            raise ScreenRefreshCancelled("task is stopping")
        require_game_foreground(self.controller)
        self.controller.post_click_key(4).wait()

    def progress(self, phase: str) -> bool:
        return TaskProgress().run(
            self.context,
            self.action_argv(
                {
                    "task_name": "MedleyLive",
                    "label": "组曲演奏",
                    "total": int(self.settings["count"]),
                    "phase": phase,
                },
                task_detail=getattr(self.argv, "task_detail", None),
            ),
        )

    def restore_progress(self, completed: int) -> bool:
        """重开失败组前精确回滚已报告的曲数，避免第 2/3 曲空血误计数。"""
        return TaskProgress().run(
            self.context,
            self.action_argv(
                {
                    "task_name": "MedleyLive",
                    "label": "组曲演奏",
                    "total": int(self.settings["count"]),
                    "phase": "restore",
                    "completed": int(completed),
                    "next_started": int(completed) < int(self.settings["count"]),
                },
                task_detail=getattr(self.argv, "task_detail", None),
            ),
        )

    def recover_home(self, *, result_navigation: bool = False) -> None:
        params = {
            "home_node": "MedleyHomeMarker",
            "modal_cancel_nodes": ["QuitConfirmCancel"],
            "click_nodes": [
                "AutoLiveLoginTap", "AutoLiveLoginNext", "AutoLiveCommonClose",
                "AutoLiveStorySkipConfirmLarge", "AutoLiveStorySkipConfirm",
                "AutoLiveStorySkip", "AutoLiveStoryMenu",
            ],
            "escape_interval_ms": 750,
            "escape_timeout_ms": 60000,
            "restart_limit": 1,
            "restart_wait_ms": 5000,
            "startup_grace_ms": 12000,
            "login_start_node": "AutoLiveLoginScreenMarker",
            "login_start_target": [640, 635],
            "login_tap_target": [640, 360],
            "login_marker_priority_attempts": 3,
            "escape_after_login_start": True,
            "package": GAME_PACKAGE,
        }
        if not result_navigation:
            # 组曲普通主页恢复也可能从空血后的第二层确认页开始；两层
            # 弹窗布局不同于单人通用节点，必须先左侧退出、后右侧确认。
            params.update({
                "live_failed_continue_node": "MedleyLiveFailedContinue",
                "live_failed_exit_node": "MedleyLiveFailedExit",
                "quit_confirm_exit_node": "MedleyQuitConfirmExit",
            })
        if result_navigation:
            params.update({
                "click_nodes": [],
                "back_only_click_nodes": list(STORY_NODES),
                "back_only": True,
                "back_acceleration_click_point": list(
                    RESULT_ANIMATION_SKIP_POINT
                ),
                "escape_interval_ms": 500,
            })
        if not CommonRecover().run(self.context, self.action_argv(params)):
            raise RuntimeError("组曲流程无法恢复主页")

    def accelerated_result_back(self, phase: str) -> None:
        def before_input() -> None:
            if self.context.tasker.stopping:
                raise ScreenRefreshCancelled("task is stopping")
            require_game_foreground(self.controller)

        accelerated_back(
            lambda: self.controller,
            before_input=before_input,
            phase=phase,
            log_prefix="MedleyResult",
        )

    def result_cadence_step(self, phase: str) -> str:
        """单步推进结算节拍，让组曲能在每次输入后检查 PGGBM。"""

        def before_input() -> None:
            if self.context.tasker.stopping:
                raise ScreenRefreshCancelled("task is stopping")
            require_game_foreground(self.controller)

        back_next = bool(getattr(self, "_result_back_next", False))
        self._result_back_next = advance_result_cadence(
            lambda: self.controller,
            back_next=back_next,
            before_input=before_input,
            phase=phase,
            log_prefix="MedleyResult",
        )
        return "BACK" if back_next else "最右下角"

    def speed_gate(self, difficulty: str) -> None:
        params = {
            "entry_mode": "home",
            "difficulty": difficulty,
            "require_profile": True,
            "dpi": MEDLEY_DPI,
            "game_fps": MEDLEY_GAME_FPS,
            "render_quality": MEDLEY_RENDER_QUALITY,
        }
        if not RealtimeGameSpeedSettingsGate().run(
            self.context,
            self.action_argv(params),
        ):
            raise RuntimeError(f"组曲 {difficulty} 流速检查失败")
        receipt = verified_settings(difficulty)
        self.home_verified_speed = (
            None if receipt is None else float(receipt.actual_note_speed)
        )

    def validate_home_speed(
        self,
        songs: tuple[MedleySong, ...],
        *,
        enabled: bool,
    ) -> float:
        speed = _same_speed(songs)
        if not enabled:
            return speed
        if self.home_verified_speed is None:
            raise RuntimeError("组曲缺少本任务主页流速读回")
        if round(self.home_verified_speed, 2) != round(speed, 2):
            raise RuntimeError(
                "主页实际复核流速与组曲三首 Profile 不一致："
                f"主页={self.home_verified_speed:.2f}，Profile={speed:.2f}"
            )
        return speed

    def restore_session_speed(
        self,
        session: dict[str, Any],
        song: MedleySong,
    ) -> None:
        if session.get("speed_verified") is not True:
            raise RuntimeError("组曲续跑会话缺少本轮主页流速凭据")
        try:
            speed = round(float(session["note_speed"]), 2)
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("组曲续跑会话的流速凭据无效") from exc
        if speed != round(song.note_speed, 2):
            raise RuntimeError(
                "组曲续跑会话流速与当前歌曲 Profile 不一致"
            )
        publish_verified_performance_settings(
            difficulty=song.difficulty,
            actual_note_speed=speed,
            expected_note_speed=speed,
            profile=song.profile,
        )
        activate_speed_settings_target(_speed_settings_target(note_speed=speed))
        log_task(
            "组曲演奏",
            "续跑",
            "INFO",
            f"已恢复本轮组曲流速凭据 {speed:.2f}，不会在中途打开设置页",
        )

    def navigate_to_tour(self) -> np.ndarray:
        self.click(HOME_LIVE_POINT)
        self.wait(1.0)
        params = {
            "expected": "巡回演出",
            "roi": [0, 100, 1280, 620],
            "click": True,
            "timeout_ms": 10000,
            "interval_ms": 500,
        }
        if not LiveSelectFind().run(self.context, self.action_argv(params)):
            raise RuntimeError("选择演出页面未找到“巡回演出”入口")
        self.wait(1.2)
        return self.capture()

    def choose_tour_type(self) -> np.ndarray:
        tour_type = str(self.settings["tour_type"])
        self.click(TOUR_TYPE_POINTS[tour_type])
        self.wait(1.2)
        return self.capture()

    def snapshot_free_song(
        self,
        index: int,
        _existing: tuple[MedleySong, ...],
    ) -> MedleySong:
        self.click(FREE_SLOT_POINTS[index - 1])
        self.wait(1.0)
        if self.settings["song_mode"] == "random":
            # 最新约定禁止在第 4 张图读取任何歌曲身份；随机模式只发送
            # 随机按钮，是否为哪首歌同样留到对应准备页确认。
            self.click(FREE_RANDOM_POINT)
            self.wait(0.5)
        requested = str(self.settings["difficulty"])
        params: dict[str, object] = {
            "difficulty": requested,
            "max_attempts": 3,
            "identity_read": False,
            "mode": "medley",
            "debug_recording": bool(self.settings["debug_recording"]),
        }
        if requested == "Special":
            params["fallback_difficulties"] = ["Expert"]
        if not RealtimeDifficultySelect().run(
            self.context,
            self.action_argv(params),
        ):
            raise RuntimeError(f"第{index}首难度选择失败")
        run = current_live_run()
        if run is None:
            raise RuntimeError(f"第{index}首选曲后缺少难度上下文")
        image = self.capture()
        song = build_medley_slot(
            index=index,
            requested_difficulty=str(run.requested_difficulty or requested),
            difficulty=str(run.difficulty),
            image=image,
            profile_store=self.profile_store,
        )
        print(
            "MedleySelection "
            f"song_index={index} requested={song.requested_difficulty} "
            f"effective={song.difficulty} identity=deferred-to-preparation",
            flush=True,
        )
        self.click(SONG_CONFIRM_POINT)
        self.wait(1.0)
        return song

    def select_free_songs(self) -> tuple[MedleySong, ...]:
        songs: tuple[MedleySong, ...] = ()
        for index in range(1, 4):
            songs = (*songs, self.snapshot_free_song(index, songs))
        return songs

    def read_preparation_song(
        self,
        index: int,
        image: np.ndarray,
        *,
        expected: MedleySong | None = None,
    ) -> MedleySong:
        difficulty = selected_difficulty(
            image,
            PREPARATION_DIFFICULTY_TARGETS[index - 1],
        )
        if difficulty is None:
            raise RuntimeError(f"第{index}曲准备页难度无法识别")
        if expected is not None and difficulty != expected.difficulty:
            raise RuntimeError(
                f"第{index}曲准备页实际难度 {difficulty} "
                f"与选曲记录 {expected.difficulty} 不一致"
            )
        identity = fingerprint_jacket(
            _crop(image, PREPARATION_COVER_ROIS[index - 1])
        )
        return build_medley_song(
            index=index,
            requested_difficulty=(
                expected.requested_difficulty if expected is not None else difficulty
            ),
            difficulty=difficulty,
            identity=identity,
            level=read_song_level(image, PREPARATION_LEVEL_ROI),
            title_reading=_recognize_tight_title(
                image,
                PREPARATION_TITLE_ROI,
            ),
            image=image,
            repository=self.repository,
            profile_store=self.profile_store,
            allow_deferred_identity=True,
            title_source="preparation",
        )

    def confirm_preparation(
        self,
        expected: MedleySong,
        image: np.ndarray,
    ) -> MedleySong:
        stage = detect_medley_stage(image)
        if stage != expected.index:
            raise RuntimeError(
                f"组曲阶段冲突：期望第{expected.index}曲，实际 {stage}"
            )
        observed = self.read_preparation_song(
            expected.index,
            image,
            expected=expected,
        )
        if (
            song_identity_confirmed(expected)
            and song_identity_confirmed(observed)
            and not song_identity_matches(expected, observed)
        ):
            raise RuntimeError(
                f"第{expected.index}曲准备页歌曲身份与选曲记录不一致"
            )
        if observed.title_confidence >= TRUSTED_TITLE_CONFIDENCE:
            title = observed.title
            title_confidence = observed.title_confidence
            observed_title = observed.observed_title
            title_source = "preparation"
        else:
            title = observed.title or expected.title
            title_confidence = expected.title_confidence
            observed_title = expected.observed_title
            title_source = (
                expected.title_source
                if title_confidence >= TRUSTED_TITLE_CONFIDENCE
                else "deferred-to-final-cover"
            )
        observed_identity_confirmed = song_identity_confirmed(observed)
        confirmed = replace(
            expected,
            song_id=(
                observed.song_id
                if observed_identity_confirmed else expected.song_id
            ),
            song_id_method=(
                observed.song_id_method
                if observed_identity_confirmed else expected.song_id_method
            ),
            bestdori_song_id=(
                observed.bestdori_song_id
                if observed.bestdori_song_id is not None
                else expected.bestdori_song_id
            ),
            title=title,
            title_confidence=title_confidence,
            observed_title=observed_title,
            title_source=title_source,
            level=observed.level,
            expected_notes=(
                observed.expected_notes
                if observed.expected_notes is not None
                else expected.expected_notes
            ),
        )
        print(
            "MedleyIdentity "
            f"song_index={expected.index} source={title_source} "
            f"bestdori_song_id={confirmed.bestdori_song_id} "
            f"title={confirmed.title!r} "
            f"observed_title={confirmed.observed_title!r} "
            f"title_confidence={confirmed.title_confidence:.3f}",
            flush=True,
        )
        return confirmed

    def complete_final_cover_identity(self, song: MedleySong) -> MedleySong:
        run = current_live_run()
        if run is None or not run.final_cover_confirmed:
            if song.title_confidence < TRUSTED_TITLE_CONFIDENCE:
                raise RuntimeError(
                    f"第{song.index}曲最终封面标题未写回组曲会话"
                )
            return song
        title = str(run.song_title or "").strip()
        confidence = float(run.song_title_confidence or 0.0)
        if song.title_confidence < TRUSTED_TITLE_CONFIDENCE and (
            not title or confidence < TRUSTED_TITLE_CONFIDENCE
        ):
            raise RuntimeError(
                f"第{song.index}曲最终封面没有可信的实际标题读数"
            )
        resolution = self.repository.resolve(
            run.song_id,
            song.difficulty,
            level=song.level,
            title=(title or song.observed_title or song.title or None),
            bestdori_song_id=song.bestdori_song_id,
        )
        if resolution.selection is None:
            raise RuntimeError(
                f"第{song.index}曲最终封面身份无法写回：{resolution.reason}"
            )
        selection = resolution.selection
        return replace(
            song,
            song_id=run.song_id,
            song_id_method=run.song_id_method,
            bestdori_song_id=selection.bestdori_song_id,
            title=selection.title,
            title_confidence=(confidence if title else song.title_confidence),
            observed_title=(title or song.observed_title),
            title_source=("final-cover" if title else song.title_source),
            expected_notes=selection.expected_notes,
        )

    def activate_song(self, song: MedleySong) -> None:
        reset_live_run(
            mode="medley",
            difficulty=song.difficulty,
            requested_difficulty=song.requested_difficulty,
            profile_name=song.profile,
            expected_note_speed=song.note_speed,
            debug_recording=bool(self.settings["debug_recording"]),
            prepared_for_play=True,
        )
        update_live_run(
            song_id=song.song_id,
            song_id_method=song.song_id_method,
            song_level=song.level,
            song_title=song.title,
            song_title_confidence=song.title_confidence,
        )

    def run_preflight(
        self,
        song: MedleySong,
        image: np.ndarray,
    ) -> tuple[MedleySong, bool]:
        song = self.confirm_preparation(song, image)
        self.activate_song(song)
        update_live_run(preparation_identity_image=image.copy())
        chart_resolution = self.repository.resolve(
            song.song_id,
            song.difficulty,
            level=song.level,
            title=(song.title or None),
            bestdori_song_id=song.bestdori_song_id,
        )
        native_prearm_deferred = chart_resolution.selection is None
        common = {
            "difficulty": song.requested_difficulty,
            "require_profile": True,
            "dpi": MEDLEY_DPI,
            "game_fps": MEDLEY_GAME_FPS,
            "render_quality": MEDLEY_RENDER_QUALITY,
            "note_speed": song.note_speed,
        }
        if not RealtimeProfileCheck().run(
            self.context,
            self.action_argv({**common, "difficulty": song.difficulty}),
        ):
            raise RuntimeError(f"第{song.index}曲 Profile 检查失败")
        if not RealtimeFormalPreflight().run(
            self.context,
            self.action_argv({}),
        ):
            raise RuntimeError(f"第{song.index}曲演出显示预检查失败")
        if not RealtimePerformanceSettingsGate().run(
            self.context,
            self.action_argv({
                **common,
                "confirm_preparation_identity": False,
                "defer_native_prearm": native_prearm_deferred,
            }),
        ):
            raise RuntimeError(f"第{song.index}曲流速凭据或 Native 预武装失败")
        return song, native_prearm_deferred

    def handle_pre_live_confirm(self) -> None:
        self.wait(1.0)
        image = self.capture()
        result = self.context.run_recognition(
            "MedleyPreLiveSettingsConfirm",
            image,
        )
        if result and result.hit and result.box:
            box = result.box
            self.click((box.x + box.w // 2, box.y + box.h // 2))
            self.wait(0.5)

    def play_song(
        self,
        session: dict[str, Any],
        song: MedleySong,
        image: np.ndarray,
    ) -> tuple[dict[str, Any], MedleySong]:
        song, native_prearm_deferred = self.run_preflight(song, image)
        run = current_live_run()
        if run is None:
            raise RuntimeError("组曲演奏缺少本局上下文")
        report_path = (
            f"screencap/medley-{session['session_id']}-song{song.index}.json"
        )
        song = MedleySong(**{**asdict(song), "report_path": report_path})
        session = self.sessions.update_song(session, song)
        session = self.sessions.update(
            session,
            stage=f"playing-{song.index}",
            status="active",
            terminal_reason=None,
        )
        self.click(MEDLEY_START_POINT)
        self.handle_pre_live_confirm()
        play_params = {
            "difficulty": song.requested_difficulty,
            "require_profile": True,
            "settings_gate_required": True,
            "debug_recording": bool(self.settings["debug_recording"]),
            "diagnostic_trace": bool(self.settings["diagnostic_trace"]),
            "duration_seconds": 600,
            "startup_timeout_seconds": 60,
            "dpi": MEDLEY_DPI,
            "game_fps": MEDLEY_GAME_FPS,
            "render_quality": MEDLEY_RENDER_QUALITY,
            "wait_for_completion": True,
            "completion_missing_frames": 120,
            "require_completion": True,
            "save_result_frame": True,
            "defer_result_collection": True,
            "deferred_result_report": report_path,
            "run_mode": "medley",
            "confirm_final_cover": True,
            "native_prearm_deferred": native_prearm_deferred,
            "require_final_cover_title": (
                song.title_confidence < TRUSTED_TITLE_CONFIDENCE
            ),
        }
        if not RealtimeProfilePlay().run(
            self.context,
            self.action_argv(play_params),
        ):
            if self.context.tasker.stopping:
                raise ScreenRefreshCancelled("task is stopping")
            failure = read_medley_play_failure(
                report_path,
                latest_failure_reason(),
                run_id=run.run_id,
            )
            raise MedleyPlayFailure(
                song_index=song.index,
                report_path=failure.report_path,
                reason=failure.reason,
                result_status=failure.result_status,
                retryable=failure.retryable,
            )
        if self.context.tasker.stopping:
            raise ScreenRefreshCancelled("task is stopping")
        song = self.complete_final_cover_identity(song)
        session = self.sessions.update_song(session, song)
        session = self.sessions.update(
            session,
            completed_songs=song.index,
            completed_total=(
                int(session.get("completed_before_round", 0)) + song.index
            ),
            stage=(
                f"ready-{song.index + 1}"
                if song.index < 3 else "results"
            ),
        )
        return session, song

    def dismiss_reward(self, image: np.ndarray) -> bool:
        for template_path in (*ESC_ONLY_REWARD_TEMPLATES, *REWARD_TEMPLATES):
            score, _point = _template_score(image, template_path)
            if score < 0.9:
                continue
            # 曲间弹窗也服从统一结算节拍；安全像素只加速动画，
            # 页面推进始终由 Android BACK 完成。
            self.accelerated_result_back("between-songs-reward")
            self.wait(0.6)
            log_task(
                "组曲演奏",
                "曲间弹窗",
                "INFO",
                "已用最右下角→BACK→最右下角关闭曲间弹窗"
                f"（score={score:.3f}）",
            )
            return True
        return False

    def capture_after_reward_overlays(self) -> np.ndarray:
        for _attempt in range(3):
            image = self.capture()
            if not self.dismiss_reward(image):
                return image
        raise RuntimeError("组曲曲间奖励弹窗连续三次关闭后仍然存在")

    def wait_for_stage(self, index: int, timeout_seconds: float = 90.0) -> np.ndarray:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            image = self.capture()
            if self.dismiss_reward(image):
                continue
            stage = detect_medley_stage(image)
            if stage == index:
                return image
            self.wait(0.35)
        raise RuntimeError(f"第{index}曲完成后未出现下一曲准备页")

    def result_header_matches(self, song: MedleySong, image: np.ndarray) -> bool:
        title = recognize_song_title(image, RESULT_TITLE_ROI)
        badge = recognize_song_title(image, RESULT_DIFFICULTY_ROI)
        level = read_song_level(image, RESULT_LEVEL_ROI)
        return bool(
            title is not None
            and badge is not None
            and level == song.level
            and badge.text.strip().casefold() == song.difficulty.casefold()
            and title_similarity(title.text, song.title) >= 0.65
        )

    def parse_stable_result(
        self,
        song: MedleySong,
        first_image: np.ndarray,
        *,
        timeout_seconds: float = 20.0,
    ) -> tuple[LiveResult, np.ndarray]:
        parser = ResultParser()
        candidate: LiveResult | None = None
        candidate_at = 0.0
        header_mismatches = 0
        image = first_image
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not self.result_header_matches(song, image):
                header_mismatches += 1
                if header_mismatches >= 3:
                    raise RuntimeError(
                        f"第{song.index}张 PGGBM 的标题、难度或等级与选曲不一致"
                    )
                self.wait(0.35)
                image = self.capture()
                continue
            header_mismatches = 0
            try:
                result = parser.parse(image)
                if song.expected_notes is not None and result.total != song.expected_notes:
                    result = parser.resolve_expected_total(
                        image,
                        expected_notes=song.expected_notes,
                        fallback=result,
                    )
            except ValueError:
                result = None
            now = time.monotonic()
            if result is not None:
                counts = (
                    result.perfect, result.great, result.good, result.bad,
                    result.miss, result.fast, result.slow,
                )
                if (
                    candidate is not None
                    and now - candidate_at >= 1.0
                    and counts == (
                        candidate.perfect, candidate.great, candidate.good,
                        candidate.bad, candidate.miss, candidate.fast,
                        candidate.slow,
                    )
                ):
                    return result, image
                candidate = result
                candidate_at = now
            self.wait(0.5)
            image = self.capture()
        raise RuntimeError(f"第{song.index}张 PGGBM 判定数字未稳定")

    def story_handled(self, image: np.ndarray) -> bool:
        def recognise(frame, node):
            result = self.context.run_recognition(node, frame)
            return result.box if result and result.hit else None

        return handle_story_page(
            image,
            recognise=recognise,
            click=self.click,
            stopping=lambda: self.context.tasker.stopping,
        )

    def dismiss_quit_confirm(self, image: np.ndarray) -> bool:
        result = self.context.run_recognition("QuitConfirmCancel", image)
        if not result or not result.hit:
            return False
        box = result.box
        self.click((int(box.x + box.w // 2), int(box.y + box.h // 2)))
        log_task(
            "组曲演奏",
            "结算",
            "INFO",
            "检测到主页退出确认框，已取消并停止发送返回键",
        )
        return True

    def advance_page(self, _before: np.ndarray) -> bool:
        page_name = "PGGBM"
        back_attempts = 0
        step_index = 0
        while back_attempts < RESULT_NAVIGATION_MAX_CYCLES:
            step_index += 1
            action = self.result_cadence_step(
                f"{page_name}-step-{step_index}"
            )
            if action == "BACK":
                back_attempts += 1
            log_task(
                "组曲演奏",
                "结算兜底",
                "INFO",
                f"{page_name}页按统一节拍执行{action}，"
                f"BACK {back_attempts}/{RESULT_NAVIGATION_MAX_CYCLES}",
            )
            self.wait(0.15 if action == "BACK" else 0.85)
            if not judgement_details_visible(self.capture()):
                return True

        log_task(
            "组曲演奏",
            "结算兜底",
            "WARN",
            f"{page_name}页面连续 {RESULT_NAVIGATION_MAX_CYCLES} 轮 "
            "最右下角/BACK加速推进后仍未离开",
        )
        return False

    def home_or_tour_select(self, image: np.ndarray) -> bool:
        result = self.context.run_recognition("MedleyHomeMarker", image)
        if result and result.hit:
            return True
        return _find_tour_live_card(image) is not None

    @staticmethod
    def pending_report_ready(song: MedleySong) -> bool:
        if not song.report_path:
            return False
        path = PROJECT_ROOT / song.report_path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return bool(
            isinstance(payload, dict)
            and payload.get("result_status") == "medley_result_pending"
            and payload.get("completed") is True
        )

    def reconcile_stage(
        self,
        session: dict[str, Any],
        songs: tuple[MedleySong, ...],
        stage: int,
    ) -> dict[str, Any]:
        completed = int(session.get("completed_songs", 0))
        expected = min(3, completed + 1)
        if stage == expected:
            return session
        previous = stage - 1
        if (
            stage == expected + 1
            and str(session.get("stage")) == f"playing-{previous}"
            and self.pending_report_ready(songs[previous - 1])
        ):
            log_task(
                "组曲演奏",
                "续跑",
                "INFO",
                f"第{previous}曲已有完整演奏报告，恢复到第{stage}曲",
            )
            return self.sessions.update(
                session,
                completed_songs=previous,
                stage=f"ready-{stage}",
            )
        raise RuntimeError(
            f"组曲续跑阶段冲突：会话期望第{expected}曲，"
            f"游戏停在第{stage}曲"
        )

    def collect_results(
        self,
        session: dict[str, Any],
        songs: tuple[MedleySong, ...],
    ) -> dict[str, Any]:
        deadline = time.monotonic() + 240.0
        result_index = int(session.get("results_completed", 0))
        if result_index >= 3:
            return session
        self._result_back_next = False
        last_image: np.ndarray | None = None
        unknown_back_attempts = 0
        cadence_steps = 0
        failure_reason: str | None = None
        while time.monotonic() < deadline:
            image = self.capture()
            last_image = image
            if self.dismiss_quit_confirm(image):
                unknown_back_attempts = 0
                cadence_steps = 0
                self.wait(0.5)
                continue
            if self.home_or_tour_select(image):
                raise MedleyResultsLeftBeforeCollection(result_index)
            if judgement_details_visible(image):
                unknown_back_attempts = 0
                cadence_steps = 0
                if (
                    result_index > 0
                    and not song_identity_matches(
                        songs[result_index - 1],
                        songs[result_index],
                    )
                    and self.result_header_matches(
                        songs[result_index - 1],
                        image,
                    )
                ):
                    log_task(
                        "组曲演奏",
                        "结算",
                        "INFO",
                        f"第{result_index}首 PGGBM 已保存，继续推进当前页面",
                    )
                    if self.advance_page(image) is False:
                        failure_reason = (
                            f"第{result_index}张 PGGBM 页面连续 "
                            f"{RESULT_NAVIGATION_MAX_CYCLES} 轮 "
                            "最右下角/BACK加速推进后仍未离开"
                        )
                        break
                    continue
                song = songs[result_index]
                result, stable_image = self.parse_stable_result(song, image)
                if not song.report_path:
                    raise RuntimeError(f"第{song.index}曲缺少延迟结算报告路径")
                finalize_deferred_result(
                    song.report_path,
                    result,
                    result_image=stable_image,
                    save_screenshot=True,
                )
                result_index += 1
                session = self.sessions.update(
                    session,
                    results_completed=result_index,
                    stage=(
                        f"result-{result_index + 1}"
                        if result_index < 3 else "post-results"
                    ),
                )
                log_task(
                    "组曲演奏",
                    "结算",
                    "INFO",
                    f"已读取并保存第{song.index}首 PGGBM 判定",
                )
                if result_index >= 3:
                    # 第三张保存后交给同一结算节拍恢复主页；CommonRecover
                    # 只检查主页与最终剧情，不识别中间结算页面。
                    return session
                if self.advance_page(stable_image) is False:
                    failure_reason = (
                        f"第{result_index}张 PGGBM 页面连续 "
                        f"{RESULT_NAVIGATION_MAX_CYCLES} 轮 "
                        "最右下角/BACK加速推进后仍未离开"
                    )
                    break
                continue
            if self.story_handled(image):
                unknown_back_attempts = 0
                cadence_steps = 0
                self.wait(0.5)
                continue
            cadence_steps += 1
            action = self.result_cadence_step(
                f"awaiting-pggbm-{result_index + 1}-step-{cadence_steps}"
            )
            if action == "BACK":
                unknown_back_attempts += 1
            log_task(
                "组曲演奏",
                "结算兜底",
                "INFO",
                f"未识别到下一张 PGGBM，按统一节拍执行{action}，"
                f"BACK {unknown_back_attempts}/"
                f"{RESULT_NAVIGATION_MAX_CYCLES}",
            )
            self.wait(0.15 if action == "BACK" else 0.85)
            if unknown_back_attempts >= RESULT_NAVIGATION_MAX_CYCLES:
                failure_reason = (
                    "组曲结算连续 "
                    f"{unknown_back_attempts} 次 BACK 加速推进后"
                    "仍未识别下一张 PGGBM"
                )
                break
        if last_image is not None:
            evidence = PROJECT_ROOT / "screencap" / (
                "medley-result-timeout-"
                f"{session['session_id']}.png"
            )
            evidence.parent.mkdir(parents=True, exist_ok=True)
            imwrite_unicode(evidence, last_image)
        if failure_reason is None:
            failure_reason = (
                "组曲结算在限定时间内未完成，"
                f"已读取 {result_index}/3 张 PGGBM"
            )
        try:
            # 读取失败后仍用相同结算节拍有界恢复；无法到达主页时再重启。
            self.recover_home(result_navigation=True)
        except ScreenRefreshCancelled:
            raise
        except RuntimeError as exc:
            failure_reason = f"{failure_reason}；主页恢复失败：{exc}"
        raise RuntimeError(failure_reason)

    def initialise_progress(
        self,
        completed: int,
        *,
        next_started: bool,
    ) -> None:
        target = int(self.settings.get("count", 3))
        if completed > 0 and not self.progress("start"):
            raise RuntimeError("组曲进度初始化失败")
        for index in range(completed):
            if not self.progress("completed"):
                raise RuntimeError("组曲续跑进度恢复失败")
            if index + 1 < completed and not self.progress("start"):
                raise RuntimeError("组曲续跑进度恢复失败")
        if next_started and completed < target and not self.progress("start"):
            raise RuntimeError("组曲当前歌曲进度恢复失败")

    def ensure_progress(self, completed: int, *, next_started: bool) -> None:
        if getattr(self, "_progress_initialised", False):
            return
        self.initialise_progress(completed, next_started=next_started)
        self._progress_initialised = True

    def finish_round(self, session: dict[str, Any]) -> bool:
        completed_total = int(session.get("completed_before_round", 0)) + 3
        session = self.sessions.update(
            session,
            status="completed",
            stage="completed",
            completed_songs=3,
            completed_total=completed_total,
            terminal_reason=None,
        )
        # 同一组的预算跨重开保留；只有三首均完成且结算已开始时才清零。
        self._play_failure_retries = 0
        target = int(self.settings.get("count", 3))
        self.recover_home(result_navigation=True)
        if completed_total >= target:
            return True
        self._next_round_completed = completed_total
        self._home_ready = True
        if not self.progress("start"):
            raise RuntimeError(f"第{completed_total + 1}曲进度报告失败")
        return self.run()

    def retry_failed_round(
        self,
        session: dict[str, Any],
        failure: MedleyPlayFailure,
    ) -> bool:
        """将失败曲所在整组作废后回主页；次数只在完整三曲结算后推进。"""
        if self.context.tasker.stopping:
            raise ScreenRefreshCancelled("task is stopping")
        completed_before = int(session.get("completed_before_round", 0))
        if not self.restore_progress(completed_before):
            reason = (
                "play_failure_retry_progress_restore_failed: "
                f"completed_before_round={completed_before}"
            )
            self.sessions.update(
                session,
                status="paused",
                terminal_reason=reason,
            )
            raise RuntimeError(reason)
        if not failure.retryable:
            raise RuntimeError(
                f"第{failure.song_index}曲失败不可重试：{failure.reason}"
            )
        retry_limit = int(getattr(self, "_play_failure_retry_limit", 0) or 0)
        retries = int(getattr(self, "_play_failure_retries", 0))
        if retries >= retry_limit:
            raise RuntimeError(
                f"第{failure.song_index}曲失败且重试次数已耗尽"
                f"（{retries}/{retry_limit}）：{failure.reason}"
            )
        self._play_failure_retries = retries + 1
        discard_prearmed_backend("medley-play-failure-retry")
        try:
            append_current_run_event(
                PROJECT_ROOT,
                "medley-play-failure",
                "retry",
                details={
                    "song_index": failure.song_index,
                    "reason": failure.reason,
                    "result_status": failure.result_status,
                    "retry": self._play_failure_retries,
                    "retry_limit": retry_limit,
                },
            )
        except Exception as exc:  # noqa: BLE001 - 诊断写入不能阻断失败恢复
            log_task(
                "组曲演奏",
                "重试",
                "WARNING",
                "无法写入失败重试生命周期事件："
                f"{type(exc).__name__}: {exc}",
            )
        log_task(
            "组曲演奏",
            "重试",
            "WARNING",
            f"第{failure.song_index}曲失败：{failure.reason}；"
            f"废弃本组三曲并从第{int(session.get('completed_before_round', 0)) + 1}曲"
            f"重开（{self._play_failure_retries}/{retry_limit}）",
        )
        try:
            # CommonRecover 的 LiveFailed 契约只点左侧“退出”，不点击星石继续。
            self.recover_home()
        except Exception as exc:
            reason = (
                "play_failure_retry_recovery_failed: "
                f"{failure.reason}; {type(exc).__name__}: {exc}"
            )
            self.sessions.update(
                session,
                status="paused",
                terminal_reason=reason,
            )
            raise RuntimeError(reason) from exc
        self.sessions.update(
            session,
            status="superseded",
            terminal_reason=f"play_failure_retry: {failure.reason}",
        )
        self._next_round_completed = completed_before
        self._home_ready = True
        # 不重复上报 start：TaskProgress 仍停留在失败曲（例如 7/9），
        # 下一组首曲成功后才把已完成数推进到 7/9。
        return self.run()

    def run(self) -> bool:
        tour_type = str(self.settings["tour_type"])
        active = self.sessions.latest(tour_type)
        if active is not None and not session_matches_outer_task(
            active,
            self.outer_task_id,
        ):
            self.sessions.update(
                active,
                status="superseded",
                terminal_reason="new_task_started",
            )
            log_task(
                "组曲演奏",
                "续跑",
                "INFO",
                "未完成会话属于上一次任务，已作废并从第一曲新建组曲",
            )
            active = None
        elif active is not None and not session_matches_settings(
            active,
            self.settings,
        ):
            log_task(
                "组曲演奏",
                "续跑",
                "INFO",
                "未完成会话与本次自由巡演选项不一致，本次不复用",
            )
            active = None
        runtime_options = self.profile_store.runtime_options()
        if getattr(self, "_play_failure_retry_limit", None) is None:
            self._play_failure_retry_limit = int(
                runtime_options.get("play_failure_retry_count", 1)
            )
        session = None
        songs: tuple[MedleySong, ...]
        start_index = 1

        # 先检查当前页，避免 Pipeline 的主页恢复把第 2/3 曲准备页退出掉。
        image = self.capture_after_reward_overlays()
        stage = detect_medley_stage(image)
        if stage is not None:
            if active is None:
                raise RuntimeError(
                    f"游戏已有进行中的第{stage}曲，但本地没有匹配的组曲会话"
                )
            songs = tuple(
                MedleySong.from_mapping(value) for value in active["songs"]
            )
            _same_speed(songs)
            active = self.reconcile_stage(active, songs, stage)
            self.confirm_preparation(songs[stage - 1], image)
            if bool(runtime_options["note_speed_settings_enabled"]):
                self.restore_session_speed(active, songs[stage - 1])
            session = self.sessions.update(
                active,
                status="active",
                terminal_reason=None,
                stage=f"ready-{stage}",
            )
            start_index = stage
            log_task("组曲演奏", "续跑", "INFO", f"已确认并续跑第{stage}曲")
        else:
            if (
                active is not None
                and int(active.get("completed_songs", 0)) == 3
                and str(active.get("stage", ""))
                in {"results", "result-1", "result-2", "result-3", "post-results"}
            ):
                if self.home_or_tour_select(image):
                    results_completed = int(active.get("results_completed", 0))
                    if results_completed >= 3:
                        completed_before = int(
                            active.get("completed_before_round", 0)
                        )
                        log_task(
                            "组曲演奏",
                            "续跑",
                            "INFO",
                            "三张 PGGBM 已全部保存，"
                            "正在补记上一组完成状态",
                        )
                        self.ensure_progress(
                            completed_before + 3,
                            next_started=False,
                        )
                        return self.finish_round(active)
                    log_task(
                        "组曲演奏",
                        "续跑",
                        "WARNING",
                        "上一组结算已被人工退出，"
                        f"仅保存 {results_completed}/3 张 PGGBM；"
                        "保留旧会话记录并从第一曲开始新一组",
                    )
                    self.sessions.update(
                        active,
                        status="superseded",
                        terminal_reason="result_pages_left_before_collection",
                    )
                    active = None
                else:
                    songs = tuple(
                        MedleySong.from_mapping(value) for value in active["songs"]
                    )
                    _same_speed(songs)
                    session = active
                    try:
                        session = self.collect_results(session, songs)
                    except MedleyResultsLeftBeforeCollection as exc:
                        log_task(
                            "组曲演奏",
                            "续跑",
                            "WARNING",
                            "当前页面不是可继续读取的旧组曲结算，"
                            "已按安全节拍返回主页；"
                            f"仅保存 {exc.results_completed}/3 张 PGGBM，"
                            "从第一曲开始新一组",
                        )
                        self.sessions.update(
                            active,
                            status="superseded",
                            terminal_reason="result_pages_left_before_collection",
                        )
                        active = None
                        session = None
                        self._home_ready = True
                    else:
                        completed_before = int(
                            active.get("completed_before_round", 0)
                        )
                        self.ensure_progress(
                            completed_before + 3,
                            next_started=False,
                        )
                        return self.finish_round(session)

            if getattr(self, "_home_ready", False):
                self._home_ready = False
            else:
                self.recover_home()
            if active is not None:
                active_songs = tuple(
                    MedleySong.from_mapping(value) for value in active["songs"]
                )
                _same_speed(active_songs)
                current_index = min(
                    3,
                    int(active.get("completed_songs", 0)) + 1,
                )
                self.speed_gate(active_songs[current_index - 1].difficulty)
            elif tour_type == "free":
                self.speed_gate(str(self.settings["difficulty"]))

            image = self.navigate_to_tour()
            stage = detect_medley_stage(image)
            if stage is not None:
                if active is None:
                    raise RuntimeError(
                        f"游戏已有进行中的第{stage}曲，但本地没有匹配的组曲会话"
                    )
                songs = tuple(
                    MedleySong.from_mapping(value) for value in active["songs"]
                )
                active = self.reconcile_stage(active, songs, stage)
                self.confirm_preparation(songs[stage - 1], image)
                session = self.sessions.update(
                    active,
                    status="active",
                    terminal_reason=None,
                    stage=f"ready-{stage}",
                )
                start_index = stage
                log_task(
                    "组曲演奏",
                    "续跑",
                    "INFO",
                    f"已从主页入口确认并续跑第{stage}曲",
                )
            elif active is not None:
                raise RuntimeError(
                    "存在未完成组曲会话，但重新进入巡回演出后未找到匹配的续跑页面"
                )
            else:
                image = self.choose_tour_type()
                if tour_type == "task":
                    songs = read_task_tour_songs(
                        image,
                        repository=self.repository,
                        profile_store=self.profile_store,
                    )
                    _same_speed(songs)
                    if bool(runtime_options["note_speed_settings_enabled"]):
                        self.recover_home()
                        self.speed_gate(songs[0].difficulty)
                        image = self.navigate_to_tour()
                        if detect_medley_stage(image) is not None:
                            raise RuntimeError("课题巡演复核前意外进入了进行中组曲")
                        image = self.choose_tour_type()
                        verified = read_task_tour_songs(
                            image,
                            repository=self.repository,
                            profile_store=self.profile_store,
                        )
                        if not lineup_matches(songs, verified):
                            raise RuntimeError("主页流速检查后课题巡演阵容发生变化")
                        songs = verified
                    else:
                        # 关闭开关时只清理本任务旧凭据并记录跳过，不进入设置页。
                        self.speed_gate(songs[0].difficulty)
                    group_speed = self.validate_home_speed(
                        songs,
                        enabled=bool(
                            runtime_options["note_speed_settings_enabled"]
                        ),
                    )
                    self.click(OVERVIEW_NEXT_POINT)
                    self.wait(1.2)
                else:
                    songs = self.select_free_songs()
                    group_speed = self.validate_home_speed(
                        songs,
                        enabled=bool(
                            runtime_options["note_speed_settings_enabled"]
                        ),
                    )
                    self.click(OVERVIEW_NEXT_POINT)
                    self.wait(1.2)
                session = self.sessions.start(
                    settings=self.settings,
                    songs=songs,
                    outer_task_id=self.outer_task_id,
                    speed_verified=bool(
                        runtime_options["note_speed_settings_enabled"]
                    ),
                    note_speed=group_speed,
                    completed_before_round=int(
                        getattr(self, "_next_round_completed", 0)
                    ),
                )
                image = self.capture()
                self.confirm_preparation(songs[0], image)

        assert session is not None
        completed_before = int(session.get("completed_before_round", 0))
        self.ensure_progress(
            completed_before + int(session.get("completed_songs", 0)),
            next_started=True,
        )
        try:
            for index in range(start_index, 4):
                song = songs[index - 1]
                if index != start_index:
                    image = self.wait_for_stage(index)
                    self.confirm_preparation(song, image)
                session, song = self.play_song(session, song, image)
                songs = tuple(
                    song if item.index == song.index else item
                    for item in songs
                )
                if not self.progress("completed"):
                    raise RuntimeError(f"第{index}曲完成后进度报告失败")
                if index < 3 and not self.progress("start"):
                    raise RuntimeError(f"第{index + 1}曲进度报告失败")
        except MedleyPlayFailure as failure:
            return self.retry_failed_round(session, failure)
        session = self.collect_results(session, songs)
        return self.finish_round(session)


@AgentServer.custom_action("MedleyLiveConfigure")
class MedleyLiveConfigure(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            configure_medley_settings(
                parse_custom_action_params(argv.custom_action_param)
            )
            return True
        except Exception as exc:
            record_failure_reason(f"组曲任务配置失败：{type(exc).__name__}: {exc}")
            traceback.print_exc()
            return False


@AgentServer.custom_action("MedleyLiveFlow")
class MedleyLiveFlow(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        flow: MedleyFlow | None = None
        try:
            flow = MedleyFlow(context, argv, current_medley_settings())
            return flow.run()
        except ScreenRefreshCancelled:
            if flow is not None:
                session = flow.sessions.latest(
                    str(flow.settings["tour_type"])
                )
                if session is not None:
                    flow.sessions.update(
                        session,
                        status="paused",
                        terminal_reason="user_stopped",
                    )
            return True
        except Exception as exc:
            reason = f"组曲流程失败：{type(exc).__name__}: {exc}"
            record_failure_reason(reason)
            if flow is not None:
                session = flow.sessions.latest(
                    str(flow.settings["tour_type"])
                )
                if session is not None:
                    flow.sessions.update(
                        session,
                        status="paused",
                        terminal_reason=reason,
                    )
            traceback.print_exc()
            log_task("组曲演奏", "流程", "ERROR", reason)
            return False
