from __future__ import annotations

# 日志通道是附加功能，输出故障不得改变演出、停止或清理结果。
from .optional_diagnostics import safe_print as print, safe_traceback

import json
import os
import threading
import time
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    from ..common_recover import CommonRecover
    from ..foreground_guard import GAME_PACKAGE, foreground_package, require_game_foreground
    from ..screen_refresh import ScreenRefreshCancelled, capture_image
    from ..task_reporting import TaskProgress, record_failure_reason
except ImportError:
    from common_recover import CommonRecover
    from foreground_guard import GAME_PACKAGE, foreground_package, require_game_foreground
    from screen_refresh import ScreenRefreshCancelled, capture_image
    from task_reporting import TaskProgress, record_failure_reason

from .difficulty_action import RealtimeDifficultySelect
from .game_effect_settings_action import _click as _maa_click
from .vision_io import imread_unicode, imwrite_unicode
from .game_effect_settings_action import _swipe as _maa_swipe
from .live_session import (
    append_current_run_event,
    current_live_run,
    reset_live_run,
    update_live_run,
)
from .life_monitor import LifeDetector, LifeGuard, LifeStatus
from .live_visual_gate import MODE_TOGGLE_POINT, live_performance_mode_is_off
from .performance_settings_action import RealtimePerformanceSettingsGate
from .playfield_monitor import PlayfieldDetector
from .chart_repository import LocalChartRepository
from .final_cover import (
    FinalCoverResolver, FinalCoverStartupFailure, FinalCoverIdentityConflict,
)
from .startup_diagnostics import CooperativeStartupRecorder, cover_diagnostic
from .room_song_entry import (RoomEntryEvidence, RoomEntryTransient, guarded_song_choice_click,
                              select_song_and_wait, rematch_backoff, wait_for_preplay_recovery_page,
                              check_preplay_connection_failed, retry_connection_failed)
