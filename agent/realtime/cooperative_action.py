from __future__ import annotations

import json
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
from .game_effect_settings_action import (
    RealtimeGameEffectSettingsGate,
    verified_game_visual_settings,
)
from .game_effect_settings_action import _click as _maa_click
from .vision_io import imread_unicode, imwrite_unicode
from .game_effect_settings_action import _swipe as _maa_swipe
from .live_session import (
    append_current_run_event,
    current_live_run,
    update_live_run,
)
from .life_monitor import LifeDetector, LifeGuard, LifeStatus
from .live_visual_gate import MODE_TOGGLE_POINT, live_performance_mode_is_off
from .performance_settings_action import RealtimePerformanceSettingsGate
from .playfield_monitor import PlayfieldDetector
from .chart_repository import LocalChartRepository
from .final_cover import FinalCoverResolver
from .profile_play_action import RealtimeProfilePlay
from .profile_store import (
    EnvironmentSignature,
    RealtimeProfileStore,
    engine_from_native_flag,
)
from .rehearsal_action import frame_resolution
from .native_prearm import discard_prearmed_backend
from .cooperative_network import GameNetworkGate
from .result_navigation import RESULT_ANIMATION_SKIP_POINT, handle_story_page


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = PROJECT_ROOT / "resource" / "image" / "cooperative"
# 与 prepare() 传给流速门禁的环境参数保持一致：这些值同时用于开局前的
# Profile 预检，避免两处硬编码漂移。
COOPERATIVE_DPI = 240
COOPERATIVE_GAME_FPS = 60
COOPERATIVE_RENDER_QUALITY = "standard"
TEMPLATE_POSITIONS = {
    "live_entry": (975, 448),
    "room_search": (565, 620),
    "search_private": (545, 493),
    "search_friend": (765, 493),
    "friend_invite_title": (175, 78),
    "private_room_title": (392, 210),
    "room_wait": (110, 58),
    "song_unspecified": (690, 612),
    "ready_button": (1010, 575),
    "member_exit_title": (399, 158),
    "connect_failed_body": (580, 345),
    "repeat_room_title": (393, 225),
    "sss_guide_close": (856, 610),
    # 断网跳车：真实弹窗正文（2026-09-07 雷电录像提取）。
    "disconnect_continue_body": (488, 313),
    "disconnect_confirm_body": (495, 307),
}

DEFAULT_SETTINGS: dict[str, object] = {
    "entry_method": "normal",
    "room_tier": "free",
    "room_code": "",
    "difficulty": "Expert",
    "count": 1,
    "post_live_action": "exit",
    "member_exit_policy": "fail",
    "max_reconnects": 3,
    "debug_recording": False,
    "diagnostic_trace": True,
    "disconnect_jump_enabled": False,
}
_SETTINGS = dict(DEFAULT_SETTINGS)
_SETTINGS_LOCK = threading.Lock()

ROOM_TIER_INDEX = {
    "free": 0,
    "beginner": 1,
    "chief": 2,
    "legend": 3,
}
COOPERATIVE_DIFFICULTY_TARGETS = {
    "Easy": (602, 575),
    "Normal": (687, 575),
    "Hard": (769, 575),
    "Expert": (852, 575),
    "Special": (942, 575),
}
MEMBER_DOWNLOAD_TIMEOUT_SECONDS = 60.0
# 错过开演转场后，多久没看到“真实生命条”就判定本局根本没开演。
# 准备页上成员的下载进度条会被生命检测器误读成很小的数值（实测 94），
# 只有 >= 500 才可能是协力演出刚开场的队伍生命条。
MISSED_TRANSITION_NO_LIFE_SECONDS = 10.0
STRONG_LIFE_MINIMUM = 500
ROOM_SONG_CHOICE_TIMEOUT_SECONDS = 180.0
SONG_CHOICE_TO_READY_TIMEOUT_SECONDS = 60.0
POST_SCORE_NAVIGATION_TIMEOUT_SECONDS = 60.0
HOME_LIVE_POINT = (1175, 645)
# 断网跳车按钮点击点：弹窗1“通信已中断。是否继续演出？”点左侧“中断”；
# 弹窗2“确认中断当前演出返回主页吗？”点右侧粉色“中断”。
DISCONNECT_CONTINUE_INTERRUPT_POINT = (508, 447)
DISCONNECT_CONFIRM_INTERRUPT_POINT = (754, 439)
READY_DELIVERY_OBSERVE_SECONDS = 2.0


def _frame_is_black_transition(image: np.ndarray) -> bool:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return float(gray.mean()) < 6.0 and float(gray.std()) < 6.0


class CooperativePlayfieldEntryEvidence:
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


def cooperative_play_params(settings: dict[str, object]) -> dict[str, object]:
    return {
        "difficulty": str(settings["difficulty"]),
        "require_profile": True,
        "settings_gate_required": True,
        "debug_recording": bool(settings["debug_recording"]),
        "diagnostic_trace": bool(settings["diagnostic_trace"]),
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
        "run_mode": "cooperative",
        "confirm_final_cover": True,
        "final_cover_timeout_seconds": MEMBER_DOWNLOAD_TIMEOUT_SECONDS,
        "native_prearm_deferred": True,
        "life_depleted_jump_request": bool(
            settings.get("disconnect_jump_enabled", False)
        ),
    }


class MemberExited(RuntimeError):
    pass


class RoomEntryUnavailable(RuntimeError):
    """点击房间后游戏仍停在房间选择页。

    这是入房环节的故障（房间被退空、服务端拒绝、页面状态卡住等），不是演奏失败，
    所以不应当消耗 play_failure_retry_count 的重试额度。
    """


class JumpOutUnavailable(RuntimeError):
    """协力局已安全跳车或无法继续自动恢复，应立即结束任务。"""


def configure_cooperative_settings(params: dict[str, object]) -> dict[str, object]:
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
        if not 1 <= count <= 99:
            raise ValueError("协力演出次数必须是1到99的整数")
        candidate["count"] = count
        _SETTINGS.clear()
        _SETTINGS.update(candidate)
        return dict(_SETTINGS)


def current_cooperative_settings() -> dict[str, object]:
    with _SETTINGS_LOCK:
        return dict(_SETTINGS)


