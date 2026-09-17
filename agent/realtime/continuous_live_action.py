from __future__ import annotations

import json
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import cv2

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    from ..common_recover import CommonRecover
    from ..foreground_guard import GAME_PACKAGE
    from ..task_reporting import record_failure_reason
except ImportError:
    from common_recover import CommonRecover
    from foreground_guard import GAME_PACKAGE
    from task_reporting import record_failure_reason

from .chart_repository import CatalogSongIdentity, LocalChartRepository
from .final_cover import FinalCoverConfirmation, FinalCoverResolution
from .live_session import reset_live_run, update_live_run
from .performance_settings_action import verified_settings
from .playfield_monitor import PlayfieldDetector
from .profile_action import PROJECT_ROOT
from .profile_play_action import RealtimeProfilePlay, resolve_profile_for_settings_gate
from .profile_store import RealtimeProfileStore
from .result_navigation import RESULT_ANIMATION_SKIP_POINT, STORY_NODES
from .song_identity import (
    UNKNOWN_SONG_ID,
    detect_full_badge,
    identify_final_song,
    same_song,
)
from .song_title_ocr import (
    FINAL_COVER_TITLE_ROI,
    recognize_song_title,
)
from .vision_io import imwrite_unicode


CONTINUOUS_SPEED_VERIFICATION_MAX_AGE_SECONDS = 15 * 60
CONTINUOUS_COVER_STABLE_FRAMES = 2
CONTINUOUS_DEFAULT_SETTINGS: dict[str, object] = {
    "difficulty": "Easy",
    "dpi": 240,
    "game_fps": 60,
    "render_quality": "standard",
    "debug_recording": False,
    "diagnostic_trace": True,
}
_CONTINUOUS_SETTINGS = dict(CONTINUOUS_DEFAULT_SETTINGS)
_CONTINUOUS_SETTINGS_LOCK = threading.Lock()


def parse_custom_action_params(raw: object) -> dict[str, object]:
    """把 MaaFramework 的空参数表示统一为空字典。"""
    decoded = json.loads(raw) if isinstance(raw, str) and raw else raw
    if decoded is None:
        return {}
    if not isinstance(decoded, dict):
        raise ValueError("一键实时演奏 Custom Action 参数必须是 JSON 对象")
    return dict(decoded)


def configure_continuous_settings(
    params: dict[str, object],
) -> dict[str, object]:
    """逐个合并一键演奏选项，避开 Custom Action 参数整块替换。"""
    with _CONTINUOUS_SETTINGS_LOCK:
        candidate = (
            dict(CONTINUOUS_DEFAULT_SETTINGS)
            if bool(params.get("reset", False))
            else dict(_CONTINUOUS_SETTINGS)
        )
        for key in CONTINUOUS_DEFAULT_SETTINGS:
            if key in params:
                candidate[key] = params[key]
        difficulty = str(candidate["difficulty"])
        if difficulty not in {"Easy", "Normal", "Hard", "Expert", "Special"}:
            raise ValueError(f"不支持的一键演奏难度：{difficulty}")
        candidate["difficulty"] = difficulty
        _CONTINUOUS_SETTINGS.clear()
        _CONTINUOUS_SETTINGS.update(candidate)
        return dict(_CONTINUOUS_SETTINGS)


def current_continuous_settings() -> dict[str, object]:
    with _CONTINUOUS_SETTINGS_LOCK:
        return dict(_CONTINUOUS_SETTINGS)


def require_recent_speed_settings(
    difficulty: str,
    *,
    enabled: bool = True,
):
    """为无导航的一键模式检查有时效的流速读回。

    一键模式不会主动打开游戏设置页：开关关闭时完全跳过检查，开启时只接受
    最近任务留下的限时流速读回结果，不能用 MFA 目标值冒充游戏实际状态。
    """
    if not enabled:
        print(
            "ContinuousRealtimeLive speed_check=skipped enabled=false",
            flush=True,
        )
        return None
    verified = verified_settings(
        difficulty,
        max_age_seconds=CONTINUOUS_SPEED_VERIFICATION_MAX_AGE_SECONDS,
    )
    if verified is None:
        raise RuntimeError(
            "一键实时演奏需要最近 15 分钟内完成一次游戏流速读回复核；"
            "不能用 MFA 目标配置冒充游戏实际状态"
        )
    return verified