from .profile_play_action import RealtimeProfilePlay
from .profile_store import (
    EnvironmentSignature,
    RealtimeProfileStore,
    engine_from_native_flag,
)
from .rehearsal_action import frame_resolution
from .native_prearm import discard_prearmed_backend
from .stamina_task import (prepare_stamina_round, allocate_flow_home, mark_uncertain, mark_stamina_settled, StaminaTaskError)
from .cooperative_network import GameNetworkGate, UidBlackholeGate, run_network_shell
from .result_navigation import (
    RESULT_ANIMATION_SKIP_POINT,
    accelerated_back,
    handle_story_page,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = PROJECT_ROOT / "resource" / "image" / "versus"
# 与 prepare() 传给流速门禁的环境参数保持一致：这些值同时用于开局前的
# Profile 预检，避免两处硬编码漂移。
VERSUS_DPI = 240
VERSUS_GAME_FPS = 60
VERSUS_RENDER_QUALITY = "standard"
TEMPLATE_POSITIONS = {
    # 竞演演出：1280×720 标定，取自 2026-10-16 MuMu 截图/录像。
    # ★ 这些是模板在整帧里的**左上角**坐标（template_box 以此为搜索窗中心
    #   向外扩 padding，窗口 = [x-pad, x+w+pad]），不是按钮中心。
    "live_entry": (940, 200),         # 演出选择页右上「竞演演出」卡片
    "room_search": (562, 620),        # 房间选择页「房间搜索」按钮（页面标记）
    "create_room": (368, 620),        # 房间选择页「创建房间」按钮
    "room_type_activity": (490, 140), # 居中「活动歌曲房间」卡片
    "room_type_free": (490, 140),     # 居中「自由选歌房间」卡片
    "room_together": (662, 532),      # 房间页「房间 大家一起选曲」栏（页面标记）
    "song_unspecified": (685, 618),   # 选曲页「不指定歌曲」
    "song_random": (685, 530),        # 选曲页「随机选曲」
    "ready_button": (1012, 568),      # 准备页「准备完毕！」
    "member_exit_title": (399, 158),
    "connect_failed_body": (580, 345),
}

DEFAULT_SETTINGS: dict[str, object] = {
    "entry_method": "confirm",    # 竞演目前只支持「选房型卡 + 确定」入房
    "room_type": "free",          # free = 自由选歌房间；activity = 活动歌曲房间
    "difficulty": "Expert",
    "count": 1,
    "post_live_action": "exit",
    "member_exit_policy": "reconnect",
    "max_reconnects": 3,
    "debug_recording": False,
    "diagnostic_trace": True,
    "opening_recording": False,
    "disconnect_jump_enabled": False,
    "song_choice": "unspecified",
}
_SETTINGS = dict(DEFAULT_SETTINGS)
_SETTINGS_LOCK = threading.Lock()

# 房间选择页只有两张卡：0=活动歌曲房间，1=自由选歌房间。
ROOM_TYPE_INDEX = {
    "activity": 0,
    "free": 1,
}
VERSUS_DIFFICULTY_TARGETS = {
    "Easy": (602, 575),
    "Normal": (687, 575),
    "Hard": (769, 575),
    "Expert": (852, 575),
    "Special": (942, 575),
}
MEMBER_DOWNLOAD_TIMEOUT_SECONDS = 60.0
ROOM_SONG_CHOICE_TIMEOUT_SECONDS = 180.0
SONG_CHOICE_TO_READY_TIMEOUT_SECONDS = 60.0
POST_SCORE_NAVIGATION_TIMEOUT_SECONDS = 60.0
HOME_LIVE_POINT = (1175, 645)
# 跳车按钮点击点：弹窗1“通信已中断。是否继续演出？”点左侧“中断”；
# 弹窗2“确认中断当前演出返回主页吗？”点右侧粉色“中断”。
DISCONNECT_CONTINUE_INTERRUPT_POINT = (508, 447)
DISCONNECT_CONFIRM_INTERRUPT_POINT = (754, 439)
READY_DELIVERY_OBSERVE_SECONDS = 2.0
# 选曲页坐标沿用贡献者的 1280×720 标定；默认仍选择不指定歌曲。
VERSUS_SONG_RANDOM_POINT = (780, 565)
VERSUS_SONG_UNSPECIFIED_POINT = (780, 655)
VERSUS_SONG_CONFIRM_POINT = (1067, 655)
VERSUS_SONG_CHOICES = ("unspecified", "random", "current")
VERSUS_SONG_CHOICE_PAUSE_SECONDS = 10.0
ROOM_RECOVERY_TRIAL_ENV = "MAABANGDREAM_VERSUS_ROOM_RECOVERY_TRIAL"
# 竞演「房间选择」页：选好房型卡后点右下「确定」入房（不是「创建房间」——
# 那是手动建带房间号的房，会卡在设置弹窗）。
VERSUS_CONFIRM_POINT = (1035, 647)
ROOM_PREPARATION_RETRY_LIMIT = 2


def _frame_is_black_transition(image: np.ndarray) -> bool:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return float(gray.mean()) < 6.0 and float(gray.std()) < 6.0


class VersusPlayfieldEntryEvidence:
    """为准备后漏黑场保留的有界演奏场动态证据。

    PlayfieldDetector 的生命条和白色判定线在准备页也可能同时出现，不能单独
    放行。这里仅观察判定线上方的音符区域：要求连续两帧出现局部列变化，并
    拒绝覆盖大面积列的转场/变暗。它只决定成员退出监听何时结束，不参与
    Native 首音锚点、scheduler 或任何歌曲时间计算。
    """

    _REFERENCE_HEIGHT = 720
    _TOP = 430
    _BOTTOM = 570
    _COLUMN_CHANGE_THRESHOLD = 18.0
    _MIN_CHANGED_COLUMNS = 12
    _BROAD_FRACTION = 0.30
    _REQUIRED_NARROW_EVENTS = 2

    def __init__(self) -> None:
        self._previous_columns: np.ndarray | None = None
        self._narrow_motion_streak = 0

    def reset(self) -> None:
        self._previous_columns = None
        self._narrow_motion_streak = 0

    def observe(self, image: np.ndarray, *, playfield_visible: bool) -> bool:
        if not playfield_visible:
            self.reset()
            return False
        if (
            not isinstance(image, np.ndarray)
            or image.ndim != 3
            or image.shape[2] < 3
            or image.shape[0] < 2
            or image.shape[1] < 2
        ):
            self.reset()
            return False
        height = image.shape[0]
        scale = height / self._REFERENCE_HEIGHT
        top = max(0, min(height - 1, round(self._TOP * scale)))
        bottom = max(top + 1, min(height, round(self._BOTTOM * scale)))
        columns = image[top:bottom, :, :3].astype("float32").mean(axis=(0, 2))
        if self._previous_columns is None:
            self._previous_columns = columns
            return False
        changed_columns = int(np.count_nonzero(
            np.abs(columns - self._previous_columns)
            >= self._COLUMN_CHANGE_THRESHOLD
        ))
        self._previous_columns = columns
        if changed_columns >= image.shape[1] * self._BROAD_FRACTION:
            # 整屏淡入、成员等待弹窗及其遮罩都会造成大面积同向变化，不是音符。
            self._narrow_motion_streak = 0
            return False
        if changed_columns < self._MIN_CHANGED_COLUMNS:
            self._narrow_motion_streak = 0
            return False
        self._narrow_motion_streak += 1
        return self._narrow_motion_streak >= self._REQUIRED_NARROW_EVENTS


def versus_play_params(
    settings: dict[str, object],
    *,
    effective_difficulty: str | None = None,
) -> dict[str, object]:
    return {
        "difficulty": str(effective_difficulty or settings["difficulty"]),
        "require_profile": True,
        "settings_gate_required": True,
        "debug_recording": bool(settings["debug_recording"]),
        "diagnostic_trace": bool(settings["diagnostic_trace"]),
        "opening_recording": bool(settings.get("opening_recording",False)),
        "duration_seconds": 600,
        "startup_timeout_seconds": 60,
        "dpi": 240,
        "game_fps": 60,
        "render_quality": "standard",
        "wait_for_completion": True,
        "completion_missing_frames": 30,
        "require_completion": True,
        "save_result_frame": True,
        "result_back_attempts": 30,
        "result_back_interval_seconds": 1.5,
        "continue_after_life_depleted": True,
        "run_mode": "versus",
        "confirm_final_cover": True,
        "versus_member_loading_guard_enabled": settings.get(
            "versus_member_loading_guard_enabled", True
        ),
        "final_cover_timeout_seconds": MEMBER_DOWNLOAD_TIMEOUT_SECONDS,
        "native_prearm_deferred": True,
        "life_depleted_jump_request": bool(
            settings.get("disconnect_jump_enabled", False)
        ),
    }


class MemberExited(RuntimeError):
    pass


class JumpOutUnavailable(RuntimeError):
    """竞演局已安全跳车或无法继续自动恢复，应立即结束任务。"""


class RoomPreparationTimeout(RoomEntryTransient):
    """准备页未在观察预算内送达，可执行有界主页恢复。"""


def configure_versus_settings(params: dict[str, object]) -> dict[str, object]:
    with _SETTINGS_LOCK:
        candidate = (
            dict(DEFAULT_SETTINGS)
            if bool(params.get("reset", False))
            else dict(_SETTINGS)
        )
        for key in DEFAULT_SETTINGS:
            if key in params:
                candidate[key] = params[key]
        count = int(candidate.get("count", 1))
        if not 0 <= count <= 999:
            raise ValueError("竞演演出次数必须是0到999的整数，0表示无限")
        candidate["count"] = count
        song_choice = str(candidate.get("song_choice", "unspecified"))
        if song_choice not in VERSUS_SONG_CHOICES:
            raise ValueError("竞演歌曲选择必须是 unspecified/random/current 之一")
        candidate["song_choice"] = song_choice
        room_type = str(candidate.get("room_type", "free"))
        if room_type not in ROOM_TYPE_INDEX:
            raise ValueError("竞演房型必须是 activity/free 之一")
        candidate["room_type"] = room_type
        _SETTINGS.clear()
        _SETTINGS.update(candidate)
        return dict(_SETTINGS)


def current_versus_settings() -> dict[str, object]:
    with _SETTINGS_LOCK:
        return dict(_SETTINGS)


def versus_profile_preflight(context: Context, difficulty: str) -> str | None:
    """任务一开始就校验 Profile 与环境签名，失败返回可读原因。

    原实现把 Profile 解析放在准备页的流速门禁里，自动化已经完成整段导航
    才可能被拒。这里提前用截图分辨率、固定 DPI/帧率/画质和引擎构造签名；
    旧 Profile 中的视觉设置字段只兼容读取，不参与匹配。
    """
    store = RealtimeProfileStore(PROJECT_ROOT / "profiles")
    try:
        image = context.tasker.controller.post_screencap().wait().get()
    except Exception:
        # 控制器尚未就绪时无法构造签名，交给准备页门禁处理。
        return None
    options = store.runtime_options()
    signature = EnvironmentSignature(
        frame_resolution(image),
        VERSUS_DPI,
        VERSUS_GAME_FPS,
        VERSUS_RENDER_QUALITY,
        1.0,
        engine=engine_from_native_flag(
            options.get("native_realtime_enabled", False)
        ),
    )
    try:
        store.resolve_latest_for_environment(
            difficulty=difficulty,
            current_signature=signature,
        )
    except ValueError as exc:
        return f"开局前 Profile 环境校验失败：{exc}"
    return None


def should_stay_in_room(settings: dict[str, object]) -> bool:
    """竞演不做房间复用：打完一局不回原房，重进房间选择页重新建房。"""
    return False


_ROOM_TYPE_TEMPLATES: dict[str, np.ndarray | None] = {}


def _room_type_templates() -> dict[str, np.ndarray | None]:
    if not _ROOM_TYPE_TEMPLATES:
        _ROOM_TYPE_TEMPLATES["room_type_activity"] = imread_unicode(
            TEMPLATE_DIR / "room_type_activity.png"
        )
        _ROOM_TYPE_TEMPLATES["room_type_free"] = imread_unicode(
            TEMPLATE_DIR / "room_type_free.png"
        )
    return _ROOM_TYPE_TEMPLATES


def classify_room_type(image: np.ndarray, *, threshold: float = 0.72) -> str | None:
    """用两张房型卡模板判断当前居中的是「活动歌曲房间」还是「自由选歌房间」。

    房间选择页只有两张卡，居中卡是原始尺寸、两侧卡缩小并部分遮挡，所以对整帧
    同时匹配两张**居中尺寸**的模板，取分数更高且超过阈值者；否则视为尚未稳定
    （轮播动画中途）返回 None，由调用方重试。
    """
    if not isinstance(image, np.ndarray) or image.ndim != 3:
        return None
    best: str | None = None
    best_score = threshold
    for name, template in _room_type_templates().items():
        if template is None or template.shape[0] > image.shape[0] or template.shape[1] > image.shape[1]:
            continue
        result = cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, _ = cv2.minMaxLoc(result)
        if score > best_score:
            best, best_score = name, score
    if best is None:
        return None
    return "activity" if best == "room_type_activity" else "free"


class VersusLiveFlow:
    def __init__(
        self,
        context: Context,
        settings: dict[str, object],
        *,
        progress_callback: Callable[[int, int], None] | None = None,
        stamina_argv=None,
    ) -> None:
        self.context = context
        self.stamina_argv = stamina_argv
        self.settings = dict(settings)
        # 本次进程候选只在任务入口读取，避免导航中途改变重试语义。
        # 竞演与协力一致：房间 180 秒未开始就回主页重进，因此恢复默认开启，
        # 只有显式置 "0" 才回退成「耗尽即终止任务」。
        self.room_recovery_trial = os.environ.get(ROOM_RECOVERY_TRIAL_ENV, "1") != "0"
        print(f"VersusLive room_recovery_trial={self.room_recovery_trial}", flush=True)
        # 在导航输入前冻结本任务的保护开关，两条封面门禁共享，不逐帧读文件。
        # 运行时选项键沿用协力的 cooperative_member_loading_guard_enabled：
        # 「成员加载页保护」是共用的 MFA 任务安全开关，没有竞演专属项。
        if "versus_member_loading_guard_enabled" not in self.settings:
            self.settings["versus_member_loading_guard_enabled"] = RealtimeProfileStore(
                PROJECT_ROOT / "profiles"
            ).runtime_options()["cooperative_member_loading_guard_enabled"]
        if not isinstance(self.settings["versus_member_loading_guard_enabled"], bool):
            raise ValueError("versus_member_loading_guard_enabled 必须是布尔值")
        print(
            "VersusLive versus_member_loading_guard_enabled="
            f"{self.settings['versus_member_loading_guard_enabled']} source=task_snapshot",
            flush=True,
        )
        self.effective_difficulty = str(settings["difficulty"])
        self.progress_callback = progress_callback
        self.detector = LifeDetector()
        # 准备完毕后的短黑场可能只持续一帧；漏检时先用与本局身份一致的
        # 稳定最终封面放行；演奏场误判不能抢先结束最终封面的连续检测。
        self.playfield_detector = PlayfieldDetector()
        self.playfield_entry_evidence = VersusPlayfieldEntryEvidence()
        self.song_choice_pause_pending = True
        self.templates = {
            path.stem: imread_unicode(path, cv2.IMREAD_COLOR)
            for path in TEMPLATE_DIR.glob("*.png")
        }
        missing = [name for name, image in self.templates.items() if image is None]
        if missing:
            raise RuntimeError(f"竞演模板损坏：{', '.join(missing)}")

    def stopped(self) -> bool:
        return bool(self.context.tasker.stopping)

    @property
    def controller(self):
        """Always return MaaFramework's current reverse-controller proxy.

        Nested pipeline tasks may replace the proxy, so retaining the value
        seen in ``__init__`` can dereference an already released native handle.
        """
        return self.context.tasker.controller

    def capture(self) -> np.ndarray:
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        try:
            started_at = time.monotonic()
            if getattr(self, "_post_score_refresh", False):
                image = capture_image(self.context, node="ResultRefreshScreen")
            else:
                image = capture_image(self.context)
            self._last_capture_timestamp = time.monotonic()
            self._last_capture_duration_ms = (self._last_capture_timestamp - started_at) * 1000
            evidence = getattr(self, "_room_entry_evidence", None)
            if evidence is not None:
                try:
                    evidence.observe(image, self._last_capture_timestamp, self.templates)
                except Exception as exc:
                    print(f"RoomEntry diagnostics_warning={type(exc).__name__}: {exc}", flush=True)
            return image
        except ScreenRefreshCancelled as exc:
            raise InterruptedError("用户已停止任务") from exc

    def _startup_record(self, image, phase, **details):
        if self.settings.get("opening_recording", False):
            try:
                from .live_session import retain_opening_observation
                retain_opening_observation(image, getattr(self, "_last_capture_timestamp", None), phase,
                    {"source": "existing-room-capture", "timestamp_kind": "capture-completed-upper-bound", **details})
            except Exception as exc:
                print(f"Opening diagnostics_warning={type(exc).__name__}: {exc}", flush=True)
        recorder = getattr(self, "_startup_recorder", None)
        if recorder is not None:
            try:
                recorder.record(image, time.monotonic(), {
                    "phase": phase,
                    "capture_ms": getattr(self, "_last_capture_duration_ms", None),
                    "captured_at": getattr(self, "_last_capture_timestamp", None),
                    **details,
                })
            except Exception as exc:
                print(f"VersusStartup diagnostics_warning={type(exc).__name__}: {exc}", flush=True)

    def _start_startup_diagnostics(self):
        # 开局逐帧 PNG 只在「完整录像」档开：这是每局最多 600 帧 / 128 MB 的原始帧流，
        # 「轻量 Trace」档保留主运行录制器的 trace 与关键帧，但不在这里存逐帧原图。
        if not self.settings.get("debug_recording", False) or getattr(self, "_startup_recorder", None) is not None:
            return
        try:
            run = current_live_run()
            self._startup_recorder = CooperativeStartupRecorder(
                PROJECT_ROOT / "debug" / "recordings", getattr(run, "run_id", None),
                loading_guard_enabled=self.settings["versus_member_loading_guard_enabled"],
            )
            print(f"VersusStartup recording={self._startup_recorder.output_dir}", flush=True)
        except Exception as exc:
            print(f"VersusStartup diagnostics_warning={type(exc).__name__}: {exc}", flush=True)

    def _startup_playfield_visible(self, image):
        if getattr(self, "_startup_recorder", None) is None:
            return None
        try:
            return bool(self.playfield_detector(image))
        except Exception as exc:
            # 仅为诊断读取演奏场；检测器故障不能改变最终封面的业务结果。
            print(f"VersusStartup diagnostics_warning={type(exc).__name__}: {exc}", flush=True)
            return None

    def template_box(
        self,
        image: np.ndarray,
        name: str,
        threshold: float = 0.90,
    ) -> tuple[int, int, int, int] | None:
        template = self.templates[name]
        x, y = TEMPLATE_POSITIONS[name]
        padding = 8
        left = max(0, x - padding)
        top = max(0, y - padding)
        right = min(image.shape[1], x + template.shape[1] + padding)
        bottom = min(image.shape[0], y + template.shape[0] + padding)
        search = image[top:bottom, left:right]
        if search.shape[0] < template.shape[0] or search.shape[1] < template.shape[1]:
            return None
        _, score, _, location = cv2.minMaxLoc(
            cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
        )
        if float(score) < threshold:
            return None
        return (
            left + int(location[0]),
            top + int(location[1]),
            int(template.shape[1]),
            int(template.shape[0]),
        )

    def visible(self, image: np.ndarray, name: str, threshold: float = 0.90) -> bool:
        return self.template_box(image, name, threshold) is not None

    def pipeline_box(self, image: np.ndarray, node: str):
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        result = self.context.run_recognition(node, image)
        if not result or not result.hit:
            return None
        return result.box

    def click(self, point: tuple[int, int]) -> None:
        _maa_click(self.context, point)

    def guarded_song_choice_click(self, point: tuple[int, int]) -> bool:
        return guarded_song_choice_click(
            self, point, "versus", TEMPLATE_POSITIONS["song_unspecified"],
        )

    def wait_for(
        self,
        names: tuple[str, ...],
        *,
        timeout: float,
        interval: float = 0.35,
        detect_member_exit: bool = True,
    ) -> tuple[str | None, np.ndarray]:
        deadline = time.monotonic() + timeout
        image = self.capture()
        while True:
            if detect_member_exit:
                check_preplay_connection_failed(self, image)
            if detect_member_exit and self.visible(
                image,
                "member_exit_title",
                0.93,
            ):
                raise MemberExited("竞演成员退出房间")
            for name in names:
                if name == "playfield":
                    if self.detector.detect(image).visible:
                        return name, image
                elif self.visible(image, name):
                    return name, image
            if time.monotonic() >= deadline:
                return None, image
            time.sleep(interval)
            image = self.capture()

    def dismiss_member_exit(self) -> None:
        image = self.capture()
        check_preplay_connection_failed(self, image)
        if self.visible(image, "member_exit_title", 0.93):
            # 当前版本弹窗：标题“错误”，正文“由于XX退出房间。将返回
            # 房间选择界面。”，底部居中“确定”按钮。
            self.click((638, 525))
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            time.sleep(0.8)

    def dismiss_connect_failed(self, attempts: int = 5) -> bool:
        return retry_connection_failed(self, attempts)

    def _adb_shell(self, args) -> tuple[int, str]:
        """核验本次 Maa 作业和远端退出码后再返回网络命令结果。"""
        return run_network_shell(self.controller, args)

    def _wait_and_click(
        self,
        name: str,
        point: tuple[int, int],
        timeout: float,
    ) -> bool:
        """有界等待模板出现并点击一次；超时或停止时失败。"""
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            image = self.capture()
            if self.visible(image, name, 0.93):
                self.click(point)
                return True
            time.sleep(0.35)
        return False

    def disconnect_jump_out(self, *, popup_timeout_s: float = 25.0) -> bool:
        """历史按游戏 UID 隔离网络的跳车实现；当前联机流程不调用。

        真实弹窗顺序（2026-09-07 雷电录像）：按游戏 UID 屏蔽出口流量 → 游戏
        退后台再切回 → 弹窗1“通信已中断。是否继续演出？”点左侧“中断” →
        弹窗2“确认中断当前演出返回主页吗？”点右侧“中断” → 恢复网络 →
        “连接失败。”弹窗有界点“重试”直到回主页。任何一步失败都在 finally
        恢复网络（fail-closed），绝不把模拟器留在网络隔离状态。
        """
        gate = UidBlackholeGate(self._adb_shell)
        try:
            required = {
                "disconnect_continue_body",
                "disconnect_confirm_body",
            }
            if not required.issubset(self.templates):
                # 模板未提取时不做任何网络/前台扰动，直接 fail-closed。
                print(
                    "VersusDisconnectJump popup_template_missing=true",
                    flush=True,
                )
                return False
            # MuMu 12 的内核没有 iptables owner 模块，uidrange 是唯一可用
            # 的后端；老环境写入失败时回退到 iptables owner。
            if not gate.cleanup_stale():
                print(
                    "VersusDisconnectJump network_cleanup_unconfirmed=true "
                    f"reason={gate.last_error or 'unknown'}",
                    flush=True,
                )
                return False
            if gate.block():
                print(
                    "VersusDisconnectJump gate_backend=uidrange",
                    flush=True,
                )
            else:
                if not gate.restore():
                    print(
                        "VersusDisconnectJump network_restore_failed=true "
                        f"reason={gate.last_error or 'unknown'}",
                        flush=True,
                    )
                    return False
                gate = GameNetworkGate(self._adb_shell)
                if not gate.block():
                    print(
                        "VersusDisconnectJump gate_block_failed=true "
                        f"reason={gate.last_error or 'unknown'}",
                        flush=True,
                    )
                    return False
                print(
                    "VersusDisconnectJump gate_backend=iptables-owner",
                    flush=True,
                )
            # 退后台再切回；网络隔离状态下切回会触发“是否切换到单人演奏”弹窗。
            self.controller.post_click_key(3).wait()
            time.sleep(0.6)
            self.controller.post_start_app(GAME_PACKAGE).wait()
            if not self._wait_and_click(
                "disconnect_continue_body",
                DISCONNECT_CONTINUE_INTERRUPT_POINT,
                popup_timeout_s,
            ):
                print(
                    "VersusDisconnectJump continue_popup_missed=true",
                    flush=True,
                )
                return False
            if not self._wait_and_click(
                "disconnect_confirm_body",
                DISCONNECT_CONFIRM_INTERRUPT_POINT,
                10.0,
            ):
                print(
                    "VersusDisconnectJump confirm_popup_missed=true",
                    flush=True,
                )
                return False
            # 先恢复网络，再对“连接失败。”做有界重试；网络隔离状态下点重试
            # 无效是游戏正常表现。
            if not gate.restore():
                print(
                    "VersusDisconnectJump network_restore_failed=true",
                    flush=True,
                )
                return False
            time.sleep(0.8)
            self.dismiss_connect_failed()
            print("VersusDisconnectJump completed=true", flush=True)
            return True
        finally:
            gate.restore()

    def ensure_room_page(self, timeout: float = 15.0) -> np.ndarray:
        state, image = self.wait_for(("room_search",), timeout=timeout)
        if state is None:
            raise RuntimeError("未识别竞演房间选择页")
        return image

    def select_room_type(self) -> None:
        """房间选择页选好房型卡后点右下「确定」入房。

        竞演与协力不同：不是加入随机房，而是自己建房等人满自动开始。房间选择页
        只有两张卡（activity=0 / free=1），滑到目标卡居中即可；入房按钮是右下
        「确定」，不是「创建房间」（后者是手动建带房间号的房，会卡在设置弹窗）。
        """
        started = time.monotonic()
        image = self.ensure_room_page()
        target = str(self.settings["room_type"])
        if target not in ROOM_TYPE_INDEX:
            raise ValueError(f"不支持的竞演房型：{target}")
        actual = classify_room_type(image)
        print(
            "VersusLive select_room reached_selection "
            f"target={target} actual={actual or 'unknown'} "
            f"elapsed={time.monotonic() - started:.2f}s",
            flush=True,
        )
        # activity 与 free 是轮播的两个端点：朝目标端点方向滑，未到位就同向重复，
        # 不先反向绕路；未知居中卡时朝目标端点滑一次，再由下一帧判向。
        swipes = 0
        for _ in range(4):
            if actual == target:
                break
            if target == "activity":
                start, end, duration = (250, 360), (1050, 360), 500
            elif target == "free":
                start, end, duration = (1050, 360), (250, 360), 500
            elif actual in ROOM_TYPE_INDEX:
                moving_right = (
                    ROOM_TYPE_INDEX[target] > ROOM_TYPE_INDEX[actual]
                )
                start, end, duration = (
                    ((820, 360), (455, 360), 280)
                    if moving_right
                    else ((455, 360), (820, 360), 280)
                )
            else:
                if ROOM_TYPE_INDEX[target] >= 1:
                    start, end, duration = (250, 360), (1050, 360), 500
                else:
                    start, end, duration = (1050, 360), (250, 360), 500
            _maa_swipe(self.context, start, end, duration)
            swipes += 1
            time.sleep(0.45)
            image = self.capture()
            actual = classify_room_type(image)

        if actual != target:
            raise RuntimeError(
                f"竞演房型复核失败：期望 {target}，识别为 {actual or 'unknown'}"
            )
        print(
            "VersusLive select_room ready_to_confirm "
            f"target={target} swipes={swipes} "
            f"elapsed={time.monotonic() - started:.2f}s",
            flush=True,
        )
        self.click(VERSUS_CONFIRM_POINT)
        self.verify_room_entry(
            "点击「确定」后仍停留在房间选择页，未进入房间",
            confirm_point=VERSUS_CONFIRM_POINT,
        )
        print(
            "VersusLive select_room room_entry_confirmed "
            f"elapsed={time.monotonic() - started:.2f}s",
            flush=True,
        )

    def verify_room_entry(
        self, failure_reason: str, *, confirm_point: tuple[int, int] | None = None,
    ) -> None:
        trial = getattr(self, "room_recovery_trial", False)
        started = time.monotonic()
        deadline = started + (45.0 if trial else 30.0)
        next_reclick_at = started + 12.0
        reclicks = 0
        departed_frames = 0
        while time.monotonic() < deadline:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            image = self.capture()
            check_preplay_connection_failed(self, image)
            if self.visible(image, "member_exit_title", 0.93):
                raise MemberExited("竞演成员退出房间")
            if any(
                self.visible(image, name)
                for name in ("room_together", "song_unspecified", "ready_button")
            ):
                print(
                    "VersusLive room_entry=confirmed marker=known-lobby",
                    flush=True,
                )
                return
            if self.visible(image, "room_search"):
                departed_frames = 0
                # 仅仍停在选择页时才允许重按「确定」，加载页不能复用该坐标。
                if trial and confirm_point is not None and reclicks < 2 and time.monotonic() >= next_reclick_at:
                    reclicks += 1
                    next_reclick_at = time.monotonic() + 12.0
                    print(f"VersusLive room_entry=reclick attempt={reclicks}/2", flush=True)
                    self.click(confirm_point)
            else:
                departed_frames += 1
                if departed_frames >= 3:
                    # 匹配/加载/成员收集页都是瞬态且随账号网络变化；稳定离开
                    # 选择页即视为进入，更长的推进判断交给 wait_for_preparation。
                    print(
                        "VersusLive room_entry=confirmed "
                        "marker=left-room-selection",
                        flush=True,
                    )
                    return
            time.sleep(0.25)
        raise RoomEntryTransient(failure_reason)

    def enter_room(self) -> None:
        # 竞演目前只支持「选房型卡 + 确定」入房。
        self.select_room_type()

    def pause_for_song_filter(self) -> str | None:
        """留出筛歌窗口，同时观察停止、成员退出和玩家提前确认。"""
        deadline = time.monotonic() + VERSUS_SONG_CHOICE_PAUSE_SECONDS
        while True:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            state, _ = self.wait_for(
                ("song_unspecified", "ready_button"), timeout=0.0,
            )
            if state == "ready_button":
                return state
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # 后续输入只消费最新画面；选曲页消失时由外层继续观察。
                return state
            time.sleep(min(0.1, remaining))

    def wait_for_preparation(self) -> None:
        choice_deadline = (
            time.monotonic() + ROOM_SONG_CHOICE_TIMEOUT_SECONDS
        )
        while time.monotonic() < choice_deadline:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            state, _ = self.wait_for(
                ("song_unspecified", "ready_button"),
                timeout=min(
                    3.0,
                    max(0.1, choice_deadline - time.monotonic()),
                ),
            )
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            if state == "ready_button":
                # 首轮已提前确认时不把筛歌窗口顺延到后续轮次。
                self.song_choice_pause_pending = False
                return
            if state == "song_unspecified":
                song_choice = str(
                    getattr(self, "settings", {}).get("song_choice", "unspecified")
                )
                if (
                    song_choice != "unspecified"
                    and getattr(self, "song_choice_pause_pending", False)
                ):
                    self.song_choice_pause_pending = False
                    print(
                        "VersusLive song_choice_pause "
                        f"seconds={VERSUS_SONG_CHOICE_PAUSE_SECONDS} choice={song_choice}",
                        flush=True,
                    )
                    state = self.pause_for_song_filter()
                    if self.stopped():
                        raise InterruptedError("用户已停止任务")
                    if state == "ready_button":
                        return
                    if state != "song_unspecified":
                        continue
                select_song_and_wait(
                    self, song_choice,
                    random_point=VERSUS_SONG_RANDOM_POINT,
                    unspecified_point=VERSUS_SONG_UNSPECIFIED_POINT,
                    confirm_point=VERSUS_SONG_CONFIRM_POINT,
                    timeout=SONG_CHOICE_TO_READY_TIMEOUT_SECONDS, label="竞演",
                )
                print(f"VersusLive song_choice={song_choice}", flush=True)
                return
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        raise RoomPreparationTimeout("进入竞演房间后180秒内未出现不指定歌曲或准备页")

    def action_argv(self, params: dict[str, object]):
        return SimpleNamespace(
            custom_action_param=json.dumps(params, ensure_ascii=False),
            task_detail=getattr(getattr(self, "stamina_argv", None), "task_detail", None),
        )

    def prepare(self) -> None:
        difficulty = str(self.settings["difficulty"])
        difficulty_params = {
            "difficulty": difficulty,
            "max_attempts": 3,
            "verify_delay_seconds": 0.25,
            "identity_read_attempts": 2,
            "identity_retry_delay_seconds": 0.15,
            "difficulty_targets": VERSUS_DIFFICULTY_TARGETS,
            "song_level_roi": (130, 580, 56, 38),
            "song_title_roi": (105, 535, 290, 52),
            "song_identity": False,
            "mode": "versus",
            "debug_recording": bool(self.settings["debug_recording"]),
        }
        if difficulty == "Special":
            # 竞演歌曲由房间决定；Special 不存在时显式回退 Expert，后续流程
            # 必须只消费实际选中的难度，不能继续拿 Special 谱面演奏。
            difficulty_params["fallback_difficulties"] = ["Expert"]
        if not RealtimeDifficultySelect().run(
            self.context, self.action_argv(difficulty_params)
        ):
            raise RuntimeError(f"竞演准备页未能选择并复核 {difficulty} 难度")
        run = current_live_run()
        if run is None or not run.prepared_for_play:
            raise RuntimeError("竞演难度选择成功但缺少本局实际难度证据")
        effective_difficulty = str(run.difficulty)
        if effective_difficulty != difficulty and not (
            difficulty == "Special" and effective_difficulty == "Expert"
        ):
            raise RuntimeError(
                "竞演实际难度不符合回退策略："
                f"请求 {difficulty}，实际 {effective_difficulty}"
            )
        self.effective_difficulty = effective_difficulty

        performance_params = {
            "difficulty": effective_difficulty,
            "require_profile": True,
            "dpi": VERSUS_DPI,
            "game_fps": VERSUS_GAME_FPS,
            "render_quality": VERSUS_RENDER_QUALITY,
            "coordinates": {"gear": (946, 650)},
            "defer_native_prearm": True,
            "cache_preparation_image": True,
        }
        if not RealtimePerformanceSettingsGate().run(
            self.context, self.action_argv(performance_params)
        ):
            raise RuntimeError("竞演准备页流速复核失败")
        run = current_live_run()
        initial_image = None if run is None else run.cooperative_prestart_image
        if initial_image is not None:
            self._last_capture_timestamp = getattr(run, "cooperative_prestart_captured_at", None)
        ready_image = self.ensure_performance_mode_off(
            initial_image=initial_image,
        )
        ready_transition = self.ready_up_and_verify(initial_image=ready_image)
        if ready_transition != "black":
            self.watch_member_exit_before_black()
        print(
            "VersusLive ready=true "
            f"requested_difficulty={difficulty} "
            f"effective_difficulty={effective_difficulty} "
            "speed_gate=verified",
            flush=True,
        )

    def ensure_performance_mode_off(
        self,
        *,
        initial_image: np.ndarray | None = None,
    ) -> np.ndarray | None:
        """竞演房间页关闭 3D/MV 演出表现，防止演出场背景变化提前触发谱面。

        房间页左下角与单人准备页同布局：循环箭头切换按钮位于
        ``MODE_TOGGLE_POINT``，其右侧标签显示当前模式。标签区域读不到
        强饱和色即视为 OFF。点击后仍无法确认关闭（例如界面改版或坐标
        漂移）时保留证据并失败，禁止在表现仍开启时点击准备。
        """
        image = initial_image
        for attempt in range(4):
            reused = image is not None
            if image is None:
                image = self.capture()
            if live_performance_mode_is_off(image):
                print(
                    "VersusLive performance_mode=off confirmed=true "
                    f"reused_preparation_image={str(reused).lower()}",
                    flush=True,
                )
                return image
            if attempt == 0:
                self._save_performance_mode_evidence(image, "before")
            self.click(MODE_TOGGLE_POINT)
            time.sleep(0.6)
            image = None
        try:
            self._save_performance_mode_evidence(self.capture(), "after")
        except InterruptedError:
            raise
        print(
            "VersusLive performance_mode=off confirmed=false "
            "action=fail-closed attempts=4",
            flush=True,
        )
        raise RuntimeError("竞演准备页无法确认演出表现 OFF，未点击准备")

    def _save_performance_mode_evidence(self, image: np.ndarray, stage: str) -> None:
        try:
            evidence_dir = PROJECT_ROOT / "debug"
            evidence_dir.mkdir(parents=True, exist_ok=True)
            path = evidence_dir / (
                "versus-performance-mode-"
                f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{stage}.png"
            )
            imwrite_unicode(path, image)
            print(f"VersusLive performance_mode_evidence={path}", flush=True)
        except Exception as exc:  # noqa: BLE001 - 证据失败不阻断演出流程
            print(
                "VersusLive performance_mode_evidence_failed="
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

    def ready_up_and_verify(
        self,
        *,
        initial_image: np.ndarray | None = None,
    ) -> str:
        """点击“准备完毕”并确认按钮消失，防止触控未送达造成空演奏。"""
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        self._start_startup_diagnostics()
        image = initial_image
        for attempt in range(3):
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            reused = image is not None
            if image is None:
                image = self.capture()
            box = self.template_box(image, "ready_button", 0.90)
            if box is None and reused:
                # 缓存帧只用于加速肯定匹配；若没看到按钮，必须再采一张新图，
                # 避免拿过期画面把“按钮缺失”误判成已经准备完毕。
                image = self.capture()
                reused = False
                box = self.template_box(image, "ready_button", 0.90)
            self._startup_record(image, "ready-before-click", attempt=attempt + 1,
                                 ready_visible=box is not None, cached_preparation=reused)
            if box is None:
                # 按钮已消失：已进入准备完毕/成员等待或加载流程。
                print(
                    f"VersusLive ready=confirmed attempt={attempt + 1}",
                    flush=True,
                )
                return "already-confirmed"
            left, top, width, height = box
            print(
                "VersusLive ready_click "
                f"attempt={attempt + 1} "
                f"reused_preparation_image={str(reused).lower()}",
                flush=True,
            )
            self.click((left + width // 2, top + height // 2))
            delivery = self.watch_ready_delivery_after_click()
            if delivery != "still-visible":
                print(
                    "VersusLive ready=confirmed "
                    f"attempt={attempt + 1} delivery={delivery}",
                    flush=True,
                )
                return delivery
            image = None
        raise RuntimeError("点击准备完毕后按钮仍在，触控可能未送达")

    def watch_ready_delivery_after_click(
        self,
        timeout: float = READY_DELIVERY_OBSERVE_SECONDS,
    ) -> str:
        """点击后高频观察送达，避免固定睡眠吞掉短黑场转场。"""
        started_at = time.monotonic()
        deadline = started_at + float(timeout)
        while time.monotonic() < deadline:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            image = self.capture()
            check_preplay_connection_failed(self, image)
            if self.visible(image, "member_exit_title", 0.93):
                self._startup_record(image, "ready-delivery", member_exit=True)
                self.dismiss_member_exit()
                raise MemberExited("竞演成员退出房间")
            if _frame_is_black_transition(image):
                outcome = "black"
            elif self.template_box(image, "ready_button", 0.90) is None:
                outcome = "button-gone"
            else:
                self._startup_record(image, "ready-delivery", ready_visible=True, black=False)
                time.sleep(0.05)
                continue
            elapsed_ms = (time.monotonic() - started_at) * 1000.0
            self._startup_record(image, "ready-delivery", outcome=outcome, black=outcome == "black",
                                 ready_visible=False, elapsed_ms=elapsed_ms)
            print(
                "VersusLive ready_delivery "
                f"outcome={outcome} elapsed_ms={elapsed_ms:.1f} "
                f"timeout_s={float(timeout):.3f}",
                flush=True,
            )
            return outcome
        print(
            "VersusLive ready_delivery outcome=still-visible "
            f"elapsed_ms={(time.monotonic() - started_at) * 1000.0:.1f} "
            f"timeout_s={float(timeout):.3f}",
            flush=True,
        )
        return "still-visible"

    def make_final_cover_entry_resolver(self) -> FinalCoverResolver | None:
        """构造准备后封面转场识别器，只接受与本局身份一致的稳定封面。"""
        run = current_live_run()
        if run is None:
            return None
        return FinalCoverResolver(
            difficulty=run.difficulty,
            observed_level=run.song_level,
            observed_title=run.song_title,
            observed_title_confidence=float(run.song_title_confidence or 0.0),
            repository=LocalChartRepository(
                PROJECT_ROOT / "resource" / "charts"
            ),
            reject_member_loading=self.settings["versus_member_loading_guard_enabled"],
        )

    def watch_member_exit_before_black(
        self,
        timeout: float = MEMBER_DOWNLOAD_TIMEOUT_SECONDS,
    ) -> str:
        """准备完毕到黑场转场之间的成员退出弹窗窗口。

        点击“准备完毕”后、进入演奏的整屏黑场之前，其他成员退出时仍会弹出
        “错误/由于XX退出房间。”；此时已离开房间等待页，常规 wait_for 的
        成员退出检测不再覆盖，弹窗会挡住转场导致整局卡死。这里高频轮询到
        黑场出现为止：看到弹窗就点“确定”并按成员退出策略处理；看到黑场
        说明转场已开始，弹窗不再可能，立即退出本窗口。若短黑场漏检，连续
        两帧稳定且能由本局难度、等级、标题共同确认的最终封面也可证明正常
        转场，并把该证据直接交给演奏入口。加载页和准备页可能误中生命条、
        判定线及局部动态，不能抢先结束封面检测；60 秒超时仍安全跳车并停止。
        """
        # 每局准备后窗口都从空白动态基线开始，绝不能让上一局未完成的局部
        # 变化跨局累积成“已错过转场”的第二次证据。
        self.playfield_entry_evidence.reset()
        final_cover_resolver = self.make_final_cover_entry_resolver()
        started_at = time.monotonic()
        deadline = started_at + float(timeout)

        def finish(outcome: str) -> str:
            elapsed_ms = (time.monotonic() - started_at) * 1000.0
            print(
                "VersusLive ready_transition "
                f"outcome={outcome} elapsed_ms={elapsed_ms:.1f} "
                f"timeout_s={float(timeout):.3f}",
                flush=True,
            )
            return outcome

        while time.monotonic() < deadline:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            image = self.capture()
            if _frame_is_black_transition(image):
                self._startup_record(image, "ready-transition", black=True, outcome="black")
                # 整屏黑场转场已经开始，成员退出弹窗窗口已过。
                return finish("black")
            check_preplay_connection_failed(self, image)
            if self.visible(image, "member_exit_title", 0.93):
                self._startup_record(image, "ready-transition", member_exit=True)
                finish("member-exit")
                self.dismiss_member_exit()
                raise MemberExited("竞演成员退出房间")
            if final_cover_resolver is not None:
                resolve_started_at = time.monotonic()
                try:
                    cover_resolution = final_cover_resolver.observe(image)
                except Exception as exc:
                    self._startup_record(image, "ready-transition", **cover_diagnostic(final_cover_resolver),
                                         error=f"{type(exc).__name__}: {exc}")
                    raise
                resolver_ms = (time.monotonic() - resolve_started_at) * 1000
                self._startup_record(image, "ready-transition", **cover_diagnostic(final_cover_resolver),
                                     black=False, confirmed=cover_resolution is not None,
                                     playfield_visible=(self._startup_playfield_visible(image)
                                                        if cover_resolution is None else None),
                                     resolver_ms=resolver_ms)
                if cover_resolution is not None:
                    update_live_run(
                        startup_final_cover_image=image.copy(),
                        startup_final_cover_resolution=cover_resolution,
                    )
                    return finish("final-cover")
            else:
                self._startup_record(image, "ready-transition", black=False, reason="missing resolver")
            # 封面未确认时持续检测；成员加载与局部动画不能替代本局歌曲身份。
            self.playfield_entry_evidence.reset()
            time.sleep(0.1)
        finish("timeout")
        self.jump_after_download_timeout()

    def wait_for_life_depleted_after_missed_transition(
        self,
        initial_image: np.ndarray,
        *,
        timeout: float = MEMBER_DOWNLOAD_TIMEOUT_SECONDS,
    ) -> None:
        """错过黑场后只监控生命归零，绝不在歌曲中段启动演奏引擎。"""
        guard = LifeGuard(confirm_frames=3)
        started_at = time.monotonic()
        deadline = started_at + float(timeout)
        image = initial_image
        visible_samples = 0
        invisible_samples = 0

        while time.monotonic() < deadline:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            reading = self.detector.detect(image)
            if reading.visible:
                visible_samples += 1
            else:
                invisible_samples += 1
            status = guard.update(reading)
            if status is LifeStatus.DEAD:
                print(
                    "VersusLive missed_transition_life_monitor "
                    "outcome=life-depleted "
                    f"elapsed_ms={(time.monotonic() - started_at) * 1000.0:.1f} "
                    f"minimum_value={guard.minimum} "
                    f"visible_samples={visible_samples} "
                    f"invisible_samples={invisible_samples}",
                    flush=True,
                )
                self.jump_after_startup_failure(
                    "准备完毕后错过开演转场，已确认生命归零并退后台返回游戏"
                )
            time.sleep(0.2)
            image = self.capture()

        print(
            "VersusLive missed_transition_life_monitor "
            "outcome=timeout "
            f"elapsed_ms={(time.monotonic() - started_at) * 1000.0:.1f} "
            f"minimum_value={guard.minimum} "
            f"alive_confirmed={guard.alive_confirmed} "
            f"visible_samples={visible_samples} "
            f"invisible_samples={invisible_samples}",
            flush=True,
        )
        self.jump_after_startup_failure(
            "准备完毕后错过开演转场，生命监控超时，已退后台返回游戏"
        )

    def jump_after_startup_failure(self, reason: str) -> None:
        """从无法安全启动演奏的竞演局退后台并返回游戏，然后停止任务。"""
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        require_game_foreground(self.controller)
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        self.controller.post_click_key(3).wait()
        time.sleep(0.6)
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        self.controller.post_start_app(GAME_PACKAGE).wait()
        deadline = time.monotonic() + 12.0
        foreground_confirmed = False
        while time.monotonic() < deadline:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            if foreground_package(self.controller) == GAME_PACKAGE:
                foreground_confirmed = True
                break
            time.sleep(0.5)
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        reason = f"{reason}；请手动跳车"
        record_failure_reason(reason)
        print(
            "VersusLive startup_jump "
            f"foreground_confirmed={str(foreground_confirmed).lower()} "
            f"reason={reason}",
            flush=True,
        )
        raise JumpOutUnavailable(reason)

    def jump_after_download_timeout(self) -> None:
        reason = "成员下载超过60秒仍未进入演出，已退后台并尝试返回游戏"
        self.jump_after_startup_failure(reason)

    def wait_for_playfield(self) -> None:
        state, _ = self.wait_for(
            ("playfield",),
            timeout=MEMBER_DOWNLOAD_TIMEOUT_SECONDS,
            interval=0.2,
        )
        if state != "playfield":
            self.jump_after_download_timeout()
        print("VersusLive member_download=complete playfield_visible=true", flush=True)

    def play(self) -> bool:
        params = versus_play_params(
            self.settings,
            effective_difficulty=getattr(
                self,
                "effective_difficulty",
                str(self.settings.get("difficulty", "Expert")),
            ),
        )
        success = RealtimeProfilePlay().run_for_flow(
            self.context, self.action_argv(params)
        )
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        run = current_live_run()
        if run is not None and bool(run.disconnect_jump_requested):
            # 生命归零：不再自动跳车（门禁/弹窗在不同设备上不可靠）。
            # 回主页 → 切回游戏 → 直接结束任务，提示用户手动跳车。
            self.controller.post_click_key(3).wait()
            time.sleep(0.6)
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            self.controller.post_start_app(GAME_PACKAGE).wait()
            time.sleep(0.8)
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            raise JumpOutUnavailable("生命归零，请手动跳车后重试")
        if not success:
            return False
        return True

    def wait_for_post_score_destination(
        self,
        names: tuple[str, ...],
        *,
        timeout: float,
    ) -> str | None:
        """Recognise a result exit before any post-Back corner tap.

        Versus result pages do not have a fixed count.  ``room_search``
        and ``live_entry`` are local templates; the home marker reuses the
        same pipeline recogniser as task startup so its proven 0.82 threshold
        remains the single source of truth.
        """
        # 演出结束后的结算页面不会再出现“成员退出”弹窗；此处关闭该检查，
        # 避免结算导航被残留模板命中打断，把弹窗处理限制在房间/准备阶段。
        # 一帧同时检查出口和剧情，不为尚未到达的房间页空等两轮超时。
        self._post_score_refresh = True
        try:
            state, image = self.wait_for(
                names, timeout=0.0, detect_member_exit=False,
            )
        finally:
            self._post_score_refresh = False
        if state is not None:
            self._quit_cancel_home_pending = False
            return state
        if self.pipeline_box(image, "VersusHomeMarker") is not None:
            self._quit_cancel_home_pending = False
            return "home"
        # 该确认框提供主页到达证据；取消后复核弹窗消失，再按主页终点收尾。
        # 若仍返回剧情状态且主页模板漏识别，外层可能再次按 BACK 打开弹窗。
        quit_box = self.pipeline_box(image, "QuitConfirmCancel")
        if quit_box is None and getattr(self, "_quit_cancel_home_pending", False):
            # 取消动画可能跨越多帧；弹窗消失后消费之前的主页证据，期间不发 BACK。
            self._quit_cancel_home_pending = False
            return "home"
        if quit_box is not None:
            self.click(
                (
                    int(quit_box.x + quit_box.w // 2),
                    int(quit_box.y + quit_box.h // 2),
                )
            )
            self._quit_cancel_home_pending = True
            self._post_score_refresh = True
            try:
                after_cancel = self.capture()
            finally:
                self._post_score_refresh = False
            if self.pipeline_box(after_cancel, "QuitConfirmCancel") is not None:
                return "story"
            self._quit_cancel_home_pending = False
            return "home"
        if handle_story_page(
            image, recognise=self.pipeline_box, click=self.click,
            stopping=self.stopped,
        ):
            return "story"
        return None

    def advance_post_score_once(
        self,
        names: tuple[str, ...],
        *,
        inspect_timeout: float,
    ) -> str | None:
        """完整执行安全像素→BACK→安全像素后再识别终点。"""
        def before_input() -> None:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            require_game_foreground(self.controller)

        accelerated_back(
            lambda: self.controller,
            before_input=before_input,
            phase="post-score",
            log_prefix="VersusResult",
        )
        return self.wait_for_post_score_destination(
            names,
            timeout=inspect_timeout,
        )

    def navigate_to_versus_room_selection(self, origin: str) -> None:
        """Explicitly recover Home/live-select into versus room select."""
        deadline = time.monotonic() + 30.0
        next_home_click_at = 0.0
        stamina_home_allocated = False
        next_entry_click_at = 0.0
        while time.monotonic() < deadline:
            image = self.capture()
            if self.visible(image, "room_search"):
                print(
                    "VersusLive state=room-selection "
                    f"reentry_from={origin} confirmed=true",
                    flush=True,
                )
                return

            quit_box = self.pipeline_box(image, "QuitConfirmCancel")
            if quit_box is not None:
                # 弹窗会挡住主页“演出”按钮；点取消后再重试导航。
                self.click(
                    (
                        int(quit_box.x + quit_box.w // 2),
                        int(quit_box.y + quit_box.h // 2),
                    )
                )
                time.sleep(0.5)
                continue

            close_box = self.pipeline_box(image, "VersusNavigationClose")
            if close_box is not None:
                self.click(
                    (
                        int(close_box.x + close_box.w // 2),
                        int(close_box.y + close_box.h // 2),
                    )
                )
                time.sleep(0.5)
                continue

            if self.pipeline_box(image, "VersusHomeMarker") is not None:
                now = time.monotonic()
                if now >= next_home_click_at:
                    if not stamina_home_allocated:
                        allocation_started = time.monotonic()
                        allocate_flow_home(self)
                        # 补充有自己的受限等待，不侵占原有主页到房间选择页的导航预算。
                        deadline += max(0.0,time.monotonic()-allocation_started)
                        stamina_home_allocated = True
                        now = time.monotonic()
                    self.click(HOME_LIVE_POINT)
                    next_home_click_at = now + 2.0
                    print(
                        "VersusLive state=home action=open-live-for-next-round",
                        flush=True,
                    )
                time.sleep(0.35)
                continue

            entry_box = self.template_box(image, "live_entry")
            if entry_box is not None:
                now = time.monotonic()
                if now >= next_entry_click_at:
                    x, y, width, height = entry_box
                    self.click((x + width // 2, y + height // 2))
                    next_entry_click_at = now + 2.0
                    print(
                        "VersusLive state=live-select "
                        "action=open-versus-for-next-round",
                        flush=True,
                    )
                time.sleep(0.35)
                continue

            time.sleep(0.35)
        raise RuntimeError(
            "已离开竞演结算，但30秒内未能重新进入竞演房间选择页"
        )

    def return_to_room_selection(self) -> None:
        deadline = time.monotonic() + POST_SCORE_NAVIGATION_TIMEOUT_SECONDS
        attempts = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("竞演结算推进60秒后仍未返回房间选择页面")
            state = self.wait_for_post_score_destination(
                ("room_search", "live_entry"),
                timeout=min(2.0, remaining),
            )
            if state == "story":
                continue
            if state == "room_search":
                print(
                    "VersusLive state=room-selection "
                    "result_navigation=complete",
                    flush=True,
                )
                return
            if state in {"home", "live_entry"}:
                self.navigate_to_versus_room_selection(state)
                return

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("竞演结算推进60秒后仍未返回房间选择页面")
            state = self.advance_post_score_once(
                ("room_search", "live_entry"),
                inspect_timeout=min(2.0, remaining),
            )
            attempts += 1
            print(
                "VersusLive state=post-score "
                "action=corner-back-corner-recognise"
                f" attempt={attempts}",
                flush=True,
            )
            if state == "room_search":
                print(
                    "VersusLive state=room-selection "
                    "result_navigation=complete",
                    flush=True,
                )
                return
            if state in {"home", "live_entry"}:
                self.navigate_to_versus_room_selection(state)
                return

    def stay_in_room(self) -> None:
        # 竞演不做房间复用，打完一局由 return_to_room_selection 重进，不支持
        # 留在同一房间复用。should_stay_in_room 恒为 False，正常不会走到这里。
        raise RuntimeError("竞演演出不支持留在同一房间")

    def run_attempt(self, reuse_room: bool = False) -> bool:
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        # 入场可能在 prepare 之前失败，必须先隔离上局的身份、完成和录像凭据。
        requested = str(self.settings.get("difficulty", "Expert"))
        reset_live_run(mode="versus", difficulty=requested,
            requested_difficulty=requested, prepared_for_play=False)
        self._room_entry_evidence = None
        try:
            task_id = getattr(self, "task_id", None)
            if task_id is None:
                task_id = getattr(getattr(getattr(self, "stamina_argv", None),
                    "task_detail", None), "task_id", None)
            self._room_entry_evidence = RoomEntryEvidence(
                self.settings, "versus", task_id, TEMPLATE_POSITIONS,
            )
        except Exception as exc:
            print(f"RoomEntry diagnostics_warning={type(exc).__name__}: {exc}", flush=True)
        self._room_entry_wait_pending = True
        outcome = "failed"
        try:
            if not reuse_room:
                self.enter_room()
            if self._room_entry_evidence is not None:
                self._room_entry_evidence.phase = "song-choice"
            self.wait_for_preparation()
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            self._room_entry_wait_pending = False
            self.prepare()
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            success = self.play()
            outcome = "completed" if success else "failed"
            return success
        except InterruptedError:
            outcome = "stopped"
            raise
        except (MemberExited, JumpOutUnavailable, RoomEntryTransient):
            mark_uncertain(getattr(self, "stamina_argv", None))
            outcome = "stopped" if self.stopped() else "startup-failed"
            raise
        except FinalCoverStartupFailure:
            mark_uncertain(getattr(self, "stamina_argv", None))
            # 已定性的启动失败不能再凭另一张弹窗截图转换成成员退出或重连。
            if self.stopped():
                outcome = "stopped"
                raise InterruptedError("用户已停止任务")
            raise
        except Exception as exc:
            if self.stopped():
                outcome = "stopped"
                raise InterruptedError("用户已停止任务") from exc
            mark_uncertain(getattr(self, "stamina_argv", None))
            # 只接收显式房间观察窗口产生的 MemberExited；普通准备/演奏异常
            # 必须保留最初完整性原因，禁止再用通用“错误”标题改写分类。
            raise
        finally:
            recorder = getattr(self, "_startup_recorder", None)
            if recorder is not None:
                try:
                    recorder.close(outcome)
                except Exception as exc:
                    print(f"VersusStartup diagnostics_warning={type(exc).__name__}: {exc}", flush=True)
                self._startup_recorder = None
            try:
                if self._room_entry_evidence is not None:
                    self._room_entry_evidence.close(PROJECT_ROOT / "debug" / "recordings", outcome)
            except Exception as exc:
                print(f"RoomEntry diagnostics_warning={type(exc).__name__}: {exc}", flush=True)
            finally:
                self._room_entry_evidence = None

    def recover_to_home(self, reason: str, *, restart_limit: int = 2) -> None:
        """完整清理失败单局并把游戏恢复到主页；不重进竞演房间。"""
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        discard_prearmed_backend("versus-play-retry")
        recovery_params = {
            "home_node": "VersusHomeMarker",
            "modal_cancel_nodes": ["QuitConfirmCancel"],
            "click_nodes": [
                "AutoLiveLoginTap",
                "AutoLiveLoginNext",
                "AutoLiveCommonClose",
                "AutoLiveStorySkipConfirmLarge",
                "AutoLiveStorySkipConfirm",
                "AutoLiveStorySkip",
                "AutoLiveStoryMenu",
            ],
            "escape_interval_ms": 1500,
            "escape_timeout_ms": 60000,
            "restart_limit": restart_limit,
            "restart_wait_ms": 5000,
            "startup_grace_ms": 12000,
            "login_start_node": "AutoLiveLoginScreenMarker",
            "login_start_target": [640, 635],
            "login_marker_priority_attempts": 3,
            "escape_after_login_start": True,
            "package": GAME_PACKAGE,
        }
        argv = SimpleNamespace(
            custom_action_param=json.dumps(
                recovery_params,
                ensure_ascii=False,
            )
        )
        recovered = CommonRecover().run(self.context, argv)
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        if not recovered:
            raise RuntimeError(
                f"竞演单局失败后无法恢复主页：{reason}"
            )

    def recover_after_play_failure(self, reason: str, *, restart_limit: int = 2) -> None:
        """完整清理失败单局并从主页重新进入竞演，禁止在旧会话中续跑。"""
        self.recover_to_home(reason, restart_limit=restart_limit)
        self.navigate_to_versus_room_selection("home")

    def verify_preplay_recovery(self):
        wait_for_preplay_recovery_page(self, "room_together", "VersusHomeMarker")

    def recover_room_preparation_timeout(self, reason: str) -> None:
        """候选路径最多重启一次；必须确认主页及房间选择页后才能重入。"""
        discard_prearmed_backend("preplay-room-recovery")
        self.verify_preplay_recovery()
        self.recover_after_play_failure(reason, restart_limit=1)

    def stop_after_room_timeout(self, reason: str) -> None:
        """房间准备重试预算耗尽后，先尽力把游戏恢复到主页再结束任务。

        直接结束会把机器人留在竞演房间里；这里补一次主页恢复，失败只记录
        警告，不改变既有的失败结论。
        """
        try:
            self.recover_to_home(reason, restart_limit=1)
        except InterruptedError:
            raise
        except Exception as exc:
            print(
                "VersusLive stop_recovery_warning="
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

    def handle_member_exit(self, reconnects: int) -> int:
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        # 兼容旧 fail/max_reconnects 保存值，但成员退出不再终止尚未完成的任务。
        discard_prearmed_backend("versus-member-exit")
        try:
            self.dismiss_member_exit()
        except RoomEntryTransient as exc:
            # 弹窗在分类与再次截图间变化时仍按明确连接失败恢复，不能逃出外层重试。
            self.recover_room_preparation_timeout(str(exc))
            return reconnects + 1
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        self.verify_preplay_recovery()
        reconnects += 1
        print(f"VersusLive member_exit=reconnect attempt={reconnects}", flush=True)
        # 成员退出弹窗关闭后，游戏往往还停留在结算页，不能直接要求
        # “房间选择页”出现。先把剩余结算页推进回房间/主页；仍失败则走
        # 主页恢复再重进，避免因为识别不到房间页把整个任务报错停掉。
        try:
            self.return_to_room_selection()
        except MemberExited:
            try:
                self.recover_room_preparation_timeout("成员退出弹窗反复出现")
            except InterruptedError:
                raise
            except Exception as recovery_error:
                raise RuntimeError(
                    "成员退出后恢复失败："
                    f"{type(recovery_error).__name__}: {recovery_error}"
                ) from recovery_error
        except InterruptedError:
            raise
        except Exception as exc:
            try:
                self.recover_room_preparation_timeout(
                    f"成员退出后未回到房间：{type(exc).__name__}: {exc}"
                )
            except InterruptedError:
                raise
            except Exception as recovery_error:
                raise RuntimeError(
                    "成员退出后恢复失败："
                    f"{type(recovery_error).__name__}: {recovery_error}"
                ) from recovery_error
        return reconnects

    def run(self) -> bool:
        total = int(self.settings.get("count", 1))
        completed = 0
        reconnects = 0
        reuse_room = False
        play_failures = 0
        room_timeouts = 0
        retry_count = max(
            0,
            min(99, int(self.settings.get("play_failure_retry_count", 0))),
        )
        def recover_completed_round(reason):
            try:
                self.recover_after_play_failure(reason)
            except InterruptedError:
                raise
            except Exception as exc:
                print(f"VersusLive post_result_warning={type(exc).__name__}: {exc}", flush=True)

        while total == 0 or completed < total:
            if self.context.tasker.stopping:
                return True
            print(
                f"VersusLive round={completed + 1}/{total or '无限'} "
                f"reuse_room={str(reuse_room).lower()}",
                flush=True,
            )
            try:
                mark_stamina_settled(getattr(self, "stamina_argv", None), completed)
                if prepare_stamina_round(self, None if total == 0 else total-completed):
                    reuse_room = False
                success = self.run_attempt(reuse_room=reuse_room)
                if not success:
                    mark_uncertain(getattr(self, "stamina_argv", None))
            except StaminaTaskError as exc:
                if self.stopped():
                    return True
                record_failure_reason(str(exc))
                return False
            except MemberExited:
                if self.stopped():
                    return True
                next_reconnects = self.handle_member_exit(reconnects)
                if next_reconnects is None:
                    return False
                reconnects = next_reconnects
                rematch_backoff(self, reconnects)
                reuse_room = False
                continue
            except JumpOutUnavailable as exc:
                if self.stopped():
                    return True
                # 已执行安全跳车，或现有自动化无法继续处理时直接结束任务。
                record_failure_reason(str(exc))
                print(
                    f"[任务][竞演演出][流程][ERROR] {exc}",
                    flush=True,
                )
                return False
            except RoomEntryTransient as exc:
                if self.stopped():
                    return True
                room_timeouts += 1
                print(f"VersusLive room_retry=home-recovery attempt={room_timeouts} reason={exc}", flush=True)
                self.recover_room_preparation_timeout(str(exc))
                rematch_backoff(self, room_timeouts)
                reuse_room = False
                continue
            except InterruptedError:
                raise
            except FinalCoverIdentityConflict as exc:
                if self.stopped():
                    return True
                # 身份硬冲突禁止重试或继续输入，但不能绕过启动失败的手动跳车提示。
                reason = f"{type(exc).__name__}: {exc}"
                try:
                    self.jump_after_startup_failure(reason)
                except InterruptedError:
                    raise
                except JumpOutUnavailable as jump_exc:
                    if self.stopped():
                        return True
                    record_failure_reason(str(jump_exc))
                except Exception as jump_error:
                    if self.stopped():
                        return True
                    # 前后台切换自身失败时仍保留最初的身份原因，不能掩盖现场故障。
                    record_failure_reason(
                        f"{reason}；跳车切换失败：{type(jump_error).__name__}: "
                        f"{jump_error}；请手动跳车"
                    )
                else:
                    if self.stopped():
                        return True
                    record_failure_reason(reason)
                return False
            except Exception as exc:
                if self.stopped():
                    return True
                if play_failures >= retry_count:
                    if isinstance(exc, FinalCoverStartupFailure):
                        # 最终封面阶段无法确认本局歌曲/身份（身份冲突、标题未确认、
                        # 封面等待 60 秒超时等）会让机器人一直不出手、把血打空；重试
                        # 预算耗尽后按启动失败跳车：退后台返回游戏并结束任务，由用户
                        # 手动跳车，避免把机器人留在没法演奏的演出里。
                        try:
                            self.jump_after_startup_failure(
                                f"{type(exc).__name__}: {exc}；"
                                "已退后台并尝试返回游戏，任务结束"
                            )
                        except JumpOutUnavailable as jump_exc:
                            if self.stopped():
                                return True
                            print(
                                f"[任务][竞演演出][流程][ERROR] {jump_exc}",
                                flush=True,
                            )
                            return False
                    raise
                play_failures += 1
                reason = f"{type(exc).__name__}: {exc}"
                try:
                    append_current_run_event(
                        PROJECT_ROOT,
                        "retry",
                        "scheduled",
                        details={
                            "mode": "versus",
                            "attempt": play_failures + 1,
                            "attempt_limit": retry_count + 1,
                            "reason": reason,
                        },
                    )
                except Exception as evidence_error:
                    print(
                        "VersusLive retry_evidence_failed="
                        f"{type(evidence_error).__name__}: {evidence_error}",
                        flush=True,
                    )
                print(
                    "VersusLive play_retry=true "
                    f"attempt={play_failures + 1}/{retry_count + 1} "
                    f"reason={reason}",
                    flush=True,
                )
                self.recover_after_play_failure(reason)
                reuse_room = False
                continue
            if self.context.tasker.stopping:
                return True
            if not success:
                if play_failures >= retry_count:
                    return False
                play_failures += 1
                reason = "RealtimeProfilePlay 返回失败"
                try:
                    append_current_run_event(
                        PROJECT_ROOT,
                        "retry",
                        "scheduled",
                        details={
                            "mode": "versus",
                            "attempt": play_failures + 1,
                            "attempt_limit": retry_count + 1,
                            "reason": reason,
                        },
                    )
                except Exception as evidence_error:
                    print(
                        "VersusLive retry_evidence_failed="
                        f"{type(evidence_error).__name__}: {evidence_error}",
                        flush=True,
                    )
                print(
                    "VersusLive play_retry=true "
                    f"attempt={play_failures + 1}/{retry_count + 1} "
                    f"reason={reason}",
                    flush=True,
                )
                self.recover_after_play_failure(reason)
                reuse_room = False
                continue

            completed += 1
            self._stamina_remaining = None if total == 0 else total-completed
            play_failures = 0
            reconnects = 0
            room_timeouts = 0
            callback = getattr(self, "progress_callback", None)
            if callback is not None:
                try:
                    callback(completed, total)
                except Exception as exc:
                    print(f"VersusLive progress_warning={type(exc).__name__}: {exc}", flush=True)
            if self.context.tasker.stopping:
                return True
            is_last = total > 0 and completed >= total

            if should_stay_in_room(self.settings):
                try:
                    self.stay_in_room()
                except MemberExited:
                    if is_last:
                        try:
                            self.dismiss_member_exit()
                        except InterruptedError:
                            raise
                        except Exception as exc:
                            print(
                                "VersusLive post_score_warning="
                                f"{type(exc).__name__}: {exc}",
                                flush=True,
                            )
                        print(
                            "VersusLive requested_count=complete "
                            "member_exit=no_reentry",
                            flush=True,
                        )
                        return True
                    recover_completed_round("已完成演出后成员退出")
                    reuse_room = False
                    continue
                except InterruptedError:
                    raise
                except Exception as exc:
                    if is_last:
                        print(
                            "VersusLive stay_skipped last_round=true "
                            f"reason={type(exc).__name__}: {exc}",
                            flush=True,
                        )
                        return True
                    print(
                        "VersusLive stay=recover "
                        f"reason={type(exc).__name__}: {exc}",
                        flush=True,
                    )
                    recover_completed_round(
                        f"结算后未返回房间：{type(exc).__name__}: {exc}"
                    )
                    reuse_room = False
                    continue
                reuse_room = True
            elif not is_last:
                try:
                    self.return_to_room_selection()
                except MemberExited:
                    recover_completed_round("已完成演出后成员退出")
                except InterruptedError:
                    raise
                except Exception as exc:
                    # 本局已经计入完成；结算页面没有走回房间时恢复主页并继续
                    # 下一局，而不是把识别失败当成整个任务的致命错误。
                    print(
                        "VersusLive post_score=recover "
                        f"reason={type(exc).__name__}: {exc}",
                        flush=True,
                    )
                    recover_completed_round(
                        f"结算后未返回房间：{type(exc).__name__}: {exc}"
                    )
                reuse_room = False
        return True


@AgentServer.custom_action("VersusLiveConfigure")
class VersusLiveConfigure(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        if context.tasker.stopping:
            return True
        try:
            settings = configure_versus_settings(
                json.loads(argv.custom_action_param or "{}")
            )
            print(f"VersusLive configured={settings}", flush=True)
            return True
        except Exception as exc:
            record_failure_reason(f"竞演演出选项无效：{type(exc).__name__}: {exc}")
            safe_traceback()
            return False


@AgentServer.custom_action("VersusLiveFlow")
class VersusLiveAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            if context.tasker.stopping:
                return True
            settings = current_versus_settings()
            runtime_options = RealtimeProfileStore(
                PROJECT_ROOT / "profiles"
            ).runtime_options()
            # 共用协力的运行时开关；竞演没有独立的成员加载页保护选项。
            settings["versus_member_loading_guard_enabled"] = runtime_options[
                "cooperative_member_loading_guard_enabled"
            ]
            settings["play_failure_retry_count"] = int(
                runtime_options.get("play_failure_retry_count", 1)
            )
            preflight_error = versus_profile_preflight(
                context, str(settings["difficulty"])
            )
            if preflight_error is not None:
                record_failure_reason(preflight_error)
                print(
                    f"[任务][竞演演出][流程][ERROR] {preflight_error}",
                    flush=True,
                )
                return False

            def progress_argv(phase: str, total: int):
                return SimpleNamespace(
                    custom_action_param=json.dumps(
                        {
                            "task_name": "VersusLive",
                            "label": "竞演演出",
                            "total": total,
                            "phase": phase,
                        },
                        ensure_ascii=False,
                    ),
                    task_detail=getattr(argv, "task_detail", None),
                    node_name=getattr(argv, "node_name", "VersusRun"),
                )

            total = int(settings["count"])
            try:
                if not TaskProgress().run(context, progress_argv("start", total)):
                    print("RealtimeLive progress_warning=start-report-failed", flush=True)
            except InterruptedError:
                raise
            except Exception as exc:
                if context.tasker.stopping:
                    return True
                print(f"RealtimeLive progress_warning={type(exc).__name__}: {exc}", flush=True)


            def report_progress(_completed: int, expected_total: int) -> None:
                if not TaskProgress().run(
                    context,
                    progress_argv("completed", expected_total),
                ):
                    raise RuntimeError("竞演演出次数进度记录失败")

            return VersusLiveFlow(
                context,
                settings,
                progress_callback=report_progress,
                stamina_argv=argv,
            ).run()
        except InterruptedError:
            return True
        except Exception as exc:
            if context.tasker.stopping:
                return True
            reason = f"竞演演出失败：{type(exc).__name__}: {exc}"
            record_failure_reason(reason)
            safe_traceback()
            print(f"[任务][竞演演出][流程][ERROR] {reason}", flush=True)
            return False


@AgentServer.custom_action("VersusLiveFinalize")
class VersusLiveFinalize(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            if context.tasker.stopping:
                return True
            if should_stay_in_room(current_versus_settings()):
                print(
                    "VersusLive finalize=stay current_room=true",
                    flush=True,
                )
                return True
            if not CommonRecover().run(context, argv):
                print("VersusLive finalize_warning=演出已完成，主页恢复失败不终止任务", flush=True)
            return True
        except Exception as exc:
            if context.tasker.stopping:
                return True
            reason = f"竞演演出结束导航失败：{type(exc).__name__}: {exc}"
            safe_traceback()
            print(f"VersusLive finalize_warning={reason}", flush=True)
            return True
