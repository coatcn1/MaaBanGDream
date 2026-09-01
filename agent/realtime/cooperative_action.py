from __future__ import annotations

import json
import threading
import time
import traceback
from collections.abc import Callable
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
from .game_effect_settings_action import RealtimeGameEffectSettingsGate
from .game_effect_settings_action import _click as _maa_click
from .game_effect_settings_action import _swipe as _maa_swipe
from .life_monitor import LifeDetector
from .performance_settings_action import RealtimePerformanceSettingsGate
from .profile_play_action import RealtimeProfilePlay
from .result_navigation import RESULT_ANIMATION_SKIP_POINT


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = PROJECT_ROOT / "resource" / "image" / "cooperative"
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
    "member_exit_title": (400, 478),
    "repeat_room_title": (393, 225),
    "sss_guide_close": (856, 610),
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
POST_SCORE_NAVIGATION_TIMEOUT_SECONDS = 60.0
HOME_LIVE_POINT = (1175, 645)


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
        "use_life_safety": False,
        "continue_after_life_depleted": True,
        "run_mode": "cooperative",
    }


class MemberExited(RuntimeError):
    pass


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
        self.templates = {
            path.stem: cv2.imread(str(path), cv2.IMREAD_COLOR)
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
    ) -> tuple[str | None, np.ndarray]:
        deadline = time.monotonic() + timeout
        image = self.capture()
        while True:
            if self.visible(image, "member_exit_title", 0.93):
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
            self.click((500, 678))
            time.sleep(0.8)

    def ensure_room_page(self, timeout: float = 15.0) -> np.ndarray:
        state, image = self.wait_for(("room_search",), timeout=timeout)
        if state is None:
            raise RuntimeError("未识别协力房间选择页")
        return image

    def close_sss_guide(self) -> None:
        for attempt in range(8):
            image = self.capture()
            if self.visible(image, "sss_guide_close"):
                self.click((980, 648))
                time.sleep(0.7)
                return
            # Once the carousel itself is readable for several settled frames,
            # this account has already dismissed the one-time guide.
            if attempt >= 5 and classify_room_tier(image) == "legend":
                return
            time.sleep(0.2)

    def select_normal_room(self) -> None:
        image = self.ensure_room_page()
        target = str(self.settings["room_tier"])
        if target not in ROOM_TIER_INDEX:
            raise ValueError(f"不支持的协力房间档位：{target}")
        actual = classify_room_tier(image)

        # Never reset the carousel to Free before selecting another room.
        # Free and Legend are the two endpoints, so swipe straight toward that
        # endpoint.  If the game snaps only one card per gesture, repeat in the
        # same direction and re-read the centre card; never traverse the wrong
        # way first.  Middle tiers use the current classified index directly.
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
            time.sleep(0.45)
            image = self.capture()
            actual = classify_room_tier(image)

        if actual != target:
            raise RuntimeError(
                f"协力房间档位复核失败：期望 {target}，识别为 {actual or 'unknown'}"
            )
        if target == "legend":
            self.close_sss_guide()
        self.click((1060, 650))
        self.verify_room_entry(
            "点击所选协力房间后仍停留在房间选择页，未开始匹配"
        )

    def verify_room_entry(self, failure_reason: str) -> None:
        deadline = time.monotonic() + 30.0
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
        raise RuntimeError(failure_reason)

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
        song_confirmed = False
        deadline = time.monotonic() + 180.0
        while time.monotonic() < deadline:
            state, _ = self.wait_for(
                ("song_unspecified", "ready_button"),
                timeout=min(3.0, max(0.1, deadline - time.monotonic())),
            )
            if state == "ready_button":
                return
            if state == "song_unspecified":
                if not song_confirmed:
                    self.click((780, 647))
                    time.sleep(0.35)
                    self.click((1068, 647))
                    song_confirmed = True
                    print("CooperativeLive song_choice=unspecified", flush=True)
                time.sleep(0.5)
        raise RuntimeError("180秒内未进入协力演出准备页")

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
            "dpi": 240,
            "game_fps": 60,
            "render_quality": "standard",
            "coordinates": {"gear": (946, 650)},
        }
        if not RealtimePerformanceSettingsGate().run(
            self.context, self.action_argv(performance_params)
        ):
            raise RuntimeError("协力准备页流速复核失败")
        self.click((1129, 610))
        print(
            f"CooperativeLive ready=true difficulty={difficulty} speed_gate=verified",
            flush=True,
        )

    def jump_after_download_timeout(self) -> None:
        require_game_foreground(self.controller)
        self.controller.post_click_key(3).wait()
        time.sleep(1.5)
        self.controller.post_start_app(GAME_PACKAGE).wait()
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            if foreground_package(self.controller) == GAME_PACKAGE:
                break
            time.sleep(0.5)
        reason = "成员下载超过60秒仍未进入演出，已主动跳车并返回游戏"
        record_failure_reason(reason)
        raise RuntimeError(reason)

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
        state, image = self.wait_for(names, timeout=timeout)
        if state is not None:
            return state
        if self.pipeline_box(image, "CooperativeHomeMarker") is not None:
            return "home"
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
            self.wait_for_playfield()
            return self.play()
        except (InterruptedError, MemberExited):
            raise
        except Exception as exc:
            if isinstance(exc, OSError) and "access violation" in str(exc).lower():
                # A second reverse-controller call can hide the original
                # invalid-handle failure behind another access violation.
                raise
            # The popup can arrive between two state-loop screenshots, for
            # example while the difficulty or speed gate is running. Convert
            # that race back into the configured member-exit policy.
            try:
                image = self.capture()
            except Exception:
                raise exc
            if self.visible(image, "member_exit_title", 0.93):
                raise MemberExited("协力成员退出房间") from exc
            raise

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
        self.ensure_room_page(timeout=15.0)
        return reconnects

    def run(self) -> bool:
        total = int(self.settings.get("count", 1))
        completed = 0
        reconnects = 0
        reuse_room = False
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
            if not success:
                return False

            completed += 1
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
                reuse_room = True
            elif not is_last:
                try:
                    self.return_to_room_selection()
                except MemberExited:
                    next_reconnects = self.handle_member_exit(reconnects)
                    if next_reconnects is None:
                        return False
                    reconnects = next_reconnects
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
