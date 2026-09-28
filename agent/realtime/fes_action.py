from __future__ import annotations

import json
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    from ..common_recover import CommonRecover
    from ..foreground_guard import GAME_PACKAGE
    from ..live_select import LiveSelectFind
    from ..task_reporting import TaskProgress, record_failure_reason
except ImportError:
    from common_recover import CommonRecover
    from foreground_guard import GAME_PACKAGE
    from live_select import LiveSelectFind
    from task_reporting import TaskProgress, record_failure_reason

from .difficulty_action import RealtimeDifficultySelect
from .game_effect_settings_action import _click as _maa_click
from .live_session import (
    append_current_run_event,
    current_live_run,
)
from .performance_settings_action import RealtimePerformanceSettingsGate
from .playfield_monitor import PlayfieldDetector
from .profile_play_action import RealtimeProfilePlay
from .profile_store import RealtimeProfileStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 与协力保持一致：准备页环境参数同时用于 Profile 预检，避免两处硬编码漂移。
FES_DPI = 240
FES_GAME_FPS = 60
FES_RENDER_QUALITY = "standard"
# Fes 活动只有 Easy/Normal/Hard/Expert 四档，没有 Special：UI 上仍有第五槽
# （实测 (946,574)），按需求 v1 不提供 Special、永不点击该槽。坐标来自雷电
# 模拟器 1280x720 真机录像 t=34s 霍夫圆实测（整行 y572-574），与协力选曲页
# DIFFICULTY_TARGETS(715..1180,545) 不同位，不可混用。
FES_DIFFICULTY_TARGETS = {
    "Easy": (602, 572),
    "Normal": (684, 574),
    "Hard": (770, 572),
    "Expert": (856, 574),
}
# 准备页底栏红色「准备完毕」按钮（HSV 实测 bbox 1014,590 218x81 → 中心）。
FES_READY_POINT = (1123, 630)
# 中继页底栏红色 OK = 自动匹配（HSV 实测 bbox 916,618 265x56 → 中心）。
FES_MATCH_OK_POINT = (1048, 646)
# 底栏「设定」gear（录像实测 ≈(946,648)）。
FES_GEAR_POINT = (946, 650)
# 满员后游戏自动进入最终确认页；匹配超时给出明确失败原因。
FES_MATCH_TIMEOUT_SECONDS = 300.0
# 点“准备完毕”后所有人点完才开演，最长 30 秒倒计时自动开演。
FES_READY_DEPARTURE_TIMEOUT_SECONDS = 90.0


class FesLifeJumpHome(RuntimeError):
    """Fes 生命归零：已切至模拟器桌面且游戏保留在后台，立即结束任务。"""


# 跳车局守卫：FesLiveFinalize 见到该标记必须跳过 CommonRecover，否则会
# 把正在桌面等玩家手动断网跳车的游戏拽回主页。每轮 run() 入口复位。
_JUMP_HOME_DONE = False

DEFAULT_SETTINGS: dict[str, object] = {
    "difficulty": "Expert",
    "count": 1,
    "debug_recording": False,
    "diagnostic_trace": True,
}
_SETTINGS = dict(DEFAULT_SETTINGS)
_SETTINGS_LOCK = threading.Lock()


def configure_fes_settings(params: dict[str, object]) -> dict[str, object]:
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
            raise ValueError("团队演出 Fes 次数必须是0到999的整数，0表示无限")
        candidate["count"] = count
        difficulty = str(candidate.get("difficulty", "Expert"))
        if difficulty not in FES_DIFFICULTY_TARGETS:
            raise ValueError(
                f"团队演出 Fes 不支持难度 {difficulty}；"
                f"可选：{'/'.join(FES_DIFFICULTY_TARGETS)}（没有 Special）"
            )
        candidate["difficulty"] = difficulty
        _SETTINGS.clear()
        _SETTINGS.update(candidate)
        return dict(_SETTINGS)


def current_fes_settings() -> dict[str, object]:
    with _SETTINGS_LOCK:
        return dict(_SETTINGS)