class ListenerDiagnosticCapture:
    """有界保留监听失败现场，写盘只发生在任务停止或失败之后。"""

    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], float] = time.monotonic,
        playfield_detector: Callable[[object], bool] | None = None,
    ) -> None:
        self._root = root
        self._clock = clock
        self._playfield_detector = playfield_detector or PlayfieldDetector()
        self._latest = None
        self._saved = False
        self._last_report_at = float("-inf")
        self._recognition_samples: list[dict[str, object]] = []
        self._candidate_keys: set[tuple[object, ...]] = set()
        self._recognition_candidates: list[tuple[object, dict[str, object]]] = []

    def observe(self, image) -> None:
        # Maa 截图在本 Action 中按只读使用；仅保留最新引用可避免每 100ms
        # 复制整帧，候选画面也只在稳定门槛首次出现时做有界复制。
        self._latest = image

    def observe_recognition(
        self,
        image: object,
        state: dict[str, object],
    ) -> None:
        self.observe(image)
        snapshot = dict(state)
        now = float(self._clock())
        if now - self._last_report_at >= 2.0:
            self._last_report_at = now
            snapshot["playfield_visible"] = bool(
                self._playfield_detector(image)
            )
            self._recognition_samples.append(snapshot)
            print(
                "ContinuousRealtimeLive recognition_wait "
                f"frames={snapshot.get('frames', 0)} "
                f"candidate={snapshot.get('candidate_song_id', UNKNOWN_SONG_ID)} "
                f"stable_frames={snapshot.get('candidate_frames', 0)} "
                f"title={snapshot.get('title')!r} "
                f"title_confidence={float(snapshot.get('title_confidence', 0.0)):.3f} "
                f"playfield={snapshot['playfield_visible']} "
                f"reason={snapshot.get('reason', 'unknown')}",
                flush=True,
            )
        candidate_frames = int(snapshot.get("candidate_frames", 0))
        title = str(snapshot.get("title") or "").strip()
        key = (
            snapshot.get("candidate_song_id"),
            title,
            snapshot.get("reason"),
        )
        if (
            candidate_frames >= 2
            and key not in self._candidate_keys
            and len(self._recognition_candidates) < 8
        ):
            self._candidate_keys.add(key)
            copied = image.copy() if callable(getattr(image, "copy", None)) else image
            self._recognition_candidates.append((copied, snapshot))

    def save(self, reason: str) -> Path | None:
        if self._saved or self._latest is None:
            return None
        self._saved = True
        output = self._root / (
            "listener-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        )
        output.mkdir(parents=True, exist_ok=False)
        image_path = output / "last-frame.png"
        if not imwrite_unicode(image_path, self._latest):
            raise OSError(f"unable to save listener diagnostic: {image_path}")
        candidate_metadata: list[dict[str, object]] = []
        for index, (candidate_image, state) in enumerate(
            self._recognition_candidates,
            start=1,
        ):
            candidate_path = output / f"candidate-{index:03d}.png"
            if imwrite_unicode(candidate_path, candidate_image):
                candidate_metadata.append({
                    **state,
                    "file": candidate_path.name,
                })
        metadata: dict[str, object] = {"reason": reason}
        if self._recognition_samples:
            metadata["recognition_samples"] = self._recognition_samples
        if candidate_metadata:
            metadata["recognition_candidates"] = candidate_metadata
        (output / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            f"ContinuousRealtimeLive diagnostic={image_path} reason={reason}",
            flush=True,
        )
        return image_path


def continuous_song_params(params: dict) -> dict:
    """构造一键演奏策略；生命归零后继续等待可识别的演奏终态。"""
    return {
        **params,
        "run_mode": "continuous",
        "require_profile": True,
        "ignore_note_speed": True,
        "confirm_final_cover": True,
        "native_prearm_deferred": True,
        "duration_seconds": None,
        "wait_for_completion": True,
        "completion_missing_frames": int(
            params.get("completion_missing_frames", 30)
        ),
        "require_completion": True,
        "save_result_frame": True,
        "continue_after_life_depleted": True,
    }


def recover_continuous_result_home(context: Context) -> None:
    """用所有演出共用的安全像素/BACK节拍从结算恢复主页。"""
    params = {
        "home_node": "RealtimeLiveHomeMarker",
        "modal_cancel_nodes": ["QuitConfirmCancel"],
        "click_nodes": [],
        "back_only_click_nodes": list(STORY_NODES),
        "back_only": True,
        "back_acceleration_click_point": list(RESULT_ANIMATION_SKIP_POINT),
        "escape_interval_ms": 500,
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
    argv = SimpleNamespace(
        custom_action_param=json.dumps(params, ensure_ascii=False)
    )
    if not CommonRecover().run(context, argv):
        print("ContinuousRealtimeLive post_result_warning=演出已完成，结算后无法恢复主页", flush=True)


@dataclass(frozen=True, slots=True)
class ContinuousSongEvidence:
    """一键监听器在开场页确认的歌曲身份与原始画面。"""

    image: object
    song: CatalogSongIdentity
    song_id: str
    song_id_method: str
    chart_resolution: FinalCoverResolution | None
    observed_title: str
    observed_title_confidence: float
    observed_frames: int


class ContinuousFinalCoverRecognizer:
    """用稳定封面、标题和任务难度确认一键演奏的本地谱面。"""

    def __init__(
        self,
        repository: LocalChartRepository,
        difficulty: str,
        *,
        stable_frames: int = CONTINUOUS_COVER_STABLE_FRAMES,
        identify: Callable[[object], object] = identify_final_song,
        title_reader: Callable[..., object | None] = recognize_song_title,
    ) -> None:
        if int(stable_frames) < 2:
            raise ValueError("一键演奏开场封面至少需要连续两帧确认")
        self.repository = repository
        self.difficulty = str(difficulty).strip()
        if not self.difficulty:
            raise ValueError("一键演奏任务难度不能为空")
        self.stable_frames = int(stable_frames)
        self._identify = identify
        self._title_reader = title_reader
        self.frames = 0
        self.last_reason = "尚未观察到开场歌曲封面"
        self.reset()

    def reset(self) -> None:
        """清除上一首歌的候选，禁止跨歌曲累计稳定帧。"""
        self._candidate_song_id = UNKNOWN_SONG_ID
        self._candidate_frames = 0
        self._title = None
        self._title_confidence = 0.0
        self._full_badge_seen = False

    def diagnostic_state(self) -> dict[str, object]:
        """返回当前门禁状态，供有界诊断记录使用。"""
        return {
            "frames": self.frames,
            "reason": self.last_reason,
            "candidate_song_id": self._candidate_song_id,
            "candidate_frames": self._candidate_frames,
            "title": self._title,
            "title_confidence": self._title_confidence,
            "full_badge_seen": self._full_badge_seen,
        }

    def observe(self, image: object) -> ContinuousSongEvidence | None:
        self.frames += 1
        identity = self._identify(image)
        song_id = str(getattr(identity, "song_id", UNKNOWN_SONG_ID))
        if song_id == UNKNOWN_SONG_ID:
            self.reset()
            self.last_reason = "开场歌曲封面尚不可见"
            return None
        if (
            self._candidate_song_id == UNKNOWN_SONG_ID
            or not same_song(song_id, self._candidate_song_id)
        ):
            self._candidate_song_id = song_id
            self._candidate_frames = 0
            self._title = None
            self._title_confidence = 0.0
            self._full_badge_seen = False
        self._candidate_frames += 1
        self._full_badge_seen = (
            self._full_badge_seen or detect_full_badge(image)
        )

        title = self._title_reader(image, roi=FINAL_COVER_TITLE_ROI)
        if (
            title is not None
            and str(getattr(title, "text", "")).strip()
            and float(getattr(title, "confidence", 0.0))
            > self._title_confidence
        ):
            self._title = str(title.text).strip()
            self._title_confidence = float(title.confidence)
        if self._title is None:
            self.last_reason = "开场歌曲标题尚未识别"
            return None
        if self._candidate_frames < self.stable_frames:
            self.last_reason = "等待开场歌曲封面稳定"
            return None

        identity_resolution = self.repository.identify_by_cover_title(
            song_id,
            self._title,
        )
        if (
            identity_resolution.identity is None
            and identity_resolution.reason
            == "song cover and title mapping is ambiguous"
        ):
            # 普通谱面与 [FULL] 谱面可能共用封面和去前缀后的标题。看到
            # FULL 徽标可立即收窄；未看到徽标时多等两帧，避免缩放淡入期
            # 的暂时漏检把 FULL 错认成普通谱面。
            full_badge = (
                True
                if self._full_badge_seen
                else (
                    False
                    if self._candidate_frames >= self.stable_frames + 2
                    else None
                )
            )
            if full_badge is not None:
                identity_resolution = self.repository.identify_by_cover_title(
                    song_id,
                    self._title,
                    full_badge=full_badge,
                )
        catalog_song = identity_resolution.identity
        if catalog_song is None:
            self.last_reason = identity_resolution.reason
            return None
        # 歌曲已经由封面与标题共同确认；谱面查询改用该曲库条目的标准
        # 指纹，避免开场页 9--14 bit 的裁切差异在第二道严格门禁再次失败。
        chart_song_id = (
            catalog_song.fingerprints[0]
            if catalog_song.fingerprints
            else song_id
        )
        chart = self.repository.resolve(
            chart_song_id,
            self.difficulty,
            title=self._title,
            bestdori_song_id=catalog_song.bestdori_song_id,
        )
        confirmation = FinalCoverConfirmation(
            song_id=song_id,
            song_id_method=str(getattr(identity, "method", "unknown")),
            bestdori_song_id=catalog_song.bestdori_song_id,
        )
        self.last_reason = "开场歌曲封面与标题已确认"
        return ContinuousSongEvidence(
            image=(
                image.copy()
                if callable(getattr(image, "copy", None))
                else image
            ),
            song=catalog_song,
            song_id=confirmation.song_id,
            song_id_method=confirmation.song_id_method,
            chart_resolution=(
                FinalCoverResolution(
                    confirmation=confirmation,
                    selection=chart.selection,
                )
                if chart.selection is not None else None
            ),
            observed_title=self._title,
            observed_title_confidence=self._title_confidence,
            observed_frames=self._candidate_frames,
        )


def run_continuous_listener(
    capture: Callable[[], object],
    stopping: Callable[[], bool],
    play_song: Callable[[ContinuousSongEvidence], bool],
    *,
    recognizer: ContinuousFinalCoverRecognizer,
    sleeper: Callable[[float], None] = time.sleep,
    poll_interval_seconds: float = 0.1,
    on_frame: Callable[[object], None] | None = None,
    on_observation: Callable[[object, dict[str, object]], None] | None = None,
) -> bool:
    """被动等待开场身份，完整演奏第一首歌后返回成功。"""
    while not stopping():
        image = capture()
        if on_frame is not None:
            on_frame(image)
        evidence = recognizer.observe(image)
        if on_observation is not None:
            on_observation(image, recognizer.diagnostic_state())
        if evidence is not None:
            if not play_song(evidence):
                raise RuntimeError("continuous realtime song playback failed")
            return True
        sleeper(poll_interval_seconds)
    return False


@AgentServer.custom_action("ContinuousRealtimeLiveConfigure")
class ContinuousRealtimeLiveConfigure(CustomAction):
    """接收独立难度/诊断节点并保存本次一键演奏配置。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        if context.tasker.stopping:
            return True
        try:
            settings = configure_continuous_settings(
                parse_custom_action_params(argv.custom_action_param)
            )
            print(
                f"ContinuousRealtimeLive configured={settings}",
                flush=True,
            )
            return True
        except Exception as exc:
            record_failure_reason(
                f"一键实时演奏选项无效：{type(exc).__name__}: {exc}"
            )
            traceback.print_exc()
            return False


@AgentServer.custom_action("ContinuousRealtimeLive")
class ContinuousRealtimeLive(CustomAction):
    """被动识别并演奏一首歌曲，结算回主页后自动结束任务。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            return self._run(context, argv)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            record_failure_reason(reason)
            traceback.print_exc()
            print(f"ContinuousRealtimeLive failed={reason}", flush=True)
            return False

    def _run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        params = {
            **current_continuous_settings(),
            **parse_custom_action_params(argv.custom_action_param),
        }
        if context.tasker.stopping:
            return True
        settings_check_enabled = bool(
            RealtimeProfileStore(
                PROJECT_ROOT / "profiles"
            ).runtime_options().get("note_speed_settings_enabled", True)
        )
        difficulty = str(params.get("difficulty", "Easy"))
        require_recent_speed_settings(
            difficulty,
            enabled=settings_check_enabled,
        )
        settings = resolve_profile_for_settings_gate(context, params)
        print(
            "ContinuousRealtimeLive started "
            f"profile={settings.profile_path.name} "
            f"difficulty={difficulty} "
            f"profile_speed={settings.note_speed:.2f}; "
            "recognition=final-cover+title; listener does not read or change "
            "game note speed, actual speed must match",
            flush=True,
        )

        song_params = continuous_song_params(params)
        recognizer = ContinuousFinalCoverRecognizer(
            LocalChartRepository(PROJECT_ROOT / "resource" / "charts"),
            difficulty,
        )
        diagnostics = ListenerDiagnosticCapture(
            Path(__file__).resolve().parents[2] / "debug" / "recordings"
        )

        def play_song(evidence: ContinuousSongEvidence) -> bool:
            if context.tasker.stopping:
                return True
            require_recent_speed_settings(
                difficulty,
                enabled=settings_check_enabled,
            )
            selection = (
                evidence.chart_resolution.selection
                if evidence.chart_resolution is not None else None
            )
            reset_live_run(
                mode="continuous",
                difficulty=difficulty,
                requested_difficulty=difficulty,
                prepared_for_play=True,
            )
            update_live_run(
                song_id=evidence.song_id,
                song_id_method=evidence.song_id_method,
                song_level=(selection.level if selection is not None else None),
                song_title=evidence.observed_title,
                song_title_confidence=evidence.observed_title_confidence,
                final_cover_confirmed=True,
                final_cover_song_id=evidence.song_id,
                final_cover_status="confirmed-by-listener",
                startup_final_cover_image=(
                    evidence.image if evidence.chart_resolution is not None else None
                ),
                startup_final_cover_resolution=evidence.chart_resolution,
            )
            print(
                "ContinuousRealtimeLive song_identified=true "
                f"bestdori_song_id={evidence.song.bestdori_song_id} "
                f"difficulty={difficulty} "
                f"local_chart={selection is not None} "
                f"title={evidence.observed_title!r} "
                f"title_confidence={evidence.observed_title_confidence:.3f} "
                f"cover_frames={evidence.observed_frames}",
                flush=True,
            )
            current_song_params = {
                **song_params,
                "confirm_final_cover": evidence.chart_resolution is not None,
                "native_prearm_deferred": evidence.chart_resolution is not None,
            }
            song_argv = SimpleNamespace(
                custom_action_param=json.dumps(
                    current_song_params,
                    ensure_ascii=False,
                )
            )
            return RealtimeProfilePlay()._run(context, song_argv)

        try:
            completed = run_continuous_listener(
                lambda: context.tasker.controller.post_screencap().wait().get(),
                lambda: context.tasker.stopping,
                play_song,
                recognizer=recognizer,
                on_observation=diagnostics.observe_recognition,
            )
        except Exception:
            diagnostics.save("failed")
            raise
        if context.tasker.stopping or not completed:
            diagnostics.save("stopped")
            print("ContinuousRealtimeLive stopped by user", flush=True)
            return True
        try:
            recover_continuous_result_home(context)
        except Exception as exc:
            print(f"ContinuousRealtimeLive post_result_warning={type(exc).__name__}: {exc}", flush=True)
        print(
            "[任务][一键实时演奏][结束][SUCCESS] "
            "已确认演奏结束，结算返回流程已执行（异常见警告）",
            flush=True,
        )
        return True