def cooperative_profile_preflight(context: Context, difficulty: str) -> str | None:
    """任务一开始就校验 Profile 与环境签名，失败返回可读原因。

    原实现把 Profile 解析放在准备页的流速门禁里：环境不匹配（例如任务
    执行过程中手动改过 TAP EFFECT）时，自动化已经完成了主页→演出选择→
    协力入口→房间→准备页的整段导航，才在准备页被拒，用户只看到“没点
    开始”。这里用与门禁相同的签名构造（截图分辨率 + 固定 DPI/帧率/画质
    + 运行时演出选项）提前做一次解析：失败立刻作为任务错误返回，导航
    一步都不做；截图不可用时回退到准备页的既有门禁。
    """
    store = RealtimeProfileStore(PROJECT_ROOT / "profiles")
    try:
        image = context.tasker.controller.post_screencap().wait().get()
    except Exception:
        # 控制器尚未就绪时无法构造签名，交给准备页门禁处理。
        return None
    visual = verified_game_visual_settings()
    options = store.runtime_options()
    signature = EnvironmentSignature(
        frame_resolution(image),
        COOPERATIVE_DPI,
        COOPERATIVE_GAME_FPS,
        COOPERATIVE_RENDER_QUALITY,
        1.0,
        int(visual.note_skin_type)
        if visual is not None
        else int(options["note_skin_type"]),
        int(visual.tap_effect)
        if visual is not None
        else int(options["tap_effect"]),
        bool(visual.judgement_assist_effect)
        if visual is not None
        else bool(options["judgement_assist_effect"]),
        engine_from_native_flag(
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
    """Only joined rooms can reuse the current room after a live."""
    return (
        str(settings.get("entry_method", "normal")) in {"friend", "private"}
        and str(settings.get("post_live_action", "exit")) == "stay"
    )


def classify_room_tier(image: np.ndarray) -> str | None:
    """Classify the selected centre room card by its stable saturated colour."""
    if (
        not isinstance(image, np.ndarray)
        or image.shape[0] < 487
        or image.shape[1] < 753
    ):
        return None
    card = image[194:487, 525:753]
    hsv = cv2.cvtColor(card, cv2.COLOR_BGR2HSV)
    mask = (hsv[:, :, 1] >= 90) & (hsv[:, :, 2] >= 80)
    hues = hsv[:, :, 0][mask]
    if hues.size < 300:
        return None
    histogram = np.bincount(hues, minlength=180)
    hue = int(np.argmax(histogram))
    if 84 <= hue <= 96:
        return "free"
    if 165 <= hue <= 179:
        return "beginner"
    if 97 <= hue <= 112:
        return "chief"
    if 10 <= hue <= 28:
        return "legend"
    return None


class CooperativeLiveFlow:
    def __init__(
        self,
        context: Context,
        settings: dict[str, object],
        *,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        self.context = context
        self.settings = settings
        self.progress_callback = progress_callback
        self.detector = LifeDetector()
        # 准备完毕后的短黑场可能只持续一帧；漏检时先用与本局身份一致的
        # 稳定最终封面放行，只有已经进入动态演奏场才走生命监控兜底。
        self.playfield_detector = PlayfieldDetector()
        self.playfield_entry_evidence = CooperativePlayfieldEntryEvidence()
        self.templates = {
            path.stem: imread_unicode(path, cv2.IMREAD_COLOR)
            for path in TEMPLATE_DIR.glob("*.png")
        }
        missing = [name for name, image in self.templates.items() if image is None]
        if missing:
            raise RuntimeError(f"协力模板损坏：{', '.join(missing)}")

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
            if getattr(self, "_post_score_refresh", False):
                return capture_image(self.context, node="ResultRefreshScreen")
            return capture_image(self.context)
        except ScreenRefreshCancelled as exc:
            raise InterruptedError("用户已停止任务") from exc

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
            if detect_member_exit and self.visible(
                image,
                "member_exit_title",
                0.93,
            ):
                raise MemberExited("协力成员退出房间")
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
        if self.visible(image, "member_exit_title", 0.93):
            # 当前版本弹窗：标题“错误”，正文“由于XX退出房间。将返回
            # 房间选择界面。”，底部居中“确定”按钮。
            self.click((638, 525))
            time.sleep(0.8)

    def dismiss_connect_failed(self, attempts: int = 5) -> bool:
        """“连接失败”弹窗：有界点击“重试”，直到弹窗消失或尝试耗尽。"""
        for _ in range(max(1, int(attempts))):
            image = self.capture()
            if not self.visible(image, "connect_failed_body", 0.90):
                return True
            self.click((748, 527))
            time.sleep(0.8)
        return not self.visible(
            self.capture(), "connect_failed_body", 0.90
        )

    def _adb_shell(self, args) -> tuple[int, str]:
        """把 MaaFramework 控制器 shell 通道适配成 (returncode, output)。"""
        command = " ".join(str(part) for part in args)
        try:
            output = self.controller.post_shell(command, 8000).wait().get()
            return 0, str(output or "")
        except Exception as exc:  # noqa: BLE001 - 任何失败都要 fail-closed
            return -1, str(exc)

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
        """生命归零后的断网跳车流程（AGENTS 第 22 条）。

        真实弹窗顺序（2026-09-07 雷电录像）：按游戏 UID 屏蔽出口流量 → 游戏
        退后台再切回 → 弹窗1“通信已中断。是否继续演出？”点左侧“中断” →
        弹窗2“确认中断当前演出返回主页吗？”点右侧“中断” → 恢复网络 →
        “连接失败。”弹窗有界点“重试”直到回主页。任何一步失败都在 finally
        恢复网络（fail-closed），绝不把模拟器留在断网状态。
        """
        gate = GameNetworkGate(self._adb_shell)
        try:
            required = {
                "disconnect_continue_body",
                "disconnect_confirm_body",
            }
            if not required.issubset(self.templates):
                # 模板未提取时不做任何网络/前台扰动，直接 fail-closed。
                print(
                    "CooperativeDisconnectJump popup_template_missing=true",
                    flush=True,
                )
                return False
            if not gate.block():
                print(
                    "CooperativeDisconnectJump gate_block_failed=true "
                    f"reason={gate.last_error or 'unknown'}",
                    flush=True,
                )
                return False
            # 退后台再切回；断网状态下切回会触发“是否切换到单人演奏”弹窗。
            self.controller.post_click_key(3).wait()
            time.sleep(0.6)
            self.controller.post_start_app(GAME_PACKAGE).wait()
            if not self._wait_and_click(
                "disconnect_continue_body",
                DISCONNECT_CONTINUE_INTERRUPT_POINT,
                popup_timeout_s,
            ):
                print(
                    "CooperativeDisconnectJump continue_popup_missed=true",
                    flush=True,
                )
                return False
            if not self._wait_and_click(
                "disconnect_confirm_body",
                DISCONNECT_CONFIRM_INTERRUPT_POINT,
                10.0,
            ):
                print(
                    "CooperativeDisconnectJump confirm_popup_missed=true",
                    flush=True,
                )
                return False
            # 先恢复网络，再对“连接失败。”做有界重试；断网状态下点重试
            # 无效是游戏正常表现。
            if not gate.restore():
                print(
                    "CooperativeDisconnectJump network_restore_failed=true",
                    flush=True,
                )
                return False
            time.sleep(0.8)
            self.dismiss_connect_failed()
            print("CooperativeDisconnectJump completed=true", flush=True)
            return True
        finally:
            gate.restore()

    # 准备完毕/开演窗口里出现这些模板，说明本局已经退回到“选择类”页面，
    # 演奏不可能再开始；继续等下去只会把整局任务拖死。
    #
    # 注意：room_wait / ready_button 在“准备完毕后的等待页”上同样存在，
    # 2026-09-12 19:17 就因为它们把一局正常开演误判成“退回房间”，
    # 因此这里只保留语义明确的选择页模板。
    ROOM_RESIDENT_TEMPLATES = (
        "room_search",
        "song_unspecified",
    )
    ROOM_RESIDENT_HITS = 5

    def resident_room_state(self, image: np.ndarray) -> str | None:
        """识别是否已经退回到房间等待/选择/选曲/准备页。"""
        for name in self.ROOM_RESIDENT_TEMPLATES:
            if name not in self.templates:
                continue
            if self.visible(image, name):
                return name
        return None

    def performance_aborted_by_disconnect(self, image: np.ndarray) -> bool:
        """演出是否已经被网络中断（游戏自己弹出了“通信已中断”）。

        2026-09-12 19:52 实测：协议断线让共享生命在 8 秒内从 1000 归零，
        屏幕上就是 disconnect_continue_body。这种局已经没有任何可挽救的
        内容，点两次“中断”退出演出后按普通单局失败重试即可，不该把 99
        局任务判死。
        """
        return self.visible(image, "disconnect_continue_body", 0.93) or self.visible(
            image, "disconnect_confirm_body", 0.93
        )

    def dismiss_disconnect_dialogs(self, image: np.ndarray) -> bool:
        """开演前出现“通信已中断”弹窗时点“中断”放弃本局。

        只在演奏尚未开始的窗口里调用：此时点“中断”等价于放弃本局，随后
        由进房重试路径重新组队，不会把 99 局任务判死（2026-09-12 18:12
        掉线局就是这样整整空等 60 秒后任务失败）。
        """
        if not self.visible(image, "disconnect_continue_body", 0.93):
            return False
        self._wait_and_click(
            "disconnect_continue_body",
            DISCONNECT_CONTINUE_INTERRUPT_POINT,
            6.0,
        )
        self._wait_and_click(
            "disconnect_confirm_body",
            DISCONNECT_CONFIRM_INTERRUPT_POINT,
            6.0,
        )
        return True

    def restart_game_and_wait(self, settle_seconds: float = 18.0) -> None:
        """强制退出游戏客户端并重新进入，把页面拉回已知状态后继续任务。

        除用户停止任务外不抛异常：重启本身失败时由调用方的单局失败重试 /
        恢复路径兜底，绝不因此把 99 局任务判死。
        """
        for label, call in (
            ("stop", self.controller.post_stop_app),
            ("start", self.controller.post_start_app),
        ):
            try:
                call(GAME_PACKAGE).wait()
                print(f"CooperativeLive game_restart={label}-sent", flush=True)
            except Exception as error:  # noqa: BLE001 - 重启失败也要继续
                print(
                    f"CooperativeLive game_restart={label}-failed "
                    f"{type(error).__name__}: {error}",
                    flush=True,
                )
            if label == "stop":
                time.sleep(1.5)
        deadline = time.monotonic() + max(0.0, float(settle_seconds))
        while time.monotonic() < deadline:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            time.sleep(0.5)
        print(
            f"CooperativeLive game_restart=settled settle_s={settle_seconds}",
            flush=True,
        )

    def ensure_room_page(self, timeout: float = 15.0) -> np.ndarray:
        state, image = self.wait_for(("room_search",), timeout=timeout)
        if state is None:
            raise RuntimeError("未识别协力房间选择页")
        return image

    def close_sss_guide(self) -> None:
        # 调用前 select_normal_room 已确认传说卡居中选中；一次性 SSS
        # 引导若存在会立即出现。没有引导关闭按钮说明本账号早已关过，
        # 直接返回，不再按固定 8 帧空等（实测每局浪费约 8 秒）。
        for attempt in range(8):
            image = self.capture()
            if self.visible(image, "sss_guide_close"):
                self.click((980, 648))
                time.sleep(0.7)
                return
            if classify_room_tier(image) == "legend":
                return
            time.sleep(0.2)

    ROOM_ENTRY_CLICK_ATTEMPTS = 3
    ROOM_ENTRY_VERIFY_SECONDS = 8.0
    ROOM_ENTRY_RETRY_DELAY_S = 4.0

    def select_normal_room(self) -> None:
        started = time.monotonic()
        try:
            image = self.ensure_room_page()
        except RuntimeError as exc:
            # 房间选择页都认不出来，同样属于“进房环节”故障，
            # 交给外层按进房重试处理而不占用演奏重试额度。
            raise RoomEntryUnavailable(str(exc)) from exc
        target = str(self.settings["room_tier"])
        if target not in ROOM_TIER_INDEX:
            raise ValueError(f"不支持的协力房间档位：{target}")
        actual = classify_room_tier(image)
        print(
            "CooperativeLive select_room reached_selection "
            f"target={target} actual={actual or 'unknown'} "
            f"elapsed={time.monotonic() - started:.2f}s",
            flush=True,
        )

        # Never reset the carousel to Free before selecting another room.
        # Free and Legend are the two endpoints, so swipe straight toward that
        # endpoint.  If the game snaps only one card per gesture, repeat in the
        # same direction and re-read the centre card; never traverse the wrong
        # way first.  Middle tiers use the current classified index directly.
        swipes = 0
        for _ in range(4):
            if actual == target:
                break
            if target == "free":
                start, end, duration = (250, 360), (1050, 360), 500
            elif target == "legend":
                start, end, duration = (1050, 360), (250, 360), 500
            elif actual in ROOM_TIER_INDEX:
                moving_right = (
                    ROOM_TIER_INDEX[target] > ROOM_TIER_INDEX[actual]
                )
                start, end, duration = (
                    ((820, 360), (455, 360), 280)
                    if moving_right
                    else ((455, 360), (820, 360), 280)
                )
            else:
                # Unknown centre card: move toward the nearest known endpoint
                # once, then let the next classified frame choose direction.
                if ROOM_TIER_INDEX[target] <= 1:
                    start, end, duration = (250, 360), (1050, 360), 500
                else:
                    start, end, duration = (1050, 360), (250, 360), 500
            _maa_swipe(self.context, start, end, duration)
            swipes += 1
            time.sleep(0.45)
            image = self.capture()
            actual = classify_room_tier(image)

        if actual != target:
            raise RuntimeError(
                f"协力房间档位复核失败：期望 {target}，识别为 {actual or 'unknown'}"
            )
        if target == "legend":
            self.close_sss_guide()
        print(
            "CooperativeLive select_room ready_to_click "
            f"target={target} swipes={swipes} "
            f"elapsed={time.monotonic() - started:.2f}s",
            flush=True,
        )
        self._click_room_with_retries(target)
        print(
            "CooperativeLive select_room room_entry_confirmed "
            f"elapsed={time.monotonic() - started:.2f}s",
            flush=True,
        )

    def verify_room_entry(
        self,
        failure_reason: str,
        timeout: float = 30.0,
    ) -> None:
        deadline = time.monotonic() + max(1.0, float(timeout))
        departed_frames = 0
        while time.monotonic() < deadline:
            image = self.capture()
            if self.visible(image, "member_exit_title", 0.93):
                raise MemberExited("协力成员退出房间")
            if any(
                self.visible(image, name)
                for name in ("room_wait", "song_unspecified", "ready_button")
            ):
                print(
                    "CooperativeLive room_entry=confirmed marker=known-lobby",
                    flush=True,
                )
                return
            if self.visible(image, "room_search"):
                departed_frames = 0
            else:
                departed_frames += 1
                if departed_frames >= 3:
                    # Matchmaking/loading/member collection layouts are
                    # transient and account/network dependent.  Stable
                    # departure from the selection page is sufficient here;
                    # wait_for_preparation owns the longer progression check.
                    print(
                        "CooperativeLive room_entry=confirmed "
                        "marker=left-room-selection",
                        flush=True,
                    )
                    return
            time.sleep(0.25)
        raise RoomEntryUnavailable(failure_reason)

    def _settle_room_selection(self, target: str, timeout: float = 2.4) -> None:
        """等房间选择页稳定后再点击。

        从房间退回选择页时卡片仍在刷新，立刻点击会被吞掉（实测两次各
        30 秒无响应），所以要求连续两帧档位识别一致。
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        previous: str | None = None
        while time.monotonic() < deadline:
            image = self.capture()
            if not self.visible(image, "room_search"):
                previous = None
                time.sleep(0.2)
                continue
            actual = classify_room_tier(image)
            if actual is not None and actual == previous:
                return
            previous = actual
            time.sleep(0.25)

    def _click_room_with_retries(self, target: str) -> None:
        """点击房间并验证进房；失败时先 Back 清浮层再重试。

        旧行为是“点一次 + 死等 30 秒”，失败即抛异常并消耗一次演奏重试；
        实测 15:38/15:40 连续两次被吞就把 99 局任务直接打死。
        """
        reason = "点击所选协力房间后仍停留在房间选择页，未开始匹配"
        attempts = self.ROOM_ENTRY_CLICK_ATTEMPTS
        for attempt in range(1, attempts + 1):
            self._settle_room_selection(target)
            self.click((1060, 650))
            try:
                self.verify_room_entry(
                    reason,
                    timeout=self.ROOM_ENTRY_VERIFY_SECONDS,
                )
            except RoomEntryUnavailable as exc:
                print(
                    "CooperativeLive room_entry_retry=true "
                    f"attempt={attempt}/{attempts} "
                    f"waited={self.ROOM_ENTRY_VERIFY_SECONDS:.1f}s",
                    flush=True,
                )
                if attempt >= attempts:
                    raise RoomEntryUnavailable(
                        f"{reason}（已点击 {attempts} 次仍无响应）"
                    ) from exc
                time.sleep(self.ROOM_ENTRY_RETRY_DELAY_S)
                # 先按一次 Back 清掉可能残留的浮层，再确认还在房间选择页；
                # 页面被推走时交给外层重新导航，绝不在这里硬等。
                try:
                    self.controller.post_click_key(4).wait()
                    time.sleep(0.8)
                    self.ensure_room_page(timeout=6.0)
                except Exception as back_error:
                    raise RoomEntryUnavailable(
                        f"{reason}（重试时无法回到房间选择页："
                        f"{type(back_error).__name__}: {back_error}）"
                    ) from back_error
                actual = classify_room_tier(self.capture())
                if actual != target:
                    raise RoomEntryUnavailable(
                        f"{reason}（点击 {attempt} 次后档位漂移为 "
                        f"{actual or 'unknown'}）"
                    )
            else:
                return

    def open_room_search(self) -> None:
        self.ensure_room_page()
        self.click((665, 650))
        state, _ = self.wait_for(
            ("search_private", "search_friend"), timeout=8.0
        )
        if state is None:
            raise RuntimeError("点击房间搜索后未出现搜索方式弹窗")

    def enter_friend_room(self) -> None:
        self.open_room_search()
        self.click((852, 528))
        state, _ = self.wait_for(("friend_invite_title",), timeout=10.0)
        if state is None:
            raise RuntimeError("未进入好友邀请房间列表")
        self.click((1038, 237))
        self.verify_room_entry("好友邀请已失效、列表为空或未能进入房间")

    def enter_private_room(self) -> None:
        code = str(self.settings.get("room_code", "")).strip()
        if len(code) != 6 or not code.isdecimal():
            raise ValueError("房间号必须是6位数字")
        self.open_room_search()
        self.click((635, 528))
        state, _ = self.wait_for(("private_room_title",), timeout=8.0)
        if state is None:
            raise RuntimeError("未打开私人房间号输入框")
        self.click((640, 370))
        require_game_foreground(self.controller)
        self.controller.post_input_text(code).wait()
        time.sleep(0.4)
        self.click((767, 474))
        self.verify_room_entry("私人房间号无效、房间已关闭或未能进入房间")

    def enter_room(self) -> None:
        method = str(self.settings["entry_method"])
        if method == "normal":
            self.select_normal_room()
        elif method == "friend":
            self.enter_friend_room()
        elif method == "private":
            self.enter_private_room()
        else:
            raise ValueError(f"不支持的协力入房方式：{method}")

    def wait_for_preparation(self) -> None:
        choice_deadline = (
            time.monotonic() + ROOM_SONG_CHOICE_TIMEOUT_SECONDS
        )
        while time.monotonic() < choice_deadline:
            state, _ = self.wait_for(
                ("song_unspecified", "ready_button"),
                timeout=min(
                    3.0,
                    max(0.1, choice_deadline - time.monotonic()),
                ),
            )
            if state == "ready_button":
                return
            if state == "song_unspecified":
                ready_deadline = (
                    time.monotonic()
                    + SONG_CHOICE_TO_READY_TIMEOUT_SECONDS
                )
                self.click((780, 647))
                time.sleep(0.35)
                self.click((1068, 647))
                print("CooperativeLive song_choice=unspecified", flush=True)
                time.sleep(0.5)
                while time.monotonic() < ready_deadline:
                    ready_state, _ = self.wait_for(
                        ("ready_button",),
                        timeout=min(
                            3.0,
                            max(0.1, ready_deadline - time.monotonic()),
                        ),
                    )
                    if ready_state == "ready_button":
                        return
                raise RuntimeError(
                    "点击不指定歌曲后60秒内未进入协力演出准备页"
                )
        self.jump_after_startup_failure(
            "进入协力房间后180秒内未出现不指定歌曲或准备页，"
            "已退后台返回游戏"
        )

    @staticmethod
    def action_argv(params: dict[str, object]):
        return SimpleNamespace(custom_action_param=json.dumps(params, ensure_ascii=False))

    def prepare(self) -> None:
        difficulty = str(self.settings["difficulty"])
        difficulty_params = {
            "difficulty": difficulty,
            "max_attempts": 3,
            "verify_delay_seconds": 0.25,
            "identity_read_attempts": 2,
            "identity_retry_delay_seconds": 0.15,
            "difficulty_targets": COOPERATIVE_DIFFICULTY_TARGETS,
            "song_level_roi": (130, 580, 56, 38),
            "song_title_roi": (105, 535, 290, 52),
            "song_identity": False,
            "mode": "cooperative",
            "debug_recording": bool(self.settings["debug_recording"]),
        }
        if not RealtimeDifficultySelect().run(
            self.context, self.action_argv(difficulty_params)
        ):
            raise RuntimeError(f"协力准备页未能选择并复核 {difficulty} 难度")

        visual_params = {
            "entry_mode": "preparation",
            "max_attempts": 1,
            "coordinates": {"preparation_gear": (946, 650)},
        }
        if not RealtimeGameEffectSettingsGate().run(
            self.context, self.action_argv(visual_params)
        ):
            raise RuntimeError("协力准备页演出视觉设置复核失败")

        performance_params = {
            "difficulty": difficulty,
            "require_profile": True,
            "dpi": COOPERATIVE_DPI,
            "game_fps": COOPERATIVE_GAME_FPS,
            "render_quality": COOPERATIVE_RENDER_QUALITY,
            "coordinates": {"gear": (946, 650)},
            "defer_native_prearm": True,
        }
        if not RealtimePerformanceSettingsGate().run(
            self.context, self.action_argv(performance_params)
        ):
            raise RuntimeError("协力准备页流速复核失败")
        self.ensure_performance_mode_off()
        ready_transition = self.ready_up_and_verify()
        if ready_transition != "black":
            self.watch_member_exit_before_black()
        print(
            f"CooperativeLive ready=true difficulty={difficulty} speed_gate=verified",
            flush=True,
        )

    def ensure_performance_mode_off(self) -> None:
        """协力房间页关闭 3D/MV 演出表现，防止演出场背景变化提前触发谱面。

        房间页左下角与单人准备页同布局：循环箭头切换按钮位于
        ``MODE_TOGGLE_POINT``，其右侧标签显示当前模式。标签区域读不到
        强饱和色即视为 OFF。点击后仍无法确认关闭（例如界面改版或坐标
        漂移）时不阻断本局：保留证据截图并继续，让既有门控推进演出。
        """
        for attempt in range(4):
            image = self.capture()
            if live_performance_mode_is_off(image):
                print(
                    "CooperativeLive performance_mode=off confirmed=true",
                    flush=True,
                )
                return
            if attempt == 0:
                self._save_performance_mode_evidence(image, "before")
            self.click(MODE_TOGGLE_POINT)
            time.sleep(0.6)
        try:
            self._save_performance_mode_evidence(self.capture(), "after")
        except InterruptedError:
            raise
        print(
            "CooperativeLive performance_mode=off confirmed=false "
            "action=continue-with-warning attempts=4",
            flush=True,
        )

    def _save_performance_mode_evidence(self, image: np.ndarray, stage: str) -> None:
        try:
            evidence_dir = PROJECT_ROOT / "debug"
            evidence_dir.mkdir(parents=True, exist_ok=True)
            path = evidence_dir / (
                "cooperative-performance-mode-"
                f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{stage}.png"
            )
            imwrite_unicode(path, image)
            print(f"CooperativeLive performance_mode_evidence={path}", flush=True)
        except Exception as exc:  # noqa: BLE001 - 证据失败不阻断演出流程
            print(
                "CooperativeLive performance_mode_evidence_failed="
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

    def ready_up_and_verify(self) -> str:
        """点击“准备完毕”并确认按钮消失，防止触控未送达造成空演奏。"""
        for attempt in range(3):
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            image = self.capture()
            box = self.template_box(image, "ready_button", 0.90)
            if box is None:
                # 按钮已消失：已进入准备完毕/成员等待或加载流程。
                print(
                    f"CooperativeLive ready=confirmed attempt={attempt + 1}",
                    flush=True,
                )
                return "already-confirmed"
            left, top, width, height = box
            self.click((left + width // 2, top + height // 2))
            delivery = self.watch_ready_delivery_after_click()
            if delivery != "still-visible":
                print(
                    "CooperativeLive ready=confirmed "
                    f"attempt={attempt + 1} delivery={delivery}",
                    flush=True,
                )
                return delivery
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
            if self.visible(image, "member_exit_title", 0.93):
                self.dismiss_member_exit()
                raise MemberExited("协力成员退出房间")
            if _frame_is_black_transition(image):
                outcome = "black"
            elif self.template_box(image, "ready_button", 0.90) is None:
                outcome = "button-gone"
            else:
                time.sleep(0.05)
                continue
            elapsed_ms = (time.monotonic() - started_at) * 1000.0
            print(
                "CooperativeLive ready_delivery "
                f"outcome={outcome} elapsed_ms={elapsed_ms:.1f} "
                f"timeout_s={float(timeout):.3f}",
                flush=True,
            )
            return outcome
        print(
            "CooperativeLive ready_delivery outcome=still-visible "
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
        转场，并把该证据直接交给演奏入口；只有已经进入动态演奏场时才进入
        只监控生命的 fail-closed 路径，绝不能把谱面开头锚到中段。静态准备页
        可能误中生命条与判定线，60 秒超时同样安全跳车并停止任务。
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
                "CooperativeLive ready_transition "
                f"outcome={outcome} elapsed_ms={elapsed_ms:.1f} "
                f"timeout_s={float(timeout):.3f}",
                flush=True,
            )
            return outcome

        resident_room_hits = 0
        life_like_samples = 0
        while time.monotonic() < deadline:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            image = self.capture()
            if _frame_is_black_transition(image):
                # 整屏黑场转场已经开始，成员退出弹窗窗口已过。
                return finish("black")
            if self.visible(image, "member_exit_title", 0.93):
                finish("member-exit")
                self.dismiss_member_exit()
                raise MemberExited("协力成员退出房间")
            if self.visible(image, "connect_failed_body", 0.90):
                # 准备完毕后游戏报“连接失败。”：本局不可能开演，直接重试。
                finish("connect-failed")
                self.dismiss_connect_failed()
                raise RoomEntryUnavailable(
                    "开演前出现“连接失败”弹窗，本局未能开始"
                )
            if self.dismiss_disconnect_dialogs(image):
                finish("disconnect")
                raise RoomEntryUnavailable(
                    "开演前通信中断，已中断本次演出并重试本局"
                )
            resident_state = self.resident_room_state(image)
            resident_room_hits = (
                resident_room_hits + 1 if resident_state else 0
            )
            if resident_room_hits >= self.ROOM_RESIDENT_HITS:
                # 连续两帧看到房间页模板：本局已经退回房间，开演不会再来。
                finish(f"back-to-{resident_state}")
                raise RoomEntryUnavailable(
                    f"准备完毕后退回{resident_state}，本局未能开始"
                )
            if final_cover_resolver is not None:
                cover_resolution = final_cover_resolver.observe(image)
                if cover_resolution is not None:
                    update_live_run(
                        startup_final_cover_image=image.copy(),
                        startup_final_cover_resolution=cover_resolution,
                    )
                    return finish("final-cover")
            playfield_visible = self.playfield_detector(image)
            if self.playfield_entry_evidence.observe(
                image,
                playfield_visible=playfield_visible,
            ):
                finish("playfield-motion-missed-transition")
                self.wait_for_life_depleted_after_missed_transition(image)
            if self.detector.detect(image).visible:
                life_like_samples += 1
            time.sleep(0.1)
        finish("timeout")
        if life_like_samples == 0:
            # 60 秒里既没有黑场、没有可确认的最终封面，也没有任何生命条：
            # 本局根本没开演（选歌后成员跳车、成员下载卡死是常见原因）。
            # 放弃本局回房间重排即可，不该像以前那样跳车并停止整场任务。
            raise RoomEntryUnavailable(
                "准备完毕后 60 秒内未进入演出，本局未能开始"
            )
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
        strong_samples = 0

        resident_room_hits = 0
        while time.monotonic() < deadline:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            # 这一窗口是 fail-closed 的“只等生命归零”，但掉线/成员退出弹窗
            # 与“退回房间页”必须立刻处理：2026-09-12 18:12 的掉线局里，网络
            # 异常让房间在准备完毕后散伙，流程却在这里空等满 60 秒，最后把
            # 14/99 的任务判死。弹窗/房间页说明本局没有开演，直接交回进房
            # 重试路径（不消耗演奏重试额度）。
            if self.visible(image, "member_exit_title", 0.93):
                print(
                    "CooperativeLive missed_transition_dialog=member-exit",
                    flush=True,
                )
                self.dismiss_member_exit()
                raise MemberExited("协力成员退出房间")
            if self.visible(image, "connect_failed_body", 0.90):
                print(
                    "CooperativeLive missed_transition_dialog=connect-failed",
                    flush=True,
                )
                self.dismiss_connect_failed()
                raise RoomEntryUnavailable(
                    "开演前出现“连接失败”弹窗，本局未能开始"
                )
            if self.dismiss_disconnect_dialogs(image):
                print(
                    "CooperativeLive missed_transition_dialog=disconnect",
                    flush=True,
                )
                raise RoomEntryUnavailable(
                    "开演前通信中断，已中断本次演出并重试本局"
                )
            resident_state = self.resident_room_state(image)
            resident_room_hits = (
                resident_room_hits + 1 if resident_state else 0
            )
            if resident_room_hits >= self.ROOM_RESIDENT_HITS:
                print(
                    "CooperativeLive missed_transition_state="
                    f"{resident_state} "
                    f"hits={resident_room_hits}",
                    flush=True,
                )
                raise RoomEntryUnavailable(
                    f"准备完毕后退回{resident_state}，本局未能开始"
                )
            reading = self.detector.detect(image)
            if reading.visible:
                visible_samples += 1
                if (
                    int(getattr(reading, "value", 0) or 0)
                    >= STRONG_LIFE_MINIMUM
                ):
                    strong_samples += 1
            else:
                invisible_samples += 1
            if (
                strong_samples == 0
                and time.monotonic() - started_at
                >= MISSED_TRANSITION_NO_LIFE_SECONDS
            ):
                # 从来没有出现过像样的生命条：本局根本没开演（选歌后成员
                # 跳车/下载卡住的准备页就是这种样子）。此时既没有谱面可以
                # 锚定，也不存在“在歌曲中段启动引擎”的风险，直接放弃本局
                # 回房间重排，而不是空等 60 秒把整场任务判死。
                print(
                    "CooperativeLive missed_transition_no_life "
                    f"elapsed_ms="
                    f"{(time.monotonic() - started_at) * 1000.0:.1f} "
                    f"visible_samples={visible_samples} "
                    f"invisible_samples={invisible_samples}",
                    flush=True,
                )
                raise RoomEntryUnavailable(
                    "错过开演转场后未出现真实生命条，判定本局未开演"
                )
            status = guard.update(reading)
            if status is LifeStatus.DEAD:
                print(
                    "CooperativeLive missed_transition_life_monitor "
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
            "CooperativeLive missed_transition_life_monitor "
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
        """从无法安全启动演奏的协力局退后台并返回游戏，然后停止任务。"""
        require_game_foreground(self.controller)
        self.controller.post_click_key(3).wait()
        time.sleep(0.6)
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
        record_failure_reason(reason)
        print(
            "CooperativeLive startup_jump "
            f"foreground_confirmed={str(foreground_confirmed).lower()} "
            f"reason={reason}",
            flush=True,
        )
        raise JumpOutUnavailable(reason)

    def jump_after_download_timeout(self) -> None:
        reason = "成员下载超过60秒仍未进入演出，已主动跳车并返回游戏"
        self.jump_after_startup_failure(reason)

    def wait_for_playfield(self) -> None:
        state, _ = self.wait_for(
            ("playfield",),
            timeout=MEMBER_DOWNLOAD_TIMEOUT_SECONDS,
            interval=0.2,
        )
        if state != "playfield":
            self.jump_after_download_timeout()
        print("CooperativeLive member_download=complete playfield_visible=true", flush=True)

    def play(self) -> bool:
        params = cooperative_play_params(self.settings)
        success = RealtimeProfilePlay().run(
            self.context, self.action_argv(params)
        )
        run = current_live_run()
        if run is not None and bool(run.disconnect_jump_requested):
            # 生命归零分两种情况：
            # 1) 游戏自己已经弹出“通信已中断”——这一局的演出是被网络掐断
            #    的，没有任何可挽救的内容。点两次“中断”退出演出，按普通
            #    单局失败重试（不消耗手动断网跳车的门禁），99 局任务继续。
            # 2) 没有弹窗——可能是我们自己的判定失误，保持原来的保守策略：
            #    回主页 → 切回游戏 → 结束任务，由用户手动断网跳车。
            abort_image = self.capture()
            if self.performance_aborted_by_disconnect(abort_image):
                print(
                    "CooperativeLive life_depleted=disconnect-aborted "
                    "round_lost=true task_continue=true",
                    flush=True,
                )
                self.dismiss_disconnect_dialogs(abort_image)
                return False
            # 2) 用户打开了“主动断网跳车”（GUI 的 CooperativeDisconnectJumpConfigure
            #    → disconnect_jump_enabled）：屏蔽游戏出口流量 → 切后台再切回
            #    → 点两次“中断” → 恢复网络 → 点“重试”回主页，然后按普通单局
            #    失败重试继续 99 局任务。跳车失败则退回第三种情况。
            if bool(self.settings.get("disconnect_jump_enabled", False)):
                jumped = False
                try:
                    jumped = self.disconnect_jump_out()
                except InterruptedError:
                    raise
                except Exception as error:  # noqa: BLE001 - 跳车失败也要继续
                    print(
                        "CooperativeLive disconnect_jump_error="
                        f"{type(error).__name__}: {error}",
                        flush=True,
                    )
                print(
                    f"CooperativeLive disconnect_jump done={jumped} "
                    "round_lost=true task_continue=true",
                    flush=True,
                )
                if not jumped:
                    self.restart_game_and_wait()
                return False
            # 3) 没开自动跳车、也没有弹窗：这一局已经废了（2026-09-12 20:22：
            #    锚点晚了约
            #    10 秒，整局只发出 33 个动作，共享生命直接归零）。按要求改成
            #    “退出游戏重新进入”：force-stop 客户端 → 重新启动 → 交给
            #    单局失败重试路径回房间继续下一局，不再把 99 局任务判死。
            print(
                "CooperativeLive life_depleted=game-restart-continue "
                "round_lost=true task_continue=true",
                flush=True,
            )
            self.restart_game_and_wait()
            return False
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

        Cooperative result pages do not have a fixed count.  ``room_search``
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
            return state
        if self.pipeline_box(image, "CooperativeHomeMarker") is not None:
            return "home"
        # 主页“要退出游戏吗”确认框：点“取消”并像剧情页一样跳过本帧
        # 的返回键，否则弹窗与返回键来回切换，结算导航卡满超时。
        quit_box = self.pipeline_box(image, "QuitConfirmCancel")
        if quit_box is not None:
            self.click(
                (
                    int(quit_box.x + quit_box.w // 2),
                    int(quit_box.y + quit_box.h // 2),
                )
            )
            return "story"
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
        """Skip animation, press Back, inspect, then perform the second tap."""
        self.click(RESULT_ANIMATION_SKIP_POINT)
        self.controller.post_click_key(4).wait()
        state = self.wait_for_post_score_destination(
            names,
            timeout=inspect_timeout,
        )
        # Keep the user-requested click-before/after-Back cadence.  This is now
        # the literal bottom-right pixel, so it stays input-neutral even when
        # the intervening recognition says Back has already reached Home.
        if state != "story":
            self.click(RESULT_ANIMATION_SKIP_POINT)
        return state

    def navigate_to_cooperative_room_selection(self, origin: str) -> None:
        """Explicitly recover Home/live-select into cooperative room select."""
        deadline = time.monotonic() + 30.0
        next_home_click_at = 0.0
        next_entry_click_at = 0.0
        while time.monotonic() < deadline:
            image = self.capture()
            if self.visible(image, "room_search"):
                print(
                    "CooperativeLive state=room-selection "
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

            close_box = self.pipeline_box(image, "CooperativeNavigationClose")
            if close_box is not None:
                self.click(
                    (
                        int(close_box.x + close_box.w // 2),
                        int(close_box.y + close_box.h // 2),
                    )
                )
                time.sleep(0.5)
                continue

            if self.pipeline_box(image, "CooperativeHomeMarker") is not None:
                now = time.monotonic()
                if now >= next_home_click_at:
                    self.click(HOME_LIVE_POINT)
                    next_home_click_at = now + 2.0
                    print(
                        "CooperativeLive state=home action=open-live-for-next-round",
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
                        "CooperativeLive state=live-select "
                        "action=open-cooperative-for-next-round",
                        flush=True,
                    )
                time.sleep(0.35)
                continue

            time.sleep(0.35)
        raise RuntimeError(
            "已离开协力结算，但30秒内未能重新进入协力房间选择页"
        )

    def return_to_room_selection(self) -> None:
        deadline = time.monotonic() + POST_SCORE_NAVIGATION_TIMEOUT_SECONDS
        attempts = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("协力结算推进60秒后仍未返回房间选择页面")
            state = self.wait_for_post_score_destination(
                ("room_search", "live_entry"),
                timeout=min(2.0, remaining),
            )
            if state == "story":
                continue
            if state == "room_search":
                print(
                    "CooperativeLive state=room-selection "
                    "result_navigation=complete",
                    flush=True,
                )
                return
            if state in {"home", "live_entry"}:
                self.navigate_to_cooperative_room_selection(state)
                return

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("协力结算推进60秒后仍未返回房间选择页面")
            state = self.advance_post_score_once(
                ("room_search", "live_entry"),
                inspect_timeout=min(2.0, remaining),
            )
            attempts += 1
            print(
                "CooperativeLive state=post-score "
                "action=corner-back-recognise-corner"
                f" attempt={attempts}",
                flush=True,
            )
            if state == "room_search":
                print(
                    "CooperativeLive state=room-selection "
                    "result_navigation=complete",
                    flush=True,
                )
                return
            if state in {"home", "live_entry"}:
                self.navigate_to_cooperative_room_selection(state)
                return

    def stay_in_room(self) -> None:
        method = str(self.settings.get("entry_method", "normal"))
        if method not in {"friend", "private"}:
            raise ValueError("留在房间仅适用于好友邀请房间或房间号入房")
        deadline = time.monotonic() + POST_SCORE_NAVIGATION_TIMEOUT_SECONDS
        result_back_attempts = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    "协力结算推进60秒后仍未出现是否留在同一房间的提示"
                )
            state = self.wait_for_post_score_destination(
                ("repeat_room_title", "room_search", "live_entry"),
                timeout=min(2.0, remaining),
            )
            if state == "story":
                continue
            if state == "repeat_room_title":
                break
            if state in {"home", "room_search", "live_entry"}:
                raise RuntimeError(
                    "未出现是否留在同一房间的提示，当前房间已经结束"
                )
            # Result pages are not a fixed sequence.  After every accelerated
            # Back, inspect the fresh frame for the repeat-room popup; if it is
            # absent, advance the next result page in the same way.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    "协力结算推进60秒后仍未出现是否留在同一房间的提示"
                )
            state = self.advance_post_score_once(
                ("repeat_room_title", "room_search", "live_entry"),
                inspect_timeout=min(2.0, remaining),
            )
            result_back_attempts += 1
            print(
                "CooperativeLive state=post-score "
                "action=corner-back-recognise-corner"
                f" attempt={result_back_attempts}",
                flush=True,
            )
            if state == "repeat_room_title":
                break
            if state in {"home", "room_search", "live_entry"}:
                raise RuntimeError(
                    "未出现是否留在同一房间的提示，当前房间已经结束"
                )
        # The exact repeat-room popup is visually identified above.  Its pink
        # “是” button is centred at (768, 447) on the canonical 1280x720 UI.
        self.click((768, 447))
        time.sleep(0.8)
        state, _ = self.wait_for(
            ("room_wait", "song_unspecified", "ready_button"),
            timeout=15.0,
        )
        if state is None:
            raise RuntimeError("已选择留在房间，但未返回协力房间等候界面")
        print("CooperativeLive repeat_room=stay confirmed=true", flush=True)

    def run_attempt(self, reuse_room: bool = False) -> bool:
        try:
            if not reuse_room:
                self.enter_room()
            self.wait_for_preparation()
            self.prepare()
            return self.play()
        except (InterruptedError, MemberExited, JumpOutUnavailable):
            raise
        except Exception as exc:
            if isinstance(exc, OSError) and "access violation" in str(exc).lower():
                # 二次访问反向控制器会用新的访问冲突掩盖最初的句柄失效。
                raise
            # 弹窗可能在任意准备步骤之间出现，统一转换为成员退出策略处理。
            try:
                image = self.capture()
            except Exception:
                raise exc
            if self.visible(image, "member_exit_title", 0.93):
                raise MemberExited("协力成员退出房间") from exc
            raise

    def recover_after_play_failure(self, reason: str) -> None:
        """完整清理失败单局并从主页重新进入协力，禁止在旧会话中续跑。"""
        discard_prearmed_backend("cooperative-play-retry")
        recovery_params = {
            "home_node": "CooperativeHomeMarker",
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
            "restart_limit": 2,
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
        if not CommonRecover().run(self.context, argv):
            raise RuntimeError(
                f"协力单局失败后无法恢复主页：{reason}"
            )
        self.navigate_to_cooperative_room_selection("home")

    def handle_member_exit(self, reconnects: int) -> int | None:
        policy = str(self.settings["member_exit_policy"])
        reconnect_limit = max(0, min(5, int(self.settings["max_reconnects"])))
        self.dismiss_member_exit()
        if policy != "reconnect":
            reason = "检测到协力成员退出房间，已确认弹窗并结束任务"
            record_failure_reason(reason)
            print(f"[任务][协力演出][成员退出][ERROR] {reason}", flush=True)
            return None
        if reconnects >= reconnect_limit:
            reason = f"协力成员退出后已重连{reconnect_limit}次，达到上限"
            record_failure_reason(reason)
            print(f"[任务][协力演出][重连][ERROR] {reason}", flush=True)
            return None
        reconnects += 1
        print(
            "CooperativeLive member_exit=reconnect "
            f"attempt={reconnects}/{reconnect_limit}",
            flush=True,
        )
        # 成员退出弹窗关闭后，游戏往往还停留在结算页，不能直接要求
        # “房间选择页”出现。先把剩余结算页推进回房间/主页；仍失败则走
        # 主页恢复再重进，避免因为识别不到房间页把整个任务报错停掉。
        try:
            self.return_to_room_selection()
        except MemberExited:
            try:
                self.recover_after_play_failure("成员退出弹窗反复出现")
            except Exception as recovery_error:
                raise RuntimeError(
                    "成员退出后恢复失败："
                    f"{type(recovery_error).__name__}: {recovery_error}"
                ) from recovery_error
        except InterruptedError:
            raise
        except Exception as exc:
            try:
                self.recover_after_play_failure(
                    f"成员退出后未回到房间：{type(exc).__name__}: {exc}"
                )
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
        entry_failures = 0
        entry_retry_limit = 3
        entry_recoveries = 0
        entry_recovery_limit = 3
        exhausted_rounds = 0
        exhausted_limit = 3
        retry_count = max(
            0,
            min(3, int(self.settings.get("play_failure_retry_count", 0))),
        )
        while completed < total:
            print(
                f"CooperativeLive round={completed + 1}/{total} "
                f"reuse_room={str(reuse_room).lower()}",
                flush=True,
            )
            try:
                success = self.run_attempt(reuse_room=reuse_room)
            except MemberExited:
                next_reconnects = self.handle_member_exit(reconnects)
                if next_reconnects is None:
                    return False
                reconnects = next_reconnects
                reuse_room = False
                continue
            except RoomEntryUnavailable as exc:
                # 进房环节（点房间没反应、退回选择页、档位漂移）不是演奏失败，
                # 单独计数重试，绝不消耗 play_failure_retry_count，否则一次
                # 进房抖动就会把 99 局任务判死。
                entry_failures += 1
                reason = f"{type(exc).__name__}: {exc}"
                if entry_failures > entry_retry_limit:
                    record_failure_reason(reason)
                    raise
                print(
                    "CooperativeLive entry_retry=true "
                    f"attempt={entry_failures}/{entry_retry_limit} "
                    f"play_failures_used={play_failures} "
                    f"reason={reason}",
                    flush=True,
                )
                if entry_failures >= 2:
                    # 连续进不去房通常是页面状态卡住：走一次完整恢复（回主页
                    # 重新进入协力），同样不消耗演奏重试额度。恢复成功后把
                    # 进房计数清零——重启游戏/重登这种恢复能把页面拉回已知
                    # 状态，不该继续占用这一次的进房重试预算。
                    try:
                        self.recover_after_play_failure(reason)
                    except Exception as recovery_error:
                        print(
                            "CooperativeLive entry_retry_recovery_failed="
                            f"{type(recovery_error).__name__}: "
                            f"{recovery_error}",
                            flush=True,
                        )
                    else:
                        entry_recoveries += 1
                        if entry_recoveries > entry_recovery_limit:
                            record_failure_reason(
                                "进房连续失败且恢复无效：" + reason
                            )
                            raise
                        entry_failures = 0
                else:
                    time.sleep(6.0)
                reuse_room = False
                continue
            except JumpOutUnavailable as exc:
                # 已执行安全跳车，或现有自动化无法继续处理时直接结束任务。
                record_failure_reason(str(exc))
                print(
                    f"[任务][协力演出][流程][ERROR] {exc}",
                    flush=True,
                )
                return False
            except Exception as exc:
                if play_failures >= retry_count:
                    raise
                play_failures += 1
                reason = f"{type(exc).__name__}: {exc}"
                try:
                    append_current_run_event(
                        PROJECT_ROOT,
                        "retry",
                        "scheduled",
                        details={
                            "mode": "cooperative",
                            "attempt": play_failures + 1,
                            "attempt_limit": retry_count + 1,
                            "reason": reason,
                        },
                    )
                except Exception as evidence_error:
                    print(
                        "CooperativeLive retry_evidence_failed="
                        f"{type(evidence_error).__name__}: {evidence_error}",
                        flush=True,
                    )
                print(
                    "CooperativeLive play_retry=true "
                    f"attempt={play_failures + 1}/{retry_count + 1} "
                    f"reason={reason}",
                    flush=True,
                )
                self.recover_after_play_failure(reason)
                reuse_room = False
                continue
            if not success:
                if play_failures >= retry_count:
                    # 单局重试预算用尽：不再直接把 99 局任务判死（2026-09-13
                    # 网络抖动时连续两局失败就把整场结束）。做一次完整恢复后
                    # 继续下一局，只有连续 exhausted_limit 轮都用尽预算才判死。
                    exhausted_rounds += 1
                    print(
                        "CooperativeLive round_exhausted=true "
                        f"streak={exhausted_rounds}/{exhausted_limit} "
                        f"completed={completed}",
                        flush=True,
                    )
                    if exhausted_rounds >= exhausted_limit:
                        record_failure_reason(
                            f"连续 {exhausted_rounds} 轮单局重试预算用尽"
                        )
                        return False
                    try:
                        self.recover_after_play_failure("单局重试预算用尽")
                    except InterruptedError:
                        raise
                    except Exception as recovery_error:
                        print(
                            "CooperativeLive exhausted_recovery_failed="
                            f"{type(recovery_error).__name__}: "
                            f"{recovery_error}",
                            flush=True,
                        )
                    play_failures = 0
                    reuse_room = False
                    continue
                play_failures += 1
                reason = "RealtimeProfilePlay 返回失败"
                try:
                    append_current_run_event(
                        PROJECT_ROOT,
                        "retry",
                        "scheduled",
                        details={
                            "mode": "cooperative",
                            "attempt": play_failures + 1,
                            "attempt_limit": retry_count + 1,
                            "reason": reason,
                        },
                    )
                except Exception as evidence_error:
                    print(
                        "CooperativeLive retry_evidence_failed="
                        f"{type(evidence_error).__name__}: {evidence_error}",
                        flush=True,
                    )
                print(
                    "CooperativeLive play_retry=true "
                    f"attempt={play_failures + 1}/{retry_count + 1} "
                    f"reason={reason}",
                    flush=True,
                )
                self.recover_after_play_failure(reason)
                reuse_room = False
                continue

            completed += 1
            play_failures = 0
            entry_failures = 0
            exhausted_rounds = 0
            callback = getattr(self, "progress_callback", None)
            if callback is not None:
                callback(completed, total)
            is_last = completed >= total

            if should_stay_in_room(self.settings):
                try:
                    self.stay_in_room()
                except MemberExited:
                    if (
                        is_last
                        and str(self.settings["member_exit_policy"]) == "reconnect"
                    ):
                        self.dismiss_member_exit()
                        print(
                            "CooperativeLive requested_count=complete "
                            "member_exit=no_reentry",
                            flush=True,
                        )
                        return True
                    next_reconnects = self.handle_member_exit(reconnects)
                    if next_reconnects is None:
                        return False
                    reconnects = next_reconnects
                    reuse_room = False
                    continue
                except InterruptedError:
                    raise
                except Exception as exc:
                    if is_last:
                        print(
                            "CooperativeLive stay_skipped last_round=true "
                            f"reason={type(exc).__name__}: {exc}",
                            flush=True,
                        )
                        return True
                    print(
                        "CooperativeLive stay=recover "
                        f"reason={type(exc).__name__}: {exc}",
                        flush=True,
                    )
                    self.recover_after_play_failure(
                        f"结算后未返回房间：{type(exc).__name__}: {exc}"
                    )
                    reuse_room = False
                    continue
                reuse_room = True
            elif not is_last:
                try:
                    self.return_to_room_selection()
                except MemberExited:
                    next_reconnects = self.handle_member_exit(reconnects)
                    if next_reconnects is None:
                        return False
                    reconnects = next_reconnects
                except InterruptedError:
                    raise
                except Exception as exc:
                    # 本局已经计入完成；结算页面没有走回房间时恢复主页并继续
                    # 下一局，而不是把识别失败当成整个任务的致命错误。
                    print(
                        "CooperativeLive post_score=recover "
                        f"reason={type(exc).__name__}: {exc}",
                        flush=True,
                    )
                    self.recover_after_play_failure(
                        f"结算后未返回房间：{type(exc).__name__}: {exc}"
                    )
                reuse_room = False
        return True


@AgentServer.custom_action("CooperativeLiveConfigure")
class CooperativeLiveConfigure(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        if context.tasker.stopping:
            return True
        try:
            settings = configure_cooperative_settings(
                json.loads(argv.custom_action_param or "{}")
            )
            print(f"CooperativeLive configured={settings}", flush=True)
            return True
        except Exception as exc:
            record_failure_reason(f"协力演出选项无效：{type(exc).__name__}: {exc}")
            traceback.print_exc()
            return False


@AgentServer.custom_action("CooperativeLiveFlow")
class CooperativeLiveAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            if context.tasker.stopping:
                return True
            settings = current_cooperative_settings()
            settings["play_failure_retry_count"] = int(
                RealtimeProfileStore(
                    PROJECT_ROOT / "profiles"
                ).runtime_options().get("play_failure_retry_count", 1)
            )
            preflight_error = cooperative_profile_preflight(
                context, str(settings["difficulty"])
            )
            if preflight_error is not None:
                record_failure_reason(preflight_error)
                print(
                    f"[任务][协力演出][流程][ERROR] {preflight_error}",
                    flush=True,
                )
                return False

            def progress_argv(phase: str, total: int):
                return SimpleNamespace(
                    custom_action_param=json.dumps(
                        {
                            "task_name": "CooperativeLive",
                            "label": "协力演出",
                            "total": total,
                            "phase": phase,
                        },
                        ensure_ascii=False,
                    ),
                    task_detail=getattr(argv, "task_detail", None),
                    node_name=getattr(argv, "node_name", "CooperativeRun"),
                )

            total = int(settings["count"])
            if not TaskProgress().run(context, progress_argv("start", total)):
                raise RuntimeError("协力演出次数初始化失败")

            def report_progress(_completed: int, expected_total: int) -> None:
                if not TaskProgress().run(
                    context,
                    progress_argv("completed", expected_total),
                ):
                    raise RuntimeError("协力演出次数进度记录失败")

            return CooperativeLiveFlow(
                context,
                settings,
                progress_callback=report_progress,
            ).run()
        except InterruptedError:
            return True
        except Exception as exc:
            if context.tasker.stopping:
                return True
            reason = f"协力演出失败：{type(exc).__name__}: {exc}"
            record_failure_reason(reason)
            traceback.print_exc()
            print(f"[任务][协力演出][流程][ERROR] {reason}", flush=True)
            return False


@AgentServer.custom_action("CooperativeLiveFinalize")
class CooperativeLiveFinalize(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            if context.tasker.stopping:
                return True
            if should_stay_in_room(current_cooperative_settings()):
                print(
                    "CooperativeLive finalize=stay current_room=true",
                    flush=True,
                )
                return True
            return CommonRecover().run(context, argv)
        except Exception as exc:
            if context.tasker.stopping:
                return True
            reason = f"协力演出结束导航失败：{type(exc).__name__}: {exc}"
            record_failure_reason(reason)
            traceback.print_exc()
            return False