def fes_play_params(
    settings: dict[str, object],
    *,
    effective_difficulty: str | None = None,
) -> dict[str, object]:
    """构造 RealtimeProfilePlay 的演奏参数，run_mode=fes。

    骨架阶段沿用协力的演奏约束（等最终封面、成员下载窗口、结算导航）。
    协力专属机制只启用“生命归零跳车请求”：数值生命确认归零后引擎在
    归零帧立即停手并置位信号；切桌面保后台由 FesLiveFlow 外层执行
    （不 post_start_app 切回游戏），成员退出监听与结算后留在房间仍不启用。
    """
    return {
        "difficulty": str(effective_difficulty or settings["difficulty"]),
        "require_profile": True,
        "settings_gate_required": True,
        "debug_recording": bool(settings["debug_recording"]),
        "diagnostic_trace": bool(settings["diagnostic_trace"]),
        "duration_seconds": 600,
        "startup_timeout_seconds": 60,
        "dpi": FES_DPI,
        "game_fps": FES_GAME_FPS,
        "render_quality": FES_RENDER_QUALITY,
        "wait_for_completion": True,
        "completion_missing_frames": 30,
        "require_completion": True,
        "save_result_frame": True,
        "result_back_attempts": 30,
        "result_back_interval_seconds": 1.5,
        "continue_after_life_depleted": True,
        # 生命归零跳车请求（引擎侧与协力共用）：数值生命条确认归零帧
        # 回调一次并立即结束本局，绝不继续向判定线发送按压。
        "life_depleted_jump_request": True,
        # 并发最终封面确认：photogate 等待段 15Hz 观察封面、首拍锚点帧
        # 一次性裁决（确认→更新身份；未确认→保留准备页谱面降级），谱面
        # 预加载不再被阻塞等待卡住（约 5.7 秒），裁决先于首拍派发。
        "final_cover_concurrent": True,
        "run_mode": "fes",
        "confirm_final_cover": True,
        "native_prearm_deferred": True,
    }


def fes_profile_preflight(context: Context, difficulty: str) -> str | None:
    """任务一开始就校验 Profile 与环境签名，失败返回可读原因。

    与协力相同：把 Profile 解析提前到导航之前，避免自动化完成整段
    导航后才被准备页门禁拒绝。
    """
    from .rehearsal_action import frame_resolution
    from .profile_store import EnvironmentSignature, engine_from_native_flag

    store = RealtimeProfileStore(PROJECT_ROOT / "profiles")
    try:
        image = context.tasker.controller.post_screencap().wait().get()
    except Exception:
        # 控制器尚未就绪时无法构造签名，交给准备页门禁处理。
        return None
    options = store.runtime_options()
    signature = EnvironmentSignature(
        frame_resolution(image),
        FES_DPI,
        FES_GAME_FPS,
        FES_RENDER_QUALITY,
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


class FesLiveFlow:
    def __init__(
        self,
        context: Context,
        settings: dict[str, object],
        *,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        self.context = context
        self.settings = settings
        self.effective_difficulty = str(settings["difficulty"])
        self.progress_callback = progress_callback

    def stopped(self) -> bool:
        return bool(self.context.tasker.stopping)

    @property
    def controller(self):
        """Always return MaaFramework's current reverse-controller proxy."""
        return self.context.tasker.controller

    def capture(self):
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        return self.context.tasker.controller.post_screencap().wait().get()

    def _ocr_box(
        self,
        image,
        expected: str,
        *,
        roi: tuple[int, int, int, int] = (0, 0, 1280, 720),
        threshold: float = 0.4,
    ):
        """OCR 查找文本，命中返回 box，否则返回 None。

        识别点全部是界面固定文案，ROI 均来自真机 1280x720 录像实测，
        分辨率无关。
        """
        from maa.pipeline import JOCR, JRecognitionType

        result = self.context.run_recognition_direct(
            JRecognitionType.OCR,
            JOCR(expected=[expected], roi=roi, threshold=threshold),
            image,
        )
        if result and result.hit and result.box:
            return result.box
        return None

    @staticmethod
    def action_argv(params: dict[str, object]):
        return SimpleNamespace(
            custom_action_param=json.dumps(params, ensure_ascii=False),
        )

    def click(self, point: tuple[int, int]) -> None:
        _maa_click(self.context, point)

    def _in_fes_flow(self, image) -> bool:
        """当前是否已在 Fes 中继/匹配/确认流程中（锚点均来自真机录像）。"""
        return bool(
            self._ocr_box(
                image, "准备完毕", roi=(700, 500, 580, 220), threshold=0.4,
            )
            or self._ocr_box(
                image, "创建房间", roi=(0, 560, 1280, 160), threshold=0.4,
            )
            or self._ocr_box(
                image, "的成员匹配", roi=(0, 500, 1280, 220), threshold=0.4,
            )
            or self._ocr_box(
                image, "请选择难度", roi=(0, 380, 1280, 240), threshold=0.4,
            )
        )

    def _on_home(self, image) -> bool:
        result = self.context.run_recognition("FesHomeLive", image)
        return bool(result and result.hit)

    def _navigate_to_entry(self) -> None:
        """从主页进入团队演出入口：点“演出”→ 选择页 OCR 点“团队演出”。

        复用 pipeline 的 FesHomeLive 模板节点（home_live 模板已在真机录像
        实测 0.99 命中 FesHomeLive 靶区）与 LiveSelectFind 动作；多轮之间
        回主页后由 enter_room 调用，完成 pipeline 导航段的等价重放。
        """
        timeout = float(
            getattr(self, "entry_home_timeout_seconds", 30.0)
        )
        deadline = time.monotonic() + timeout
        entered = False
        while time.monotonic() < deadline:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            image = self.capture()
            result = self.context.run_recognition("FesHomeLive", image)
            if result and result.hit and result.box:
                box = result.box
                self.click(
                    (int(box.x + box.w // 2), int(box.y + box.h // 2))
                )
                time.sleep(1.0)
                entered = True
                break
            time.sleep(0.5)
        if not entered:
            raise RuntimeError(
                f"主页 {timeout:.0f} 秒内未找到“演出”入口"
                "（home_live 模板已在真机录像命中，入口可能被弹窗遮挡）"
            )
        argv = SimpleNamespace(custom_action_param=json.dumps({
            "expected": "团队演出",
            "roi": [0, 100, 1280, 620],
            "click": True,
            "timeout_ms": 15000,
            "interval_ms": 500,
            "missing_reason": (
                "选择演出页未找到团队演出 Fes 入口"
                "（活动未开放或 OCR 文本待校准）"
            ),
        }, ensure_ascii=False))
        if not LiveSelectFind().run(self.context, argv):
            raise RuntimeError("团队演出 Fes 入口点击失败")
        print("FesLive entry_navigation=clicked", flush=True)

    def enter_room(self) -> None:
        """自动匹配入房（v1 只支持自动匹配，不做创建/加入私人房间）。

        点击活动入口后先落到中继页：底栏红色 OK 即自动匹配（实测中心
        (1048,646)），点击后游戏自动匹配房间；满员后自动进入选曲揭晓与
        「准备完毕」最终确认页，全程无需手动点击。这里以「准备完毕」出现
        为入房成功信号，超时给出明确失败原因。
        """
        timeout = float(
            getattr(self, "match_timeout_seconds", FES_MATCH_TIMEOUT_SECONDS)
        )
        started = time.monotonic()
        deadline = started + timeout
        image = self.capture()
        if not self._in_fes_flow(image) and self._on_home(image):
            # 多轮之间回主页后由这里重新导航进活动；首轮 pipeline 已点击
            # 入口或处于页面过渡时（既不在流程也不在主页）直接进等待循环。
            self._navigate_to_entry()
        last_state = ""
        ok_clicked = False
        while True:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            image = self.capture()
            if self._ocr_box(
                image, "准备完毕", roi=(700, 500, 580, 220), threshold=0.4,
            ):
                print(
                    "FesLive room=confirmed entry=auto-match "
                    f"elapsed={time.monotonic() - started:.1f}s",
                    flush=True,
                )
                return
            if not ok_clicked and self._ocr_box(
                image, "创建房间", roi=(0, 560, 1280, 160), threshold=0.4,
            ):
                self.click(FES_MATCH_OK_POINT)
                ok_clicked = True
                print(
                    "FesLive match_ok_tapped=true "
                    f"point=({FES_MATCH_OK_POINT[0]},{FES_MATCH_OK_POINT[1]})",
                    flush=True,
                )
                state = "hub"
            elif self._ocr_box(
                image, "的成员匹配", roi=(0, 500, 1280, 220), threshold=0.4,
            ):
                state = "matching"
            elif self._ocr_box(
                image, "请选择难度", roi=(0, 380, 1280, 240), threshold=0.4,
            ):
                state = "reveal"
            else:
                state = "roster-or-loading"
            if state != last_state:
                print(
                    f"FesLive room_state={state} "
                    f"elapsed={time.monotonic() - started:.1f}s",
                    flush=True,
                )
                last_state = state
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"进入团队演出后 {timeout:.0f} 秒内未到达最终确认页"
                    "（自动匹配未完成或房间未满员）"
                )
            time.sleep(1.0)

    def prepare(self) -> None:
        difficulty = str(self.settings["difficulty"])
        difficulty_params = {
            "difficulty": difficulty,
            "max_attempts": 3,
            "verify_delay_seconds": 0.25,
            "identity_read_attempts": 2,
            "identity_retry_delay_seconds": 0.15,
            "difficulty_targets": FES_DIFFICULTY_TARGETS,
            # 曲名/等级 ROI：等级 ROI 真机实测读出 27，曲名与协力同版式。
            "song_level_roi": (130, 580, 56, 38),
            "song_title_roi": (105, 535, 290, 52),
            "song_identity": False,
            "mode": "fes",
            "debug_recording": bool(self.settings["debug_recording"]),
        }
        if difficulty == "Special":
            # Fes 没有 Special；configure 阶段已拦截，这里保留兜底日志。
            raise ValueError("团队演出 Fes 没有 Special 难度")
        if not RealtimeDifficultySelect().run(
            self.context, self.action_argv(difficulty_params)
        ):
            raise RuntimeError(
                f"团队演出 Fes 准备页未能选择并复核 {difficulty} 难度"
            )
        run = current_live_run()
        if run is None or not run.prepared_for_play:
            raise RuntimeError("团队演出 Fes 难度选择成功但缺少本局实际难度证据")
        effective_difficulty = str(run.difficulty)
        if effective_difficulty != difficulty:
            raise RuntimeError(
                "团队演出 Fes 实际难度与请求不一致："
                f"请求 {difficulty}，实际 {effective_difficulty}"
            )
        self.effective_difficulty = effective_difficulty

        performance_params = {
            "difficulty": effective_difficulty,
            "require_profile": True,
            "dpi": FES_DPI,
            "game_fps": FES_GAME_FPS,
            "render_quality": FES_RENDER_QUALITY,
            "coordinates": {"gear": FES_GEAR_POINT},
            "defer_native_prearm": True,
            "cache_preparation_image": True,
        }
        if not RealtimePerformanceSettingsGate().run(
            self.context, self.action_argv(performance_params)
        ):
            raise RuntimeError("团队演出 Fes 准备页流速复核失败")
        print(
            "FesLive prepare=ready "
            f"requested_difficulty={difficulty} "
            f"effective_difficulty={effective_difficulty} "
            "speed_gate=verified",
            flush=True,
        )
        self._ready_up_and_wait()

    def _ready_up_and_wait(self) -> None:
        """点击「准备完毕」并等待开演：所有人点完即开演，最长 30 秒倒计时
        自动开演；无论哪种情况游戏都会进入加载，本方法以「NOW LOADING」
        出现为离开信号返回，后续交给 RealtimeProfilePlay 等待加载与演奏。
        """
        timeout = float(
            getattr(
                self,
                "ready_departure_timeout_seconds",
                FES_READY_DEPARTURE_TIMEOUT_SECONDS,
            )
        )
        image = self.capture()
        if self._ocr_box(
            image, "准备完毕", roi=(700, 500, 580, 220), threshold=0.4,
        ) is None:
            raise RuntimeError(
                "最终确认页未找到“准备完毕”按钮（OCR 未命中，待真机复核）"
            )
        self.click(FES_READY_POINT)
        print(
            "FesLive ready_tapped=true "
            f"point=({FES_READY_POINT[0]},{FES_READY_POINT[1]})",
            flush=True,
        )
        # 兜底开演检测：NOW LOADING 被漏掉/跳过时（真机 2026-09-27 00:10 局，
        # ready 等待挂死 90s、引擎从未启动 → 不读谱 + 生命归零不跳桌面），直接
        # 认“已进入演奏场”交棒给 play()。确认页是名册+难度、无 7 轨白色判定线，
        # PlayfieldDetector（生命条 + ≥6 轨白判定）不会误触发；即便极端误触发，
        # play() 的 photogate 也会在真首音前静默等待，优雅降级不致挂死。
        playfield_detector = PlayfieldDetector()
        playfield_seen = 0
        started = time.monotonic()
        deadline = started + timeout
        next_heartbeat_s = 10.0
        while time.monotonic() < deadline:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            time.sleep(0.5)
            image = self.capture()
            if self._ocr_box(
                image, "NOW LOADING", roi=(0, 300, 1280, 300), threshold=0.4,
            ):
                print(
                    "FesLive ready_departed=true signal=loading "
                    f"elapsed={time.monotonic() - started:.1f}s",
                    flush=True,
                )
                return
            # 兜底：loading 没等到但演奏场已成立 = 已开演，立即交棒，绝不让
            # ready 等待把引擎饿死（引擎不启动 → 不读谱 + 生命归零不跳桌面）。
            # 连续 2 帧确认，滤掉转场瞬间的误命中。
            if playfield_detector(image):
                playfield_seen += 1
                if playfield_seen >= 2:
                    print(
                        "FesLive ready_departed=true signal=playfield "
                        f"elapsed={time.monotonic() - started:.1f}s",
                        flush=True,
                    )
                    return
            else:
                playfield_seen = 0
            elapsed = time.monotonic() - started
            if elapsed >= next_heartbeat_s:
                # 真机 2026-09-26 02:12 局：点完 42s 零输出，房间未满/点击
                # 未生效/被弹窗挡住三种死法外观相同，人工无从判读。每 10s
                # 打一行在场证据，静默等待变成可诊断状态。
                ready_visible = bool(self._ocr_box(
                    image,
                    "准备完毕",
                    roi=(700, 500, 580, 220),
                    threshold=0.4,
                ))
                hint = (
                    "确认页仍在（准备按钮可见）"
                    if ready_visible
                    else "确认页已变化（可能已就绪等他人或被弹窗遮挡）"
                )
                print(
                    f"FesLive ready_waiting elapsed={elapsed:.0f}s "
                    "now_loading=false "
                    f"ready_button={str(ready_visible).lower()} "
                    f"hint={hint}",
                    flush=True,
                )
                next_heartbeat_s += 10.0
        raise RuntimeError(
            f"点击准备完毕后 {timeout:.0f} 秒内未离开最终确认页"
            "（其他玩家未准备且倒计时未触发，或触控未送达）"
        )

    def _jump_home_desktop(self) -> None:
        """KEYCODE_HOME 切至模拟器桌面；游戏保留在后台，不重启不切回。

        与协力版的关键差异：协力随后 post_start_app 切回游戏再由玩家
        操作，Fes 版按需求留在桌面——游戏后台的演出现场就是玩家手动
        断网跳车的窗口，任何失败都不允许触发重试/恢复把它拽回去。
        """
        global _JUMP_HOME_DONE
        if _JUMP_HOME_DONE:
            return
        try:
            append_current_run_event(
                PROJECT_ROOT,
                "jump",
                "home-desktop",
                details={
                    "mode": "fes",
                    "reason": "life-depleted",
                    "relaunch_game": False,
                },
            )
        except Exception as evidence_error:
            print(
                "FesLive jump_evidence_failed="
                f"{type(evidence_error).__name__}: {evidence_error}",
                flush=True,
            )
        try:
            self.controller.post_click_key(3).wait()
        except Exception as exc:
            # 切桌面失败也绝不允许走重试/恢复：游戏必须留在演出现场。
            print(
                f"FesLive jump_home_failed={type(exc).__name__}: {exc}",
                flush=True,
            )
        else:
            # 留 0.6s 给桌面渲染（与协力跳车同一节奏），也让玩家看清交接。
            time.sleep(0.6)
            print("FesLive jump_home=true relaunch_game=false", flush=True)
        _JUMP_HOME_DONE = True

    def play(self) -> bool:
        params = fes_play_params(
            self.settings,
            effective_difficulty=getattr(
                self,
                "effective_difficulty",
                str(self.settings.get("difficulty", "Expert")),
            ),
        )
        success = bool(
            RealtimeProfilePlay().run(self.context, self.action_argv(params))
        )
        run = current_live_run()
        if run is not None and bool(run.disconnect_jump_requested):
            # 生命归零：引擎已在归零帧停手并置位跳车信号。只切桌面、
            # 不 post_start_app——游戏留在后台，由玩家手动断网跳车。
            self._jump_home_desktop()
            raise FesLifeJumpHome("生命归零，已切至模拟器桌面，请手动断网跳车")
        return success

    def run_attempt(self) -> bool:
        self.enter_room()
        self.prepare()
        return self.play()

    def recover_after_play_failure(self, reason: str) -> None:
        """完整清理失败单局并从主页重新进入活动，禁止在旧会话中续跑。"""
        from .native_prearm import discard_prearmed_backend

        discard_prearmed_backend("fes-play-retry")
        recovery_params = {
            "home_node": "FesHomeMarker",
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
                f"团队演出 Fes 单局失败后无法恢复主页：{reason}"
            )

    def run(self) -> bool:
        global _JUMP_HOME_DONE
        # 逐任务复位：上一局的跳车标记不得让后续任务跳过正常主页恢复。
        _JUMP_HOME_DONE = False
        total = int(self.settings.get("count", 1))
        completed = 0
        play_failures = 0
        retry_count = max(
            0,
            min(99, int(self.settings.get("play_failure_retry_count", 0))),
        )
        while total == 0 or completed < total:
            if self.stopped():
                return True
            print(
                f"FesLive round={completed + 1}/{total or '无限'}",
                flush=True,
            )
            try:
                success = self.run_attempt()
            except InterruptedError:
                raise
            except FesLifeJumpHome as exc:
                # 跳车已切桌面：记录可读原因结束任务，禁止重试与恢复。
                record_failure_reason(str(exc))
                print(
                    f"[任务][团队演出 Fes][流程][ERROR] {exc}",
                    flush=True,
                )
                return False
            except Exception as exc:
                jump_run = current_live_run()
                if jump_run is not None and bool(
                    jump_run.disconnect_jump_requested
                ):
                    # 归零跳车信号已置位后的后续异常（原生门禁/清理等）
                    # 不许进重试：recover_after_play_failure 会把游戏从
                    # 桌面拽回主页，破坏手动断网跳车窗口。
                    self._jump_home_desktop()
                    failure = FesLifeJumpHome(
                        "生命归零，已切至模拟器桌面，请手动断网跳车"
                        f"（{type(exc).__name__}: {exc}）"
                    )
                    record_failure_reason(str(failure))
                    print(
                        f"[任务][团队演出 Fes][流程][ERROR] {failure}",
                        flush=True,
                    )
                    return False
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
                            "mode": "fes",
                            "attempt": play_failures + 1,
                            "attempt_limit": retry_count + 1,
                            "reason": reason,
                        },
                    )
                except Exception as evidence_error:
                    print(
                        f"FesLive retry_evidence_failed="
                        f"{type(evidence_error).__name__}: {evidence_error}",
                        flush=True,
                    )
                print(
                    "FesLive play_retry=true "
                    f"attempt={play_failures + 1}/{retry_count + 1} "
                    f"reason={reason}",
                    flush=True,
                )
                self.recover_after_play_failure(reason)
                continue
            if self.stopped():
                return True
            if not success:
                if play_failures >= retry_count:
                    return False
                play_failures += 1
                reason = "RealtimeProfilePlay 返回失败"
                print(
                    "FesLive play_retry=true "
                    f"attempt={play_failures + 1}/{retry_count + 1} "
                    f"reason={reason}",
                    flush=True,
                )
                self.recover_after_play_failure(reason)
                continue

            completed += 1
            play_failures = 0
            callback = getattr(self, "progress_callback", None)
            if callback is not None:
                try:
                    callback(completed, total)
                except Exception as exc:
                    print(
                        f"FesLive progress_warning={type(exc).__name__}: {exc}",
                        flush=True,
                    )
            if self.stopped():
                return True
            if not (total == 0 or completed < total):
                break
            # 多轮之间回主页再重新导航，保持每一轮的起点可预期。
            try:
                self.recover_after_play_failure("进入下一轮")
            except InterruptedError:
                raise
            except Exception as exc:
                print(
                    f"FesLive post_round_recover_warning={type(exc).__name__}: {exc}",
                    flush=True,
                )
        return True


@AgentServer.custom_action("FesLiveConfigure")
class FesLiveConfigure(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        if context.tasker.stopping:
            return True
        try:
            settings = configure_fes_settings(
                json.loads(argv.custom_action_param or "{}")
            )
            print(f"FesLive configured={settings}", flush=True)
            return True
        except Exception as exc:
            record_failure_reason(
                f"团队演出 Fes 选项无效：{type(exc).__name__}: {exc}"
            )
            traceback.print_exc()
            return False


@AgentServer.custom_action("FesLiveFlow")
class FesLiveAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            if context.tasker.stopping:
                return True
            settings = current_fes_settings()
            settings["play_failure_retry_count"] = int(
                RealtimeProfileStore(
                    PROJECT_ROOT / "profiles"
                ).runtime_options().get("play_failure_retry_count", 1)
            )
            preflight_error = fes_profile_preflight(
                context, str(settings["difficulty"])
            )
            if preflight_error is not None:
                record_failure_reason(preflight_error)
                print(
                    f"[任务][团队演出 Fes][流程][ERROR] {preflight_error}",
                    flush=True,
                )
                return False

            def progress_argv(phase: str, total: int):
                return SimpleNamespace(
                    custom_action_param=json.dumps(
                        {
                            "task_name": "FesLive",
                            "label": "团队演出 Fes",
                            "total": total,
                            "phase": phase,
                        },
                        ensure_ascii=False,
                    ),
                    task_detail=getattr(argv, "task_detail", None),
                    node_name=getattr(argv, "node_name", "FesRun"),
                )

            total = int(settings["count"])
            if not TaskProgress().run(context, progress_argv("start", total)):
                raise RuntimeError("团队演出 Fes 次数初始化失败")

            def report_progress(_completed: int, expected_total: int) -> None:
                if not TaskProgress().run(
                    context,
                    progress_argv("completed", expected_total),
                ):
                    raise RuntimeError("团队演出 Fes 次数进度记录失败")

            return FesLiveFlow(
                context,
                settings,
                progress_callback=report_progress,
            ).run()
        except InterruptedError:
            return True
        except Exception as exc:
            if context.tasker.stopping:
                return True
            reason = f"团队演出 Fes 失败：{type(exc).__name__}: {exc}"
            record_failure_reason(reason)
            traceback.print_exc()
            print(f"[任务][团队演出 Fes][流程][ERROR] {reason}", flush=True)
            return False


@AgentServer.custom_action("FesLiveFinalize")
class FesLiveFinalize(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            if context.tasker.stopping:
                return True
            if _JUMP_HOME_DONE:
                # 跳车局玩家正在桌面手动断网：CommonRecover 会把游戏从
                # 后台拽回主页，必须跳过。正常链路 FesRun 失败即止走不到
                # 这里，此守卫覆盖 ContinueRunningWhenError 仍开启的部署。
                print(
                    "FesLive finalize_skipped=jump-home-desktop",
                    flush=True,
                )
                return True
            if not CommonRecover().run(context, argv):
                print(
                    "FesLive finalize_warning=演出已完成，主页恢复失败不终止任务",
                    flush=True,
                )
            return True
        except Exception as exc:
            if context.tasker.stopping:
                return True
            reason = f"团队演出 Fes 结束导航失败：{type(exc).__name__}: {exc}"
            traceback.print_exc()
            print(f"FesLive finalize_warning={reason}", flush=True)
            return True
