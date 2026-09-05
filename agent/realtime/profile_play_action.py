from __future__ import annotations

import json
import math
import time
import traceback
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path

import cv2

try:
    from ..foreground_guard import require_game_foreground
    from ..task_reporting import record_failure_reason
except ImportError:  # AgentServer imports realtime as a top-level package.
    from foreground_guard import require_game_foreground
    from task_reporting import record_failure_reason

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from .controller_touch import ControllerTouchDispatcher
from .debug_recorder import RealtimeDebugRecorder, append_lifecycle_event
from .engine import EngineStats, RealtimeEngine
from .final_cover import FinalCoverResolution, FinalCoverResolver
from .game_effect_settings_action import verified_game_visual_settings
from .life_monitor import LifeDetector, LifeGuard, PlayfieldCompletionGuard
from .live_failed_detector import (
    LiveFailedPopupDetector,
    exit_failed_live,
)
from .live_session import (
    LiveRunContext,
    current_live_run,
    reset_live_run,
    update_live_run,
)
from .note_detector import NoteDetector
from .profile_action import PROJECT_ROOT
from .profile_store import (
    EnvironmentSignature,
    RealtimeProfileStore,
    RuntimeSettings,
)
from .rehearsal_action import frame_resolution
from .result_navigation import (
    RESULT_ANIMATION_SKIP_POINT,
    ResultNavigationStatus,
    accelerated_back,
    back_then_click,
    navigate_result_pages,
)
from .result_parser import LiveResult, ResultParser, adjusted_timing_offset
from .run_reporting import (
    PreflightPerformanceSnapshot,
    result_report_payload as _result_report_payload,
    write_json_atomic as _write_json_atomic,
    write_preflight_terminal_result,
)
from .timing_feedback import AdaptiveTimingController, TimingFeedbackDetector
from .touch_planner import RealtimePlanner, sliding_holds_enabled
from .runtime_options import debug_enabled, diagnostic_trace_enabled
from .performance_settings_action import verified_settings
from .chart_repository import ChartResolution, LocalChartRepository
from .native_prearm import (
    consume_prearmed_backend,
    controller_adb_endpoint,
    discard_prearmed_backend,
    prepare_native_for_settings_gate,
    resolve_confirmed_chart,
)
from .playfield_monitor import PlayfieldDetector, PlayfieldLifecycleMonitor


REWARD_CONFIRM_TEMPLATE = PROJECT_ROOT / "resource" / "image" / "result_reward_confirm.png"
REWARD_OK_TEMPLATE = PROJECT_ROOT / "resource" / "image" / "result_reward_ok.png"
RESULT_RANK_NEXT_TEMPLATE = (
    PROJECT_ROOT / "resource" / "image" / "result_rank_next.png"
)
JUDGEMENT_DETAILS_TEMPLATE = (
    PROJECT_ROOT / "resource" / "image" / "result_judgement_details.png"
)
ACTIVITY_POINTS_TEMPLATE = (
    PROJECT_ROOT / "resource" / "image" / "result_activity_points.png"
)
ACHIEVEMENT_LIST_CLOSE_TEMPLATE = (
    PROJECT_ROOT / "resource" / "image" / "common_close.png"
)
# Kept as a compatibility alias for callers/tests that override this template.
RESULT_NEXT_TEMPLATE = RESULT_RANK_NEXT_TEMPLATE
REWARD_TEMPLATE_THRESHOLD = 0.85
REWARD_DISMISS_LIMIT = 4
REWARD_CLICK_DELAY_SECONDS = 1.0
ACHIEVEMENT_LIST_CLOSE_TEMPLATE_THRESHOLD = 0.9
ACHIEVEMENT_LIST_CLOSE_CLICK_LIMIT = 2
ACHIEVEMENT_LIST_CLOSE_CLICK_DELAY_SECONDS = 1.0
RESULT_NEXT_TEMPLATE_THRESHOLD = 0.9
RESULT_NEXT_CLICK_LIMIT = 2
RESULT_NEXT_CLICK_DELAY_SECONDS = 1.0
# The judgement labels animate in before their final colours settle.  The
# captured 2026-08-31 loading frame scores 0.776 against the final marker,
# then 0.999 once the counts appear.  Keep the lower loading threshold safe by
# searching only the fixed judgement-panel region below.
JUDGEMENT_DETAILS_TEMPLATE_THRESHOLD = 0.75
JUDGEMENT_DETAILS_MARKER_REGION = (0.58, 0.34, 0.68, 0.70)
ACTIVITY_POINTS_TEMPLATE_THRESHOLD = 0.9
ACTIVITY_POINTS_CLICK_DELAY_SECONDS = 1.0
ACTIVITY_POINTS_CLICK_LIMIT = 2
# The activity-points template is a page identity marker in the score panel,
# not an actionable control.  The pink confirmation button occupies this
# stable normalised position on the 1280x720 result layout (1067, 644).
ACTIVITY_POINTS_CONFIRM_X_RATIO = 1067 / 1280
ACTIVITY_POINTS_CONFIRM_Y_RATIO = 644 / 720
# Compatibility alias retained for callers/tests that imported the old name.
COOPERATIVE_RESULT_ANIMATION_SKIP_POINT = RESULT_ANIMATION_SKIP_POINT
# The same white "confirm" control appears throughout the result UI.  A real
# modal acknowledgement is always centred in the lower part of the 1280x720
# screen; score-page achievement entries live outside this region.  Template
# text alone is therefore never sufficient to classify a reward popup.
REWARD_POPUP_BUTTON_REGION = (0.40, 0.68, 0.60, 0.90)
# Song achievement details have a dedicated Chinese "close" control at the
# bottom centre.  Recognising this page lets result collection recover from a
# stale/legacy accidental navigation without pressing Back or guessing.
ACHIEVEMENT_LIST_CLOSE_REGION = (0.40, 0.80, 0.60, 0.94)


_LAST_LIFE_SAFETY_ABORT = False


def _native_adb_endpoint(controller) -> tuple[str, str]:
    """从当前 Maa controller 取 Native 设备端点，缺失时禁止猜测本机路径。"""
    return controller_adb_endpoint(controller)


def _completion_missing_frames(
    configured_frames: int,
    *,
    native: bool,
    target_fps: int,
) -> int:
    """Native 降至 5Hz 后保持结算缺帧门槛的原有时长。"""
    frames = max(1, int(configured_frames))
    if not native:
        return frames
    return max(1, math.ceil(frames * 5.0 / max(1, int(target_fps))))


def _native_execution_gate_failures(
    native_report: dict[str, object],
) -> list[str]:
    """返回 Native 完整性门禁失败项；空列表才允许进入结算解析。"""
    planned = int(native_report.get("planned", 0))
    sent = int(native_report.get("sent", 0))
    executed = int(native_report.get("executed", 0))
    underflows = int(native_report.get("underflows", 0))
    failures = []
    state = str(native_report.get("state") or "<missing>").lower()
    session_state = str(
        native_report.get("session_state") or "<missing>"
    ).lower()
    game_terminal = str(
        native_report.get("game_terminal_reason") or ""
    )
    # 生命归零是游戏自身的终态：弹窗出现后引擎主动取消会话，剩余谱面动作
    # 不会、也不应继续派发。此时 cancelled 会话与不完整的设备回读是预期，
    # 只要触点已确认释放且无传输/设备错误，就不算技术失败。
    life_failed_stop = "演出失败" in game_terminal
    if (
        (state != "finished" or session_state != "finished")
        and not life_failed_stop
    ):
        failures.append(
            f"terminal_state={state} session_state={session_state}"
        )
    if (
        planned <= 0 or sent != planned or executed != planned
    ) and not life_failed_stop:
        failures.append(
            f"planned/sent/executed={planned}/{sent}/{executed}"
        )
    if (
        not bool(native_report.get("executed_observation_complete", False))
        and not life_failed_stop
    ):
        failures.append(
            "device evidence incomplete: "
            + str(native_report.get("executed_observation_reason", "unknown"))
        )
    if underflows != 0:
        failures.append(f"queue_underflows={underflows}")
    for field in (
        "publisher_error",
        "publish_error",
        "device_error",
        "observation_error",
        "release_error",
    ):
        value = native_report.get(field)
        if value:
            failures.append(f"{field}={value}")
    if native_report.get("release_confirmed") is not True:
        failures.append("release_confirmed=false")
    try:
        stop_latency_ms = float(native_report["stop_latency_ms"])
    except (KeyError, TypeError, ValueError):
        failures.append("stop_latency_ms=invalid")
    else:
        if not math.isfinite(stop_latency_ms) or stop_latency_ms > 500.0:
            failures.append(f"stop_latency_ms={stop_latency_ms}")
    return failures


def resolve_local_chart_for_run(
    live_run: LiveRunContext | None,
    difficulty: str,
    *,
    repository: LocalChartRepository | None = None,
) -> ChartResolution:
    """只解析本轮准备页已经确认过的本地谱面。"""
    return resolve_confirmed_chart(
        live_run,
        difficulty,
        repository=repository,
        project_root=PROJECT_ROOT,
    )


def _effective_native_chart_selection(
    selected: Any,
    prepared: Any,
) -> Any:
    """校验最终封面谱面与 Native 预武装谱面，返回本轮实际消费的谱面。

    协力漏键抖动会基于同一首歌生成 run 特定的 jittered 副本；副本路径
    不同但歌曲身份一致，必须以预武装副本为准，否则正式消费会因缓存键
    不一致而失败。真正的歌曲/难度不一致仍必须硬失败。
    """
    if prepared is None:
        raise RuntimeError("最终封面确认后的 Native 预武装谱面不一致")
    if getattr(prepared, "path", None) == getattr(selected, "path", None):
        return selected
    same_song = (
        getattr(prepared, "bestdori_song_id", None)
        == getattr(selected, "bestdori_song_id", None)
        and str(getattr(prepared, "difficulty", "")).strip().lower()
        == str(getattr(selected, "difficulty", "")).strip().lower()
    )
    prepared_level = getattr(prepared, "level", None)
    selected_level = getattr(selected, "level", None)
    same_level = (
        prepared_level is None
        or selected_level is None
        or int(prepared_level) == int(selected_level)
    )
    if not same_song or not same_level:
        raise RuntimeError("最终封面确认后的 Native 预武装谱面不一致")
    return prepared


@dataclass(frozen=True, slots=True)
class FinalCoverWaitOutcome:
    status: str
    resolution: FinalCoverResolution | None
    reason: str
    frames: int
    playfield_seen: bool
    image: object | None = None


BLACK_BURST_SECONDS = 0.6


def _frame_is_black(image) -> bool:
    """识别协力进入演出前的整屏黑场转场。"""
    if image is None or getattr(image, "ndim", 0) != 3:
        return False
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return bool(float(gray.mean()) < 6.0 and float(gray.std()) < 6.0)


def wait_for_final_cover(
    controller,
    live_run: LiveRunContext,
    selection,
    difficulty: str,
    stopping,
    *,
    repository: LocalChartRepository | None = None,
    timeout_seconds: float = 60.0,
    poll_interval_seconds: float = 0.02,
    observer=None,
    fallback_selection_available: bool | None = None,
) -> FinalCoverWaitOutcome:
    """确认最终封面；识别缺失时保留准备页谱面或降级到视觉演奏。"""
    if not 1 <= float(timeout_seconds) <= 180:
        raise ValueError("final_cover_timeout_seconds 必须在 1..180 之间")
    resolver = FinalCoverResolver(
        difficulty=difficulty,
        observed_level=live_run.song_level,
        observed_title=live_run.song_title,
        selection=selection,
        repository=(
            repository
            if selection is None else None
        ),
    )
    evidence_reason = resolver.evidence_reason()
    if evidence_reason is not None:
        raise RuntimeError(f"最终封面确认缺少准备页证据：{evidence_reason}")
    playfield_detector = PlayfieldDetector()
    playfield_streak = 0
    black_burst_until = float("-inf")
    last_image = None
    can_keep_selection = (
        selection is not None
        if fallback_selection_available is None
        else bool(fallback_selection_available)
    )
    deadline = time.monotonic() + float(timeout_seconds)
    while time.monotonic() < deadline:
        if stopping():
            raise InterruptedError("用户已停止任务")
        image = controller.post_screencap().wait().get()
        last_image = image
        now_mono = time.monotonic()
        if _frame_is_black(image):
            # 协力在封面出现前会先整屏黑一下，随后封面或演奏场淡入。黑场
            # 清空演奏场计数，并进入一小段无 sleep 的密集采样窗口，给短暂
            # 出现的封面留出匹配机会，而不是在淡入首帧就放弃。
            playfield_streak = 0
            black_burst_until = now_mono + BLACK_BURST_SECONDS
        resolution = resolver.observe(image)
        playfield_streak = (
            playfield_streak + 1 if playfield_detector(image) else 0
        )
        if observer is not None:
            observer(
                image,
                now_mono,
                {
                    "event": "final_cover_observation",
                    "status": "confirmed" if resolution is not None else "observing",
                    "frames": resolver.frames,
                    "playfield_streak": playfield_streak,
                    "reason": resolver.last_reason,
                },
            )
        if resolution is not None:
            print(
                "RealtimeFinalCover confirmed=true "
                "bestdori_song_id="
                f"{resolution.confirmation.bestdori_song_id} "
                f"frames={resolver.frames}",
                flush=True,
            )
            return FinalCoverWaitOutcome(
                status="confirmed",
                resolution=resolution,
                reason="confirmed",
                frames=resolver.frames,
                playfield_seen=playfield_streak > 0,
                image=image,
            )
        if playfield_streak >= 2 and now_mono >= black_burst_until:
            status = (
                "degraded-selected-chart"
                if can_keep_selection else "degraded-visual-legacy"
            )
            print(
                "RealtimeFinalCover confirmed=false fallback="
                f"{status} frames={resolver.frames} reason={resolver.last_reason}",
                flush=True,
            )
            return FinalCoverWaitOutcome(
                status=status,
                resolution=None,
                reason=resolver.last_reason,
                frames=resolver.frames,
                playfield_seen=True,
                image=image,
            )
        if poll_interval_seconds > 0 and now_mono >= black_burst_until:
            time.sleep(float(poll_interval_seconds))
    status = (
        "degraded-selected-chart"
        if can_keep_selection else "degraded-visual-legacy"
    )
    print(
        "RealtimeFinalCover confirmed=false fallback="
        f"{status} frames={resolver.frames} timeout=true reason={resolver.last_reason}",
        flush=True,
    )
    return FinalCoverWaitOutcome(
        status=status,
        resolution=None,
        reason=(
            f"{float(timeout_seconds):g} 秒内未确认最终歌曲封面："
            f"{resolver.last_reason}"
        ),
        frames=resolver.frames,
        playfield_seen=False,
        image=last_image,
    )


class StallSafeCapture:
    """Screencap wrapper that never blocks the engine for a full stall.

    LDPlayer's EmulatorExtras screencap can freeze for 200-400 ms under
    load.  A blocking capture stalls the whole engine loop, so every note
    due during that window goes unhit and the song fails.  This wrapper
    double-buffers: it returns the latest completed frame immediately and
    posts the next capture right away so the screencap overlaps the engine's
    detection/planning work.  When the backend is stuck, the wrapper reuses
    the last completed frame instead of blocking, so the engine clock and
    the chart-timeline after-due rescues keep advancing.
    """

    def __init__(self, controller, *, timeout_seconds: float = 0.05):
        self._controller = controller
        self._timeout_seconds = float(timeout_seconds)
        self._last_image = None
        self._pending = None
        self.stall_count = 0

    @staticmethod
    def _job_done(job) -> bool:
        try:
            return bool(job.done)
        except Exception:
            return True

    def __call__(self):
        if self._pending is not None and self._job_done(self._pending):
            try:
                image = self._pending.get()
                if image is not None:
                    self._last_image = image
            except Exception:
                pass
            self._pending = None
        if self._pending is None:
            # Start the next capture immediately so it overlaps the engine's
            # detection/planning work (true double buffering).
            self._pending = self._controller.post_screencap()
        if self._last_image is None and not self._job_done(self._pending):
            # The very first frame must exist before the detector can run;
            # blocking once here is unavoidable and only happens at startup.
            self._pending.wait()
            self._last_image = self._pending.get()
            self._pending = self._controller.post_screencap()
            return self._last_image
        if self._job_done(self._pending):
            try:
                image = self._pending.get()
            except Exception:
                image = None
            if image is None:
                if self._last_image is None:
                    # First frame must exist before the detector can run.
                    image = self._controller.post_screencap().wait().get()
                else:
                    image = self._last_image
            self._last_image = image
            # Pre-post the next capture for the following frame.
            self._pending = self._controller.post_screencap()
            return image
        # The in-flight capture has not finished: reuse the last completed
        # frame so the engine clock and chart rescues keep advancing.
        self.stall_count += 1
        return self._last_image

    @property
    def last_image(self):
        """Latest completed capture, retained for terminal diagnostics."""
        return self._last_image


def _run_mode(params: dict, *, is_rehearsal: bool) -> str:
    explicit = params.get("run_mode")
    if explicit:
        return str(explicit)
    if params.get("visual_evaluation"):
        return "visual-evaluation"
    if params.get("calibration_report"):
        return "calibration"
    if params.get("ignore_note_speed"):
        return "continuous"
    return "rehearsal" if is_rehearsal else "formal"


def _relative_artifact_path(path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _recorder_checkpoint(
    recorder,
    image,
    phase: str,
    status: str,
    *,
    details: dict[str, object] | None = None,
) -> None:
    method = getattr(recorder, "save_checkpoint", None)
    if callable(method):
        method(image, phase, status, details=details)


def _recorder_update_metadata(recorder, live_run: LiveRunContext) -> None:
    method = getattr(recorder, "update_session_metadata", None)
    if callable(method):
        method(live_run.to_mapping())


def resolve_life_policy(
    params: dict,
    runtime_options: dict,
) -> tuple[bool, bool, int | None]:
    """Return rehearsal mode, continue-after-depletion and safety threshold."""
    require_profile = bool(params.get("require_profile", True))
    is_rehearsal = bool(params.get("rehearsal_mode", not require_profile))
    ignore_rehearsal_life = bool(
        runtime_options.get("rehearsal_ignore_life_safety", True)
    )
    continue_after_depleted = bool(params.get(
        "continue_after_life_depleted",
        is_rehearsal and ignore_rehearsal_life,
    ))
    default_use_safety = not (is_rehearsal and ignore_rehearsal_life)
    use_life_safety = bool(params.get("use_life_safety", default_use_safety))
    life_threshold = (
        int(runtime_options["life_exit_threshold"])
        if use_life_safety and runtime_options["life_safety_enabled"] else None
    )
    return is_rehearsal, continue_after_depleted, life_threshold


def resolve_life_monitor_enabled(
    params: dict,
    runtime_options: dict,
) -> bool:
    """只有用户启用生命保护且本次流程允许时才读取数值生命值。"""
    require_profile = bool(params.get("require_profile", True))
    is_rehearsal = bool(params.get("rehearsal_mode", not require_profile))
    ignore_rehearsal_life = bool(
        runtime_options.get("rehearsal_ignore_life_safety", True)
    )
    default_use_safety = not (is_rehearsal and ignore_rehearsal_life)
    return bool(
        params.get("use_life_safety", default_use_safety)
        and runtime_options.get("life_safety_enabled", True)
    )


def pause_overlay_changed(before, after) -> bool:
    if before.shape != after.shape or before.size == 0:
        return False
    height, width = before.shape[:2]
    roi = (slice(height // 8, height * 7 // 8), slice(width // 8, width * 7 // 8))
    difference = cv2.absdiff(before[roi], after[roi])
    return float(difference.mean()) >= 8.0


def _write_calibration_report(
    path,
    *,
    result,
    stats,
    timing_offset_ms,
    song_id="unknown",
    run_context: LiveRunContext | None = None,
):
    payload = _result_report_payload(
        result,
        stats,
        timing_offset_ms=stats.initial_timing_offset_ms,
        suggested_timing_offset_ms=int(timing_offset_ms),
        run_context=run_context,
        result_status="stable",
    )
    payload.update({
        "timing_offset_ms": int(timing_offset_ms),
        "initial_timing_offset_ms": stats.initial_timing_offset_ms,
        "survived": not stats.life_depleted,
        "completed": bool(stats.completed),
    })
    if run_context is None:
        payload["song_id"] = str(song_id)
    _write_json_atomic(path, payload)


def _persist_profile_timing_offset(
    settings: RuntimeSettings,
    offset_ms: int,
) -> None:
    """把结算建议的时序偏移写回已验收 Profile。

    模拟器侧的输入延迟会随会话漂移 10~20ms，固定偏移会让整局落在判定窗
    的慢/快边缘。正式演奏结算稳定后，把 bounded 建议写回同一 Profile 的
    settings.timing_offset_ms，下一次开演即从修正后的偏移开始；使用
    replace 原子替换以保留 accepted 状态，不产生需要重新验收的草稿。
    任何写回失败只记录日志，绝不影响本局结果与任务状态。
    """
    try:
        store = RealtimeProfileStore(PROJECT_ROOT / "profiles")
        payload = store.load(settings.profile_path.name)
        payload.pop("_path", None)
        current = payload.get("settings")
        if not isinstance(current, dict):
            print(
                "RealtimeProfilePlay timing_offset_persist_skipped="
                "profile settings missing",
                flush=True,
            )
            return
        current["timing_offset_ms"] = int(offset_ms)
        payload["settings"] = current
        payload["modified_at"] = datetime.now().isoformat(timespec="seconds")
        store.replace(settings.profile_path.name, payload)
        print(
            "RealtimeProfilePlay timing_offset_persisted="
            f"{offset_ms} profile={settings.profile_path.name}",
            flush=True,
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(
            "RealtimeProfilePlay timing_offset_persist_failed="
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


def _result_counts(result: LiveResult) -> tuple[int, ...]:
    return (
        result.perfect, result.great, result.good, result.bad,
        result.miss, result.fast, result.slow,
    )


class ResultCollectionStatus(str, Enum):
    STABLE = "stable"
    ADVANCED = "advanced"
    TIMED_OUT = "timed_out"
    STOPPED = "stopped"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ResultCollectionOutcome:
    status: ResultCollectionStatus
    result: LiveResult | None = None
    image: object | None = None
    elapsed_seconds: float = 0.0
    page_state: str = "unknown"
    reason: str | None = None


def _wait_until(deadline, stopping, *, clock, sleeper) -> bool:
    while clock() < deadline:
        if stopping():
            return False
        sleeper(min(.1, max(0.0, deadline - clock())))
    return not stopping()


def _dismiss_reward_popup(
    controller,
    image,
    *,
    before_input=lambda: None,
    templates=(REWARD_CONFIRM_TEMPLATE, REWARD_OK_TEMPLATE),
    threshold: float = REWARD_TEMPLATE_THRESHOLD,
) -> bool:
    """Click a visible central result-popup acknowledgement, if any."""
    best_point = _template_click_point(
        image,
        templates,
        threshold,
        center_region=REWARD_POPUP_BUTTON_REGION,
    )
    if best_point is None:
        return False
    before_input()
    controller.post_click(*best_point).wait()
    return True


def _template_click_point(
    image,
    template_paths,
    threshold: float,
    *,
    center_region: tuple[float, float, float, float] | None = None,
) -> tuple[int, int] | None:
    best_score = threshold
    best_point = None
    for template_path in template_paths:
        template = cv2.imread(str(template_path))
        if template is None:
            continue
        if (
            image.shape[0] < template.shape[0]
            or image.shape[1] < template.shape[1]
        ):
            continue
        matched = cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED)
        search = matched
        offset_x = offset_y = 0
        if center_region is not None:
            image_height, image_width = image.shape[:2]
            template_height, template_width = template.shape[:2]
            min_x, min_y, max_x, max_y = center_region
            left = max(0, round(image_width * min_x - template_width / 2))
            top = max(0, round(image_height * min_y - template_height / 2))
            right = min(
                matched.shape[1],
                round(image_width * max_x - template_width / 2) + 1,
            )
            bottom = min(
                matched.shape[0],
                round(image_height * max_y - template_height / 2) + 1,
            )
            if right <= left or bottom <= top:
                continue
            search = matched[top:bottom, left:right]
            offset_x, offset_y = left, top
        _, score, _, location = cv2.minMaxLoc(search)
        if score > best_score:
            best_score = score
            height, width = template.shape[:2]
            best_point = (
                offset_x + location[0] + width // 2,
                offset_y + location[1] + height // 2,
            )
    return best_point


def _activity_points_confirm_point(
    image,
    template_path,
    threshold: float,
) -> tuple[int, int] | None:
    """Return the real confirm button after the activity page is identified.

    ``result_activity_points.png`` deliberately contains a distinctive page
    label.  Clicking the centre of that label does nothing; only use it as the
    recognition gate, then scale the known lower-right confirm position to the
    captured resolution.
    """
    marker = _template_click_point(image, (template_path,), threshold)
    if marker is None:
        return None
    height, width = image.shape[:2]
    return (
        round(width * ACTIVITY_POINTS_CONFIRM_X_RATIO),
        round(height * ACTIVITY_POINTS_CONFIRM_Y_RATIO),
    )


def _plausible_result(
    result: LiveResult,
    *,
    expected_notes: int | None,
    maximum_notes: int,
) -> tuple[bool, str | None]:
    if result.total <= 0:
        return False, "judgement total is zero"
    if result.total > maximum_notes:
        return False, f"judgement total {result.total} exceeds {maximum_notes}"
    if expected_notes is not None and result.total != expected_notes:
        return False, (
            f"judgement total {result.total} does not match chart "
            f"expected_notes {expected_notes}"
        )
    if result.confidence < 0.30:
        return False, f"result confidence {result.confidence:.3f} is too low"
    if result.fast < 0 or result.slow < 0 or result.fast + result.slow > result.total:
        return False, "FAST/SLOW counts are inconsistent with judgement total"
    return True, None


def _advance_result_rank_page(
    controller,
    image,
    *,
    before_input=lambda: None,
    template_path=RESULT_NEXT_TEMPLATE,
    threshold: float = RESULT_NEXT_TEMPLATE_THRESHOLD,
) -> bool:
    """Advance a recognised rank page through Android Back, never a click."""
    template = cv2.imread(str(template_path))
    if template is None:
        return False
    matched = cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, location = cv2.minMaxLoc(matched)
    if score < threshold:
        return False
    before_input()
    controller.post_click_key(4).wait()
    return True


def collect_result(
    controller,
    stopping,
    *,
    parser: ResultParser | None = None,
    sleeper=time.sleep,
    clock=time.monotonic,
    before_input=lambda: None,
    timeout_seconds: float = 60.0,
    slow_phase_seconds: float = 30.0,
    slow_interval_seconds: float = 1.5,
    medium_interval_seconds: float = 1.0,
    stability_interval_seconds: float = 1.0,
    reward_templates=(REWARD_CONFIRM_TEMPLATE, REWARD_OK_TEMPLATE),
    reward_threshold: float = REWARD_TEMPLATE_THRESHOLD,
    reward_dismiss_limit: int = REWARD_DISMISS_LIMIT,
    reward_click_delay_seconds: float = REWARD_CLICK_DELAY_SECONDS,
    result_next_template=RESULT_NEXT_TEMPLATE,
    result_next_threshold: float = RESULT_NEXT_TEMPLATE_THRESHOLD,
    result_next_click_limit: int = RESULT_NEXT_CLICK_LIMIT,
    result_next_click_delay_seconds: float = RESULT_NEXT_CLICK_DELAY_SECONDS,
    judgement_details_template=JUDGEMENT_DETAILS_TEMPLATE,
    judgement_details_threshold: float = JUDGEMENT_DETAILS_TEMPLATE_THRESHOLD,
    activity_points_template=ACTIVITY_POINTS_TEMPLATE,
    activity_points_threshold: float = ACTIVITY_POINTS_TEMPLATE_THRESHOLD,
    activity_points_click_delay_seconds: float = (
        ACTIVITY_POINTS_CLICK_DELAY_SECONDS
    ),
    activity_points_click_limit: int = ACTIVITY_POINTS_CLICK_LIMIT,
    achievement_close_template=ACHIEVEMENT_LIST_CLOSE_TEMPLATE,
    achievement_close_threshold: float = (
        ACHIEVEMENT_LIST_CLOSE_TEMPLATE_THRESHOLD
    ),
    achievement_close_click_delay_seconds: float = (
        ACHIEVEMENT_LIST_CLOSE_CLICK_DELAY_SECONDS
    ),
    achievement_close_click_limit: int = (
        ACHIEVEMENT_LIST_CLOSE_CLICK_LIMIT
    ),
    unknown_back_grace_seconds: float = 20.0,
    unknown_back_interval_seconds: float = 1.0,
    unknown_back_limit: int = 12,
    expected_notes: int | None = None,
    maximum_notes: int = 3000,
    cooperative_mode: bool = False,
    robust_navigation: bool = False,
) -> ResultCollectionOutcome:
    """Reach PGGBM, read single-live counts, and advance result pages.

    Production runs use the shared accelerated navigator for both single and
    cooperative lives.  It never needs to name intermediate score, reward, or
    loading pages; the safe click/Back loop continues until PGGBM is identified.
    The older page-specific path remains available only for focused parser and
    compatibility tests.
    """
    parser = parser or ResultParser()
    started_at = clock()
    deadline = started_at + timeout_seconds
    candidate: LiveResult | None = None
    candidate_at = 0.0
    last_image = None
    dismissals = 0
    result_next_clicks = 0
    activity_points_clicks = 0
    achievement_close_clicks = 0
    unknown_back_presses = 0
    unknown_since: float | None = None
    page_state = "unknown"
    last_reason: str | None = None
    pending_image = None

    if robust_navigation or cooperative_mode:
        # Single and cooperative lives share the same result transition.  The
        # playfield/life-bar disappearance starts this loop, but does not prove
        # that the network-backed first score page has loaded.  Intermediate
        # pages are intentionally not classified: safe click -> recognise ->
        # Back -> safe click repeats until the fully loaded PGGBM marker wins.
        terminal_threshold = max(0.9, judgement_details_threshold)

        def identify_terminal(image) -> str | None:
            if judgement_details_template is None:
                return None
            marker = _template_click_point(
                image,
                (judgement_details_template,),
                terminal_threshold,
                center_region=JUDGEMENT_DETAILS_MARKER_REGION,
            )
            return "pggbm" if marker is not None else None

        navigation = navigate_result_pages(
            controller,
            stopping,
            identify_terminal,
            before_input=before_input,
            timeout_seconds=max(0.0, deadline - clock()),
            clock=clock,
            sleeper=sleeper,
            log_prefix="RealtimeResult",
        )
        last_image = navigation.image
        if navigation.status is ResultNavigationStatus.STOPPED:
            return ResultCollectionOutcome(
                ResultCollectionStatus.STOPPED,
                image=navigation.image,
                elapsed_seconds=clock() - started_at,
                page_state=navigation.page_state,
                reason=navigation.reason,
            )
        if navigation.status is ResultNavigationStatus.TIMED_OUT:
            return ResultCollectionOutcome(
                ResultCollectionStatus.TIMED_OUT,
                image=navigation.image,
                elapsed_seconds=clock() - started_at,
                page_state="result-navigation",
                reason=navigation.reason,
            )
        pending_image = navigation.image
        if cooperative_mode:
            # The terminal marker has been recognised.  Advance PGGBM once,
            # then the cooperative outer flow checks the repeat-room popup
            # after every subsequent Back.
            back_then_click(
                controller,
                before_input=before_input,
                phase="pggbm",
                log_prefix="RealtimeResult",
            )
            return ResultCollectionOutcome(
                ResultCollectionStatus.ADVANCED,
                image=navigation.image,
                elapsed_seconds=clock() - started_at,
                page_state="pggbm",
                reason=(
                    "协力结算已循环推进至PGGBM并完成返回操作；"
                    f"中间返回{navigation.back_attempts}次"
                ),
            )

    def post_result_back() -> None:
        before_input()
        controller.post_click_key(4).wait()

    while clock() < deadline:
        if stopping():
            return ResultCollectionOutcome(
                ResultCollectionStatus.STOPPED,
                elapsed_seconds=clock() - started_at,
                page_state=page_state,
                reason="user stopped result collection",
            )
        if pending_image is not None:
            image = pending_image
            pending_image = None
        else:
            image = controller.post_screencap().wait().get()
        last_image = image
        now = clock()
        details_marker_visible = (
            judgement_details_template is not None
            and _template_click_point(
                image,
                (judgement_details_template,),
                judgement_details_threshold,
                center_region=JUDGEMENT_DETAILS_MARKER_REGION,
            ) is not None
        )

        achievement_close_point = (
            _template_click_point(
                image,
                (achievement_close_template,),
                achievement_close_threshold,
                center_region=ACHIEVEMENT_LIST_CLOSE_REGION,
            )
            if (
                achievement_close_template is not None
                and not details_marker_visible
            ) else None
        )
        if achievement_close_point is not None:
            if achievement_close_clicks >= achievement_close_click_limit:
                return ResultCollectionOutcome(
                    ResultCollectionStatus.BLOCKED,
                    image=image,
                    elapsed_seconds=now - started_at,
                    page_state="achievement-list",
                    reason=(
                        "已识别达成报酬一览，但"
                        f"{achievement_close_clicks}次返回后页面仍未消失"
                    ),
                )
            post_result_back()
            achievement_close_clicks += 1
            unknown_since = None
            page_state = "achievement-list"
            candidate = None
            print(
                "RealtimeResult state=achievement-list action=back"
                + f" attempt={achievement_close_clicks}",
                flush=True,
            )
            if not _wait_until(
                min(deadline, now + achievement_close_click_delay_seconds),
                stopping,
                clock=clock,
                sleeper=sleeper,
            ):
                return ResultCollectionOutcome(
                    ResultCollectionStatus.STOPPED,
                    elapsed_seconds=clock() - started_at,
                    page_state=page_state,
                    reason="user stopped result collection",
                )
            continue

        reward_point = (
            _template_click_point(
                image,
                reward_templates,
                reward_threshold,
                center_region=REWARD_POPUP_BUTTON_REGION,
            )
            if not details_marker_visible else None
        )
        if reward_point is not None:
            if dismissals >= reward_dismiss_limit:
                return ResultCollectionOutcome(
                    ResultCollectionStatus.BLOCKED,
                    image=image,
                    elapsed_seconds=now - started_at,
                    page_state="reward-popup",
                    reason=(
                        "recognised reward popup did not disappear after "
                        f"{dismissals} Back attempts"
                    ),
                )
            # Daily-first-live and seven-day streak rewards can appear as two
            # consecutive popups with the same dedicated confirm/OK marker.
            # Dismiss each recognised popup, with a strict sequence limit that
            # also bounds retries if the emulator drops an input.
            post_result_back()
            dismissals += 1
            unknown_since = None
            print(
                "RealtimeResult state=reward-popup action=back"
                + f" attempt={dismissals}",
                flush=True,
            )
            page_state = "reward-popup"
            candidate = None
            if not _wait_until(
                min(deadline, now + reward_click_delay_seconds),
                stopping,
                clock=clock,
                sleeper=sleeper,
            ):
                return ResultCollectionOutcome(
                    ResultCollectionStatus.STOPPED,
                    elapsed_seconds=clock() - started_at,
                    page_state=page_state,
                    reason="user stopped result collection",
                )
            continue

        activity_points_point = (
            _activity_points_confirm_point(
                image, activity_points_template, activity_points_threshold,
            )
            if (
                activity_points_template is not None
                and not details_marker_visible
            )
            else None
        )
        if activity_points_point is not None:
            # A normal, boost-consuming live can insert the event points page
            # before the score/judgement page.  Calibration rehearsals often
            # skip it, which previously made the formal round look as if its
            # judgement details had already been lost.  Advance only after the
            # dedicated page marker matches.  Some emulator frames accept the
            # first Back job but do not deliver it to the game; retry the same
            # safe shortcut once, then fail closed if the marker persists.
            if activity_points_clicks >= activity_points_click_limit:
                return ResultCollectionOutcome(
                    ResultCollectionStatus.BLOCKED,
                    image=image,
                    elapsed_seconds=now - started_at,
                    page_state="activity-points",
                    reason=(
                        "已识别活动点数页，但"
                        f"{activity_points_clicks}次返回推进后页面仍未消失"
                    ),
                )
            post_result_back()
            activity_points_clicks += 1
            unknown_since = None
            page_state = "activity-points"
            candidate = None
            print(
                "RealtimeResult state=activity-points action=back"
                + f" attempt={activity_points_clicks}",
                flush=True,
            )
            if not _wait_until(
                min(deadline, now + activity_points_click_delay_seconds),
                stopping,
                clock=clock,
                sleeper=sleeper,
            ):
                return ResultCollectionOutcome(
                    ResultCollectionStatus.STOPPED,
                    elapsed_seconds=clock() - started_at,
                    page_state=page_state,
                    reason="user stopped result collection",
                )
            continue

        rank_point = (
            _template_click_point(
                image, (result_next_template,), result_next_threshold,
            )
            if not details_marker_visible and not cooperative_mode else None
        )
        if rank_point is not None:
            if result_next_clicks >= result_next_click_limit:
                return ResultCollectionOutcome(
                    ResultCollectionStatus.BLOCKED,
                    image=image,
                    elapsed_seconds=now - started_at,
                    page_state="rank-page",
                    reason=(
                        "已识别排名结算页，但"
                        f"{result_next_clicks}次返回推进后页面仍未消失"
                    ),
                )
            post_result_back()
            result_next_clicks += 1
            unknown_since = None
            page_state = "rank-page"
            candidate = None
            print(
                "RealtimeResult state=rank-page action=back"
                + f" attempt={result_next_clicks}",
                flush=True,
            )
            if not _wait_until(
                min(deadline, now + result_next_click_delay_seconds),
                stopping,
                clock=clock,
                sleeper=sleeper,
            ):
                return ResultCollectionOutcome(
                    ResultCollectionStatus.STOPPED,
                    elapsed_seconds=clock() - started_at,
                    page_state=page_state,
                    reason="user stopped result collection",
                )
            continue

        details_visible = (
            judgement_details_template is None
            or details_marker_visible
        )
        if not details_visible:
            # Fixed digit ROIs overlap unrelated score/rank-page elements.
            # Never invoke the parser until the dedicated judgement-page
            # identity marker is visible.  The game can spend roughly five
            # seconds animating the PGGBM counts after the score page appears,
            # and the preceding result transition starts even earlier.  Treat
            # that marker-less interval as loading before attempting recovery.
            candidate = None
            if unknown_since is None:
                unknown_since = now
                print(
                    "RealtimeResult state=result-loading action=wait"
                    + f" grace_seconds={unknown_back_grace_seconds:.1f}",
                    flush=True,
                )
            if now - unknown_since >= unknown_back_grace_seconds:
                page_state = "unknown"
                if unknown_back_presses >= unknown_back_limit:
                    return ResultCollectionOutcome(
                        ResultCollectionStatus.BLOCKED,
                        image=image,
                        elapsed_seconds=now - started_at,
                        page_state=page_state,
                        reason=(
                            "结算后未知页面在"
                            f"{unknown_back_presses}次返回后仍未消失"
                        ),
                    )
                post_result_back()
                unknown_back_presses += 1
                print(
                    "RealtimeResult state=unknown action=back"
                    + f" attempt={unknown_back_presses}",
                    flush=True,
                )
                interval = unknown_back_interval_seconds
            else:
                page_state = "result-loading"
                interval = min(
                    slow_interval_seconds,
                    medium_interval_seconds,
                )
            if not _wait_until(
                min(deadline, now + interval),
                stopping,
                clock=clock,
                sleeper=sleeper,
            ):
                return ResultCollectionOutcome(
                    ResultCollectionStatus.STOPPED,
                    elapsed_seconds=clock() - started_at,
                    page_state=page_state,
                    reason="user stopped result collection",
                )
            continue

        unknown_since = None

        try:
            result = parser.parse(image)
        except ValueError:
            result = None

        if result is not None:
            if expected_notes is not None and result.total != expected_notes:
                resolver = getattr(parser, "resolve_expected_total", None)
                if callable(resolver):
                    try:
                        result = resolver(
                            image,
                            expected_notes=expected_notes,
                            fallback=result,
                        )
                    except ValueError:
                        # Preserve the original parse so the normal plausibility
                        # path reports a precise expected-total mismatch.
                        pass
            plausible, validation_reason = _plausible_result(
                result,
                expected_notes=expected_notes,
                maximum_notes=maximum_notes,
            )
            if not plausible:
                candidate = None
                page_state = "judgement-details-invalid"
                last_reason = validation_reason
                if not _wait_until(
                    min(deadline, now + stability_interval_seconds),
                    stopping,
                    clock=clock,
                    sleeper=sleeper,
                ):
                    return ResultCollectionOutcome(
                        ResultCollectionStatus.STOPPED,
                        elapsed_seconds=clock() - started_at,
                        page_state=page_state,
                        reason="user stopped result collection",
                    )
                continue
            page_state = "judgement-details"
            if (
                candidate is not None
                and now - candidate_at >= stability_interval_seconds
                and _result_counts(result) == _result_counts(candidate)
            ):
                if robust_navigation:
                    accelerated_back(
                        controller,
                        before_input=before_input,
                        phase="pggbm-stable",
                        log_prefix="RealtimeResult",
                    )
                return ResultCollectionOutcome(
                    ResultCollectionStatus.STABLE,
                    result=result,
                    image=image,
                    elapsed_seconds=now - started_at,
                    page_state=page_state,
                )
            if candidate is None or _result_counts(result) != _result_counts(candidate):
                candidate = result
                candidate_at = now
            if not _wait_until(
                min(deadline, now + stability_interval_seconds),
                stopping,
                clock=clock,
                sleeper=sleeper,
            ):
                return ResultCollectionOutcome(
                    ResultCollectionStatus.STOPPED,
                    elapsed_seconds=clock() - started_at,
                    page_state=page_state,
                    reason="user stopped result collection",
                )
            continue
        candidate = None
        page_state = "unknown"
        interval = min(slow_interval_seconds, medium_interval_seconds)
        if not _wait_until(
            min(deadline, now + interval),
            stopping,
            clock=clock,
            sleeper=sleeper,
        ):
            return ResultCollectionOutcome(
                ResultCollectionStatus.STOPPED,
                elapsed_seconds=clock() - started_at,
                page_state=page_state,
                reason="user stopped result collection",
            )

    return ResultCollectionOutcome(
        ResultCollectionStatus.TIMED_OUT,
        image=last_image,
        elapsed_seconds=clock() - started_at,
        page_state=page_state,
        reason=last_reason or "result page was not recognised before timeout",
    )


def _visual_signature_values(
    store: RealtimeProfileStore,
    *,
    require_verified: bool,
) -> tuple[int, int, bool]:
    verified = verified_game_visual_settings()
    if verified is not None:
        return (
            verified.note_skin_type,
            verified.tap_effect,
            verified.judgement_assist_effect,
        )
    if require_verified:
        raise RuntimeError("本次开演前尚未实际验证游戏视觉设置")
    options = store.runtime_options()
    return (
        int(options.get("note_skin_type", 1)),
        int(options.get("tap_effect", 1)),
        bool(options.get("judgement_assist_effect", True)),
    )


def resolve_profile_for_settings_gate(
    context: Context,
    params: dict,
    *,
    controller=None,
    require_verified_visual: bool = False,
):
    controller = controller or context.tasker.controller
    store = RealtimeProfileStore(PROJECT_ROOT / "profiles")
    note_skin_type, tap_effect, judgement_assist_effect = (
        _visual_signature_values(store, require_verified=require_verified_visual)
    )
    image = controller.post_screencap().wait().get()
    signature = EnvironmentSignature(
        frame_resolution(image),
        int(params.get("dpi", 240)),
        int(params.get("game_fps", 60)),
        str(params.get("render_quality", "standard")),
        1.0,
        note_skin_type,
        tap_effect,
        judgement_assist_effect,
    )
    resolver = (
        store.resolve_latest_for_visual_evaluation_environment
        if params.get("visual_evaluation")
        else store.resolve_latest_for_environment
    )
    return resolver(
        difficulty=str(params.get("difficulty", "Easy")),
        current_signature=signature,
    )


def resolve_profile(context: Context, params: dict, *, controller=None):
    controller = controller or context.tasker.controller
    difficulty = str(params.get("difficulty", "Easy"))
    verified = verified_settings(difficulty)
    if bool(params.get("settings_gate_required", False)) and verified is None:
        raise RuntimeError("本次开演前尚未实际验证游戏流速")
    note_speed = (
        verified.actual_note_speed
        if verified is not None
        else float(params.get("note_speed", 2.0))
    )
    store = RealtimeProfileStore(PROJECT_ROOT / "profiles")
    note_skin_type, tap_effect, judgement_assist_effect = (
        _visual_signature_values(
            store,
            require_verified=bool(params.get("settings_gate_required", False)),
        )
    )
    image = controller.post_screencap().wait().get()
    signature = EnvironmentSignature(
        frame_resolution(image),
        int(params.get("dpi", 240)),
        int(params.get("game_fps", 60)),
        str(params.get("render_quality", "standard")),
        note_speed,
        note_skin_type,
        tap_effect,
        judgement_assist_effect,
    )
    visual_evaluation = bool(params.get("visual_evaluation", False))
    if verified is not None and verified.profile:
        resolver = (
            store.resolve_for_visual_evaluation
            if visual_evaluation else store.resolve
        )
        return resolver(
            verified.profile,
            difficulty=difficulty,
            current_signature=signature,
        )
    latest_resolver = (
        store.resolve_latest_for_visual_evaluation
        if visual_evaluation else store.resolve_latest
    )
    return latest_resolver(
        difficulty=difficulty,
        current_signature=signature,
    )


@AgentServer.custom_action("RealtimeProfileCheck")
class RealtimeProfileCheck(CustomAction):
    """Refuse to start a live before its accepted Profile is available."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        params: dict = {}
        try:
            if context.tasker.stopping:
                return True
            params = json.loads(argv.custom_action_param or "{}")
            settings = resolve_profile_for_settings_gate(
                context, params, require_verified_visual=True,
            )
            print(
                "RealtimeProfileCheck "
                f"profile={settings.profile_path.name} "
                f"expected_speed={settings.note_speed:.2f}",
                flush=True,
            )
            return True
        except Exception as exc:
            if context.tasker.stopping:
                print("RealtimeProfileCheck stopped=true", flush=True)
                return True
            reason = f"{type(exc).__name__}: {exc}"
            record_failure_reason(reason)
            try:
                write_preflight_terminal_result(
                    output_dir=PROJECT_ROOT / "screencap",
                    params=params,
                    terminal_stage="profile_check",
                    reason=reason,
                    visual_settings=verified_game_visual_settings(),
                )
            except Exception as artifact_error:
                print(
                    "RealtimeProfileCheck artifact_failed="
                    f"{type(artifact_error).__name__}: {artifact_error}",
                    flush=True,
                )
                traceback.print_exc()
            traceback.print_exc()
            print(f"RealtimeProfileCheck failed={type(exc).__name__}: {exc}", flush=True)
            return False


@AgentServer.custom_action("RealtimeProfilePlay")
class RealtimeProfilePlay(CustomAction):
    """Run a bounded rehearsal using only a matching accepted local profile."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            return self._run(context, argv)
        except Exception as exc:
            record_failure_reason(f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
            print(f"RealtimeProfilePlay failed={type(exc).__name__}: {exc}", flush=True)
            return False

    def _run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        global _LAST_LIFE_SAFETY_ABORT
        _LAST_LIFE_SAFETY_ABORT = False
        params = json.loads(argv.custom_action_param or "{}")
        if context.tasker.stopping:
            return True
        verified = None
        settings = None
        visual = None
        recorder = None
        try:
            controller = context.tasker.controller
            require_profile = bool(params.get("require_profile", True))
            difficulty = str(params.get("difficulty", "Easy"))
            ignore_note_speed = bool(params.get("ignore_note_speed", False))
            verified = (
                None if ignore_note_speed else verified_settings(difficulty)
            )
            if (
                bool(params.get("settings_gate_required", False))
                and verified is None
            ):
                raise RuntimeError("本次开演前尚未实际验证游戏流速")
            visual = verified_game_visual_settings()
            if (
                bool(params.get("settings_gate_required", False))
                and visual is None
            ):
                raise RuntimeError("本次开演前尚未实际验证游戏视觉设置")
            settings = (
                (
                    resolve_profile_for_settings_gate(
                        context, params, controller=controller,
                    )
                    if ignore_note_speed
                    else resolve_profile(context, params, controller=controller)
                )
                if require_profile else None
            )
            if context.tasker.stopping:
                return True
            target_fps = (
                settings.target_fps
                if settings else int(params.get("target_fps", 60))
            )
            timing_offset_ms = (
                settings.timing_offset_ms
                if settings else int(params.get("timing_offset_ms", 0))
            )
            runtime_options = RealtimeProfileStore(
                PROJECT_ROOT / "profiles"
            ).runtime_options()
            chart_prediction_enabled = (
                bool(runtime_options.get("chart_prediction_enabled", False))
            )
            chart_predict_presses = bool(
                runtime_options.get("chart_predict_presses", False)
            )
            native_requested = bool(
                runtime_options.get("native_realtime_enabled", False)
            )
            final_cover_required = bool(
                params.get(
                    "confirm_final_cover",
                    params.get("settings_gate_required", False),
                )
            ) and not ignore_note_speed
            native_prearm_deferred = bool(
                native_requested
                and final_cover_required
                and params.get("native_prearm_deferred", False)
            )
            chart_timeline = None
            selected_chart = None
            if (
                chart_prediction_enabled
                or native_requested
                or final_cover_required
            ):
                live_run = current_live_run()
                try:
                    resolution = resolve_local_chart_for_run(
                        live_run,
                        difficulty,
                    )
                except Exception as exc:
                    if native_requested:
                        discard_prearmed_backend(
                            "profile-chart-resolution-failed"
                        )
                    if native_requested or final_cover_required:
                        raise RuntimeError(
                            "开演前本地谱面解析失败："
                            f"{type(exc).__name__}: {exc}"
                        ) from exc
                    if not isinstance(
                        exc,
                        (OSError, ValueError, KeyError, TypeError),
                    ):
                        raise
                    chart_prediction_enabled = False
                    print(
                        "RealtimeProfilePlay chart_prediction=off "
                        f"reason=local chart repository invalid: {exc}",
                        flush=True,
                    )
                else:
                    chart_reason = resolution.reason
                    if resolution.selection is not None:
                        selected_chart = resolution.selection
                        chart_timeline = selected_chart.timeline
                        if chart_prediction_enabled:
                            print(
                                "RealtimeProfilePlay chart_prediction=on "
                                "bestdori_song_id="
                                f"{selected_chart.bestdori_song_id} "
                                f"difficulty={selected_chart.difficulty} "
                                f"song={live_run.song_id}",
                                flush=True,
                            )
                        elif native_requested:
                            print(
                                "RealtimeProfilePlay native_chart=resolved "
                                f"chart={selected_chart.path}",
                                flush=True,
                            )
                    elif native_requested and not native_prearm_deferred:
                        discard_prearmed_backend(
                            "profile-chart-resolution-missing"
                        )
                        raise RuntimeError(
                            "Native 已显式启用，但当前歌曲没有经过确认的"
                            f"本地谱面：{chart_reason}"
                        )
                    elif final_cover_required:
                        print(
                            "RealtimeProfilePlay chart_resolution=deferred "
                            f"reason={chart_reason}",
                            flush=True,
                        )
                    else:
                        chart_prediction_enabled = False
                        print(
                            "RealtimeProfilePlay chart_prediction=off "
                            f"reason={chart_reason}",
                            flush=True,
                        )
            (
                is_rehearsal,
                continue_after_depleted,
                life_threshold,
            ) = resolve_life_policy(params, runtime_options)
            numeric_life_monitor_enabled = resolve_life_monitor_enabled(
                params,
                runtime_options,
            )
            debug_recording = bool(
                params.get("debug_recording") or debug_enabled()
            )
            diagnostic_trace = bool(
                debug_recording
                or params.get(
                    "diagnostic_trace",
                    diagnostic_trace_enabled(),
                )
            )
            run_mode = _run_mode(params, is_rehearsal=is_rehearsal)
            expected_note_speed = (
                verified.expected_note_speed
                if verified is not None
                else float(
                    getattr(
                        settings,
                        "note_speed",
                        params.get("note_speed", 2.0),
                    )
                )
            )
            actual_note_speed = (
                verified.actual_note_speed if verified is not None else None
            )
            live_run = current_live_run()
            if (
                live_run is None
                or run_mode == "continuous"
                or not live_run.prepared_for_play
            ):
                live_run = reset_live_run(
                    mode=run_mode,
                    difficulty=difficulty,
                )
            else:
                live_run = update_live_run(prepared_for_play=False)
            live_run = update_live_run(
                mode=run_mode,
                difficulty=difficulty,
                profile_name=(settings.profile_path.name if settings else None),
                expected_note_speed=expected_note_speed,
                actual_note_speed=actual_note_speed,
                note_skin_type=(
                    visual.note_skin_type if visual is not None else None
                ),
                tap_effect=(visual.tap_effect if visual is not None else None),
                judgement_assist=(
                    visual.judgement_assist_effect
                    if visual is not None else None
                ),
                debug_recording=debug_recording,
                recording_path=None,
            )
            if debug_recording:
                recorder = RealtimeDebugRecorder(
                    PROJECT_ROOT / "debug" / "recordings"
                )
            elif diagnostic_trace:
                recorder = RealtimeDebugRecorder(
                    PROJECT_ROOT / "debug" / "recordings",
                    video_enabled=False,
                )
            if recorder is not None:
                live_run = update_live_run(
                    recording_path=_relative_artifact_path(recorder.output_dir),
                )
                recorder.set_session_metadata(live_run.to_mapping())
                append_lifecycle_event(
                    recorder.output_dir,
                    "preflight",
                    "ready",
                    details={
                        "song_id": live_run.song_id,
                        "song_id_method": live_run.song_id_method,
                        "song_level": live_run.song_level,
                        "song_title": live_run.song_title,
                        "profile_name": live_run.profile_name,
                        "expected_note_speed": live_run.expected_note_speed,
                        "actual_note_speed": live_run.actual_note_speed,
                    },
                )
                try:
                    preflight_image = controller.post_screencap().wait().get()
                    _recorder_checkpoint(
                        recorder,
                        preflight_image,
                        "preflight",
                        "ready",
                        details={
                            "song_id": live_run.song_id,
                            "difficulty": live_run.difficulty,
                        },
                    )
                except Exception as checkpoint_error:
                    append_lifecycle_event(
                        recorder.output_dir,
                        "preflight",
                        "checkpoint-error",
                        details={
                            "reason": (
                                f"{type(checkpoint_error).__name__}: "
                                f"{checkpoint_error}"
                            ),
                        },
                    )
                print(
                    "RealtimeProfilePlay diagnostics="
                    f"{recorder.output_dir} "
                    f"mode={'video' if debug_recording else 'trace-only'}",
                    flush=True,
                )
            if final_cover_required:
                # 协力准备页的歌曲身份可能受随机选曲和网络阶段影响，最终封面
                # 必须独立解析谱面，不能被早先的候选结果锁死。
                cover_selection = (
                    None if live_run.mode == "cooperative" else selected_chart
                )
                cover_checkpoint_saved = False

                def observe_final_cover(image, timestamp, diagnostic) -> None:
                    nonlocal cover_checkpoint_saved
                    assert recorder is not None
                    record_phase = getattr(recorder, "record_phase", None)
                    if callable(record_phase):
                        record_phase(
                            image,
                            timestamp,
                            "final-cover",
                            diagnostics=[diagnostic],
                        )
                    if not cover_checkpoint_saved:
                        _recorder_checkpoint(
                            recorder,
                            image,
                            "final-cover",
                            "started",
                            details=diagnostic,
                        )
                        cover_checkpoint_saved = True

                cover_outcome = wait_for_final_cover(
                    controller,
                    live_run,
                    cover_selection,
                    difficulty,
                    lambda: context.tasker.stopping,
                    repository=(
                        LocalChartRepository(
                            PROJECT_ROOT / "resource" / "charts"
                        )
                        if cover_selection is None else None
                    ),
                    timeout_seconds=float(
                        params.get("final_cover_timeout_seconds", 60.0)
                    ),
                    observer=(
                        observe_final_cover if recorder is not None else None
                    ),
                    fallback_selection_available=selected_chart is not None,
                )
                if recorder is not None and cover_outcome.image is not None:
                    _recorder_checkpoint(
                        recorder,
                        cover_outcome.image,
                        "final-cover",
                        cover_outcome.status,
                        details={
                            "reason": cover_outcome.reason,
                            "frames": cover_outcome.frames,
                            "playfield_seen": cover_outcome.playfield_seen,
                        },
                    )
                if cover_outcome.resolution is not None:
                    confirmation = cover_outcome.resolution.confirmation
                    selected_chart = cover_outcome.resolution.selection
                    chart_timeline = selected_chart.timeline
                    live_run = update_live_run(
                        song_id=confirmation.song_id,
                        song_id_method=confirmation.song_id_method,
                        final_cover_confirmed=True,
                        final_cover_song_id=confirmation.song_id,
                        final_cover_status="confirmed",
                        final_cover_reason=None,
                        prepared_for_play=True,
                    )
                else:
                    live_run = update_live_run(
                        final_cover_confirmed=False,
                        final_cover_song_id=None,
                        final_cover_status=cover_outcome.status,
                        final_cover_reason=cover_outcome.reason,
                        prepared_for_play=selected_chart is not None,
                    )
                    if selected_chart is None:
                        chart_timeline = None
                        chart_prediction_enabled = False
                        chart_predict_presses = False
                        if native_requested:
                            discard_prearmed_backend(
                                "final-cover-visual-legacy-fallback"
                            )
                        native_requested = False
                    print(
                        "RealtimeProfilePlay cover_confirmation=degraded "
                        f"fallback={cover_outcome.status} "
                        f"reason={cover_outcome.reason}",
                        flush=True,
                    )
                if (
                    native_requested
                    and native_prearm_deferred
                    and selected_chart is not None
                ):
                    prepared_selection = prepare_native_for_settings_gate(
                        controller=controller,
                        live_run=live_run,
                        difficulty=difficulty,
                        project_root=PROJECT_ROOT,
                        runtime_options=runtime_options,
                        ready_timeout_s=float(
                            params.get("native_ready_timeout_seconds", 4.0)
                        ),
                        ttl_s=float(
                            params.get("native_prearm_ttl_seconds", 30.0)
                        ),
                    )
                    try:
                        selected_chart = _effective_native_chart_selection(
                            selected_chart,
                            prepared_selection,
                        )
                    except RuntimeError:
                        discard_prearmed_backend(
                            "final-cover-prearm-chart-mismatch"
                        )
                        raise
                    chart_timeline = selected_chart.timeline
                live_run = update_live_run(prepared_for_play=False)
                if recorder is not None:
                    _recorder_update_metadata(recorder, live_run)
                    append_lifecycle_event(
                        recorder.output_dir,
                        "final-cover",
                        cover_outcome.status,
                        details={
                            "reason": cover_outcome.reason,
                            "frames": cover_outcome.frames,
                            "playfield_seen": cover_outcome.playfield_seen,
                        },
                    )
        except Exception as exc:
            if context.tasker.stopping:
                return True
            reason = f"{type(exc).__name__}: {exc}"
            performance_snapshot = None
            if verified is not None:
                performance_snapshot = PreflightPerformanceSnapshot(
                    expected_note_speed=float(verified.expected_note_speed),
                    actual_note_speed=float(verified.actual_note_speed),
                    profile=(
                        verified.profile
                        or (
                            settings.profile_path.name
                            if settings is not None else None
                        )
                    ),
                )
            elif settings is not None:
                performance_snapshot = PreflightPerformanceSnapshot(
                    expected_note_speed=float(
                        getattr(
                            settings,
                            "note_speed",
                            params.get("note_speed", 2.0),
                        )
                    ),
                    profile=settings.profile_path.name,
                )
            try:
                if recorder is not None:
                    try:
                        _recorder_update_metadata(recorder, live_run)
                    except Exception:
                        pass
                    try:
                        failure_image = controller.post_screencap().wait().get()
                        _recorder_checkpoint(
                            recorder,
                            failure_image,
                            "preflight",
                            "error",
                            details={"reason": reason},
                        )
                    except Exception:
                        pass
                    recorder.close()
                write_preflight_terminal_result(
                    output_dir=PROJECT_ROOT / "screencap",
                    params=params,
                    terminal_stage="profile_play_preflight",
                    reason=reason,
                    visual_settings=visual,
                    performance_snapshot=performance_snapshot,
                )
            except Exception as artifact_error:
                print(
                    "RealtimeProfilePlay preflight_artifact_failed="
                    f"{type(artifact_error).__name__}: {artifact_error}",
                    flush=True,
                )
                traceback.print_exc()
            raise

        def write_failure_artifacts(
            stats: EngineStats,
            *,
            result_status: str,
            reason: str,
        ) -> None:
            calibration_report = params.get("calibration_report")
            if not params.get("save_result_frame") and not calibration_report:
                return
            payload = _result_report_payload(
                None,
                stats,
                timing_offset_ms=timing_offset_ms,
                suggested_timing_offset_ms=None,
                run_context=live_run,
                result_status=result_status,
                reason=reason,
            )
            if params.get("save_result_frame"):
                output = PROJECT_ROOT / "screencap"
                output.mkdir(parents=True, exist_ok=True)
                stamp = (
                    datetime.now().strftime("%Y%m%d-%H%M%S")
                    + f"-{live_run.run_id[:8]}"
                )
                _write_json_atomic(
                    output / f"realtime-result-{stamp}.json", payload,
                )
            if calibration_report:
                report_path = PROJECT_ROOT / str(calibration_report)
                report_path.parent.mkdir(parents=True, exist_ok=True)
                _write_json_atomic(report_path, {
                    **payload,
                    "timing_offset_ms": stats.final_timing_offset_ms,
                    "survived": not stats.life_depleted,
                    "completed": bool(stats.completed),
                })

        mode = f"profile={settings.profile_path.name}" if settings else "mode=rehearsal-defaults"
        speed_message = (
            f"actual_speed={verified.actual_note_speed:.2f} "
            f"expected_speed={verified.expected_note_speed:.2f}"
            if verified is not None
            else f"declared_speed={float(params.get('note_speed', 2.0)):.2f}"
        )
        print(f"RealtimeProfilePlay {mode} {speed_message}", flush=True)
        if ignore_note_speed and settings is not None:
            print(
                "RealtimeProfilePlay listener_mode=true "
                f"profile_speed={settings.note_speed:.2f} "
                "actual game note speed must match the accepted Profile",
                flush=True,
            )
        touch = None
        native_backend = None
        try:
            if native_requested:
                if selected_chart is None:
                    discard_prearmed_backend("profile-native-chart-missing")
                    raise RuntimeError(
                        "Native 已显式启用，但当前歌曲没有经过确认的本地谱面"
                    )
                try:
                    native_backend = consume_prearmed_backend(
                        live_run.run_id,
                        selected_chart.path,
                    )
                    native_backend.configure_timing_offset(timing_offset_ms)
                    print(
                        "RealtimeProfilePlay native_prearmed=consumed "
                        f"chart={selected_chart.path} "
                        f"timing_offset_ms={timing_offset_ms}",
                        flush=True,
                    )
                except Exception as exc:
                    raise RuntimeError(
                        "Native 预武装消费失败；已禁止回退 Legacy，"
                        "且禁止开演后重新构造："
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
            require_game_foreground(controller)
            # Foreground verification is intentionally outside the realtime
            # touch hot path. A dumpsys query before every down/move/up blocks
            # capture for 100-450 ms and causes otherwise correct SLOW notes.
            touch = ControllerTouchDispatcher(
                controller,
                lambda: context.tasker.stopping,
            )
            playfield_monitor = None
            if final_cover_required and not numeric_life_monitor_enabled:
                completion_missing_checks = None
                if params.get("wait_for_completion"):
                    completion_seconds = (
                        int(params.get("completion_missing_frames", 120))
                        / max(1, target_fps)
                    )
                    completion_missing_checks = max(
                        3,
                        math.ceil(completion_seconds / 0.2),
                    )
                playfield_monitor = PlayfieldLifecycleMonitor(
                    confirm_checks=2,
                    missing_checks=completion_missing_checks,
                    active_check_interval_seconds=0.2,
                )
            engine = RealtimeEngine(
                NoteDetector(),
                RealtimePlanner(
                    judgement_y=565,
                    timing_offset_ms=timing_offset_ms,
                    rescue_first_visible=True,
                    enable_slide=sliding_holds_enabled(
                        str(params.get("difficulty", "Easy"))
                    ),
                    chart_timeline=chart_timeline,
                    chart_prediction=chart_prediction_enabled,
                    chart_predict_presses=(
                        chart_predict_presses
                        and chart_prediction_enabled
                    ),
                ),
                touch,
                life_detector=(
                    LifeDetector() if numeric_life_monitor_enabled else None
                ),
                life_guard=(
                    LifeGuard() if numeric_life_monitor_enabled else None
                ),
                completion_guard=(
                    PlayfieldCompletionGuard(
                        _completion_missing_frames(
                            int(params.get("completion_missing_frames", 120)),
                            native=native_requested,
                            target_fps=target_fps,
                        )
                    )
                    if (
                        numeric_life_monitor_enabled
                        and params.get("wait_for_completion")
                    )
                    else None
                ),
                debug_recorder=recorder,
                timing_feedback_detector=TimingFeedbackDetector(),
                timing_controller=AdaptiveTimingController(
                    timing_offset_ms,
                    # Hard+ sessions drift their game-side input latency by
                    # 10-20 ms run to run; adapt faster and wider so the
                    # finale does not play at the wrong end of the window.
                    # Normal keeps the gentler defaults.
                    **(
                        {
                            # 判定条 2/3 窗口检测后信号可信；单局常只有 3-4 个
                            # 非 PERFECT 判定，首个判定条即小幅修正，同向持续
                            # 再加大步长，尽早跟上逐局 10-30ms 的输入延迟漂移。
                            "step_ms": 2,
                            "unanimous_step_ms": 4,
                            "minimum_samples": 1,
                            "imbalance": 1,
                            "window_size": 4,
                            "adjustment_cooldown_seconds": 0.5,
                            "maximum_live_adjustment_ms": 35,
                        }
                        if sliding_holds_enabled(
                            str(params.get("difficulty", "Easy"))
                        )
                        else {}
                    ),
                ),
                native_backend=native_backend,
                playfield_monitor=playfield_monitor,
                live_failed_detector=LiveFailedPopupDetector(),
            )
        except Exception as setup_error:
            cleanup_errors = []
            recorder_error = None
            if recorder is not None:
                try:
                    recorder.close()
                except Exception as cleanup_error:
                    recorder_error = (
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            if touch is not None:
                try:
                    touch.close()
                except Exception as cleanup_error:
                    cleanup_errors.append(
                        f"touch_close={type(cleanup_error).__name__}: {cleanup_error}"
                    )
            if native_backend is not None:
                try:
                    native_backend.stop()
                except Exception as cleanup_error:
                    cleanup_errors.append(
                        "native_backend_stop="
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            reason = f"preflight error: {type(setup_error).__name__}: {setup_error}"
            preflight_stats = EngineStats(
                0,
                0,
                False,
                initial_timing_offset_ms=timing_offset_ms,
                final_timing_offset_ms=timing_offset_ms,
                terminal_reason=reason,
                cleanup_failed=bool(cleanup_errors),
                cleanup_errors=tuple(cleanup_errors),
                recorder_error=recorder_error,
                engine_mode="native" if native_requested else "legacy",
            )
            try:
                write_failure_artifacts(
                    preflight_stats,
                    result_status="preflight_error",
                    reason=reason,
                )
            except Exception as artifact_error:
                setup_error.add_note(
                    "preflight artifact write failed: "
                    f"{type(artifact_error).__name__}: {artifact_error}"
                )
            raise
        save_screenshot = debug_recording
        print(
            "RealtimeProfilePlay life_policy "
            f"rehearsal={is_rehearsal} "
            f"continue_after_depleted={continue_after_depleted} "
            f"threshold={life_threshold} "
            f"numeric_monitor={numeric_life_monitor_enabled} "
            f"playfield_monitor={playfield_monitor is not None}",
            flush=True,
        )

        def pause_for_life(reading) -> None:
            global _LAST_LIFE_SAFETY_ABORT
            _LAST_LIFE_SAFETY_ABORT = True
            confirmed = False
            for attempt in range(2):
                try:
                    require_game_foreground(controller)
                    before = controller.post_screencap().wait().get()
                    controller.post_click(1237, 58).wait()
                    time.sleep(.4)
                    after = controller.post_screencap().wait().get()
                    confirmed = pause_overlay_changed(before, after)
                except Exception:
                    confirmed = False
                if confirmed:
                    break
            print(
                f"RealtimeProfilePlay life_safety value={reading.value} "
                f"threshold={life_threshold} pause_confirmed={confirmed}",
                flush=True,
            )
            if not confirmed:
                print(
                    "RealtimeProfilePlay life_safety warning: "
                    "pause overlay was not confirmed; touches are already "
                    "released, continuing as a life-safety abort",
                    flush=True,
                )

        duration_value = params.get("duration_seconds", 30)
        duration_seconds = (
            None if duration_value is None else float(duration_value)
        )
        startup_timeout_seconds = float(
            params.get("startup_timeout_seconds", 60)
        )
        try:
            stall_safe_capture = StallSafeCapture(controller)
            stats = engine.run(
                stall_safe_capture,
                lambda: context.tasker.stopping,
                duration_seconds=duration_seconds,
                target_fps=target_fps,
                continue_after_life_depleted=continue_after_depleted,
                life_exit_threshold=life_threshold,
                on_life_safety=(
                    pause_for_life if life_threshold is not None else None
                ),
                startup_timeout_seconds=startup_timeout_seconds,
            )
            if recorder is not None and stall_safe_capture.last_image is not None:
                _recorder_checkpoint(
                    recorder,
                    stall_safe_capture.last_image,
                    "engine",
                    "completed" if stats.completed else "incomplete",
                    details={
                        "processed_frames": stats.processed_frames,
                        "dispatched_actions": stats.dispatched_actions,
                        "terminal_reason": stats.terminal_reason,
                    },
                )
                _recorder_checkpoint(
                    recorder,
                    stall_safe_capture.last_image,
                    "cleanup",
                    "failed" if stats.cleanup_failed else "completed",
                    details={
                        "cleanup_errors": list(stats.cleanup_errors),
                        "stopped": stats.stopped,
                    },
                )
                append_lifecycle_event(
                    recorder.output_dir,
                    "cleanup",
                    "failed" if stats.cleanup_failed else "completed",
                    details={
                        "cleanup_errors": list(stats.cleanup_errors),
                        "stopped": stats.stopped,
                    },
                )
            if native_requested and not stats.stopped:
                native_report = dict(stats.native_report)
                native_failures = _native_execution_gate_failures(
                    native_report
                )
                if native_failures:
                    native_error = RuntimeError(
                        "Native 演奏未通过完整性门禁："
                        + "; ".join(native_failures)
                    )
                    native_error.realtime_stats = stats
                    raise native_error
        except Exception as exc:
            if (
                recorder is not None
                and "stall_safe_capture" in locals()
                and stall_safe_capture.last_image is not None
            ):
                try:
                    _recorder_checkpoint(
                        recorder,
                        stall_safe_capture.last_image,
                        "engine",
                        "error",
                        details={
                            "reason": f"{type(exc).__name__}: {exc}",
                        },
                    )
                except Exception:
                    pass
            error_stats = getattr(exc, "realtime_stats", None)
            if error_stats is not None and context.tasker.stopping:
                stopped_reason = "用户已停止任务"
                stopped_stats = replace(
                    error_stats,
                    stopped=True,
                    terminal_reason=stopped_reason,
                )
                write_failure_artifacts(
                    stopped_stats,
                    result_status="stopped",
                    reason=stopped_reason,
                )
                return True
            if error_stats is None:
                cleanup_errors = []
                recorder_error = None
                if recorder is not None:
                    try:
                        recorder.close()
                    except Exception as cleanup_error:
                        recorder_error = (
                            f"{type(cleanup_error).__name__}: {cleanup_error}"
                        )
                if touch is not None:
                    try:
                        touch.close()
                    except Exception as cleanup_error:
                        cleanup_errors.append(
                            "touch_close="
                            f"{type(cleanup_error).__name__}: {cleanup_error}"
                        )
                reason = f"preflight error: {type(exc).__name__}: {exc}"
                error_stats = EngineStats(
                    0,
                    0,
                    False,
                    initial_timing_offset_ms=timing_offset_ms,
                    final_timing_offset_ms=timing_offset_ms,
                    terminal_reason=reason,
                    cleanup_failed=bool(cleanup_errors),
                    cleanup_errors=tuple(cleanup_errors),
                    recorder_error=recorder_error,
                    engine_mode="native" if native_requested else "legacy",
                )
                status = "preflight_error"
            else:
                reason = (
                    error_stats.terminal_reason
                    or f"{type(exc).__name__}: {exc}"
                )
                status = "engine_error"
            write_failure_artifacts(
                error_stats,
                result_status=status,
                reason=reason,
            )
            raise
        capture_metrics = stats.stage_timings_ms.get("capture", {})
        print(
            "RealtimeProfilePlay "
            f"frames={stats.processed_frames} actions={stats.dispatched_actions} "
            f"stopped={stats.stopped} life_abort={stats.aborted_for_life} "
            f"life_depleted={stats.life_depleted} completed={stats.completed} "
            f"life_failed={stats.life_failed} "
            f"feedback_fast={stats.timing_feedback_fast} "
            f"feedback_slow={stats.timing_feedback_slow} "
            f"feedback_valid={stats.timing_feedback_valid} "
            f"feedback_ignored={stats.timing_feedback_ignored} "
            f"filtered_adjacent={stats.filtered_adjacent_artifacts} "
            f"rejected_holds={stats.rejected_hold_candidates} "
            f"timing_offset={stats.initial_timing_offset_ms}"
            f"->{stats.final_timing_offset_ms} "
            f"tap={stats.action_counts.get('tap', 0)} "
            f"flick={stats.action_counts.get('flick', 0)} "
            f"hold={stats.action_counts.get('down', 0)} "
            f"frame_ms_p50={stats.frame_interval_p50_ms:.2f} "
            f"frame_ms_p95={stats.frame_interval_p95_ms:.2f} "
            f"frame_ms_max={stats.frame_interval_max_ms:.2f} "
            f"effective_fps={stats.effective_fps:.2f} "
            f"capture_ms_p95={capture_metrics.get('p95', 0.0):.2f} "
            f"capture_ms_max={capture_metrics.get('max', 0.0):.2f} "
            f"frame_outliers={len(stats.frame_interval_outliers)} "
            f"actual_speed={live_run.actual_note_speed} "
            f"expected_speed={live_run.expected_note_speed} "
            f"note_skin_type={live_run.note_skin_type} "
            f"tap_effect={live_run.tap_effect} "
            f"judgement_assist={live_run.judgement_assist} "
            f"touch_recoveries={stats.recovered_contacts} "
            f"down_recoveries={stats.down_recoveries} "
            f"stale_move_recoveries={stats.stale_move_recoveries} "
            f"touch_resets={stats.touch_resets} "
            f"input_wait_count={stats.input_wait_count} "
            f"input_wait_total_ms={stats.input_wait_total_ms:.1f} "
            f"input_wait_max_ms={stats.input_wait_max_ms:.1f} "
            f"reason={stats.terminal_reason}",
            flush=True,
        )
        result_output = PROJECT_ROOT / "screencap"
        result_stamp = (
            datetime.now().strftime("%Y%m%d-%H%M%S")
            + f"-{live_run.run_id[:8]}"
        )
        save_result = bool(params.get("save_result_frame"))
        result_report_path = result_output / f"realtime-result-{result_stamp}.json"

        def write_calibration_payload(payload: dict) -> None:
            calibration_report = params.get("calibration_report")
            if not calibration_report:
                return
            report_path = PROJECT_ROOT / str(calibration_report)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(report_path, {
                **payload,
                "timing_offset_ms": stats.final_timing_offset_ms,
                "survived": not stats.life_depleted,
                "completed": bool(stats.completed),
            })

        if save_result and stats.life_failed and not stats.stopped:
            # 生命归零：先把失败现场落盘，再有界退出到主页。退出导航失败时
            # 不掩盖“演出失败”这一真实原因，后续 CommonRecover 仍可兜底。
            reason = "演出失败：生命值归零"
            record_failure_reason(reason)
            failed_payload = _result_report_payload(
                None,
                stats,
                timing_offset_ms=timing_offset_ms,
                suggested_timing_offset_ms=None,
                run_context=live_run,
                result_status="life_failed",
                reason=reason,
            )
            _write_json_atomic(result_report_path, failed_payload)
            write_calibration_payload(failed_payload)
            if recorder is not None and stall_safe_capture.last_image is not None:
                _recorder_checkpoint(
                    recorder,
                    stall_safe_capture.last_image,
                    "result",
                    "life-failed",
                    details={"reason": reason},
                )
            navigation_ok = False
            try:
                navigation_ok = exit_failed_live(context)
            except Exception as nav_error:
                print(
                    "RealtimeProfilePlay life_failed_exit_error="
                    f"{type(nav_error).__name__}: {nav_error}",
                    flush=True,
                )
            print(
                "RealtimeProfilePlay life_failed "
                f"exit_navigation={'ok' if navigation_ok else 'failed'}",
                flush=True,
            )
            print(
                f"[任务][实时演奏][演奏][ERROR] {reason}",
                flush=True,
            )
            return False

        if save_result and stats.stopped:
            result_output.mkdir(parents=True, exist_ok=True)
            stopped_payload = _result_report_payload(
                None,
                stats,
                timing_offset_ms=timing_offset_ms,
                suggested_timing_offset_ms=None,
                run_context=live_run,
                result_status="stopped",
                reason=stats.terminal_reason or "用户已停止任务",
            )
            _write_json_atomic(result_report_path, stopped_payload)
            write_calibration_payload(stopped_payload)

        if save_result and (
            not stats.completed or stats.cleanup_failed
        ) and not stats.stopped:
            result_output.mkdir(parents=True, exist_ok=True)
            status = (
                "playfield_start_timeout" if stats.startup_timed_out
                else "life_safety_abort" if stats.aborted_for_life
                else "cleanup_failed" if stats.cleanup_failed
                else "engine_incomplete"
            )
            failed_payload = _result_report_payload(
                None,
                stats,
                timing_offset_ms=timing_offset_ms,
                suggested_timing_offset_ms=None,
                run_context=live_run,
                result_status=status,
                reason=stats.terminal_reason or "实时演奏引擎未完成",
            )
            if stats.startup_timed_out and stall_safe_capture.last_image is not None:
                startup_diagnostic = result_output / (
                    f"realtime-startup-timeout-{result_stamp}.png"
                )
                try:
                    if cv2.imwrite(
                        str(startup_diagnostic), stall_safe_capture.last_image,
                    ):
                        failed_payload["startup_diagnostic_frame"] = str(
                            startup_diagnostic.relative_to(PROJECT_ROOT).as_posix()
                        )
                    else:
                        failed_payload["startup_diagnostic_error"] = (
                            f"无法保存开演失败现场: {startup_diagnostic}"
                        )
                except Exception as exc:
                    failed_payload["startup_diagnostic_error"] = (
                        "保存开演失败现场异常: "
                        f"{type(exc).__name__}: {exc}"
                    )
            _write_json_atomic(result_report_path, failed_payload)
            write_calibration_payload(failed_payload)

        if stats.completed and not stats.cleanup_failed and save_result:
            result_output.mkdir(parents=True, exist_ok=True)
            try:
                outcome = collect_result(
                    controller,
                    lambda: context.tasker.stopping,
                    before_input=lambda: require_game_foreground(controller),
                    timeout_seconds=180.0,
                    expected_notes=(
                        selected_chart.expected_notes
                        if selected_chart is not None else None
                    ),
                    cooperative_mode=(run_mode == "cooperative"),
                    robust_navigation=True,
                )
                if recorder is not None and outcome.image is not None:
                    _recorder_checkpoint(
                        recorder,
                        outcome.image,
                        "result",
                        outcome.status.value,
                        details={
                            "page_state": outcome.page_state,
                            "reason": outcome.reason,
                        },
                    )
            except Exception as exc:
                reason = (
                    "结算读取异常: "
                    f"{type(exc).__name__}: {exc}"
                )
                collection_error_payload = _result_report_payload(
                    None,
                    stats,
                    timing_offset_ms=timing_offset_ms,
                    suggested_timing_offset_ms=None,
                    run_context=live_run,
                    result_status="result_collection_error",
                    reason=reason,
                )
                _write_json_atomic(
                    result_report_path, collection_error_payload,
                )
                write_calibration_payload(collection_error_payload)
                raise
            if outcome.status is ResultCollectionStatus.ADVANCED:
                advanced_payload = _result_report_payload(
                    None,
                    stats,
                    timing_offset_ms=timing_offset_ms,
                    suggested_timing_offset_ms=None,
                    run_context=live_run,
                    result_status="cooperative_result_advanced",
                    reason=outcome.reason or "协力总分页已推进",
                )
                _write_json_atomic(result_report_path, advanced_payload)
                print(
                    "RealtimeProfilePlay cooperative_result=advanced",
                    flush=True,
                )
                return True
            if outcome.status is ResultCollectionStatus.STOPPED:
                stopped_payload = _result_report_payload(
                    None,
                    stats,
                    timing_offset_ms=timing_offset_ms,
                    suggested_timing_offset_ms=None,
                    run_context=live_run,
                    result_status="stopped",
                    reason="用户在结算读取期间停止任务",
                )
                _write_json_atomic(result_report_path, stopped_payload)
                write_calibration_payload(stopped_payload)
                print("RealtimeProfilePlay result collection stopped by user", flush=True)
                return True
            if outcome.status in {
                ResultCollectionStatus.TIMED_OUT,
                ResultCollectionStatus.BLOCKED,
            }:
                diagnostic = result_output / (
                    f"realtime-result-timeout-{result_stamp}.png"
                )
                diagnostic_error = None
                diagnostic_saved = False
                if outcome.image is not None:
                    try:
                        diagnostic_saved = bool(
                            cv2.imwrite(str(diagnostic), outcome.image)
                        )
                        if not diagnostic_saved:
                            diagnostic_error = (
                                f"无法保存结算失败现场: {diagnostic}"
                            )
                    except Exception as exc:
                        diagnostic_error = (
                            "保存结算失败现场异常: "
                            f"{type(exc).__name__}: {exc}"
                        )
                reason = outcome.reason or "结算数字在 60 秒内未稳定"
                timeout_payload = _result_report_payload(
                    None,
                    stats,
                    timing_offset_ms=timing_offset_ms,
                    suggested_timing_offset_ms=None,
                    run_context=live_run,
                    result_status=outcome.status.value,
                    reason=reason,
                )
                if diagnostic_saved:
                    timeout_payload["result_diagnostic_frame"] = str(
                        diagnostic.relative_to(PROJECT_ROOT).as_posix()
                    )
                if diagnostic_error is not None:
                    timeout_payload["result_diagnostic_error"] = diagnostic_error
                _write_json_atomic(result_report_path, timeout_payload)
                write_calibration_payload(timeout_payload)
                print(
                    "RealtimeProfilePlay result_timeout=true "
                    f"diagnostic={diagnostic.name if diagnostic_saved else 'none'} "
                    f"reason={reason}",
                    flush=True,
                )
                # A calibration round must return control to its outer state
                # machine so the invalid report can be persisted/resumed.
                # Ordinary play has no such consumer: treating this technical
                # failure as success hid repeated broken result flows from MFA.
                if params.get("calibration_report"):
                    return True
                failure_reason = (
                    f"结算读取失败（{outcome.page_state}）：{reason}"
                )
                record_failure_reason(failure_reason)
                print(
                    f"[任务][实时演奏][结算][ERROR] {failure_reason}",
                    flush=True,
                )
                return False
            result_data = outcome.result
            result = outcome.image
            if result_data is None or result is None:
                reason = "结算读取返回 stable，但判定数据或画面不完整"
                incomplete_payload = _result_report_payload(
                    None,
                    stats,
                    timing_offset_ms=timing_offset_ms,
                    suggested_timing_offset_ms=None,
                    run_context=live_run,
                    result_status="result_collection_error",
                    reason=reason,
                )
                _write_json_atomic(result_report_path, incomplete_payload)
                write_calibration_payload(incomplete_payload)
                raise RuntimeError(reason)
            screenshot_path = result_output / f"realtime-result-{result_stamp}.png"
            screenshot_error = None
            if save_screenshot:
                try:
                    if not cv2.imwrite(str(screenshot_path), result):
                        screenshot_error = (
                            f"无法保存结算截图: {screenshot_path}"
                        )
                except Exception as exc:
                    screenshot_error = (
                        "保存结算截图异常: "
                        f"{type(exc).__name__}: {exc}"
                    )
            effective_timing_offset_ms = stats.final_timing_offset_ms
            suggestion = (
                effective_timing_offset_ms
                if stats.engine_mode == "native"
                else adjusted_timing_offset(
                    effective_timing_offset_ms, result_data,
                )
            )
            stable_payload = _result_report_payload(
                result_data,
                stats,
                timing_offset_ms=timing_offset_ms,
                suggested_timing_offset_ms=suggestion,
                run_context=live_run,
                result_status=(
                    "experimental"
                    if run_mode == "visual-evaluation" else "stable"
                ),
            )
            if screenshot_error is not None:
                stable_payload["result_screenshot_error"] = screenshot_error
            _write_json_atomic(result_report_path, stable_payload)
            if screenshot_error is not None:
                print(
                    "RealtimeProfilePlay screenshot_error="
                    + screenshot_error,
                    flush=True,
                )
            print(
                "RealtimeProfilePlay "
                "result_frame="
                f"{screenshot_path.name if save_screenshot and screenshot_error is None else 'none'} "
                f"perfect={result_data.perfect} great={result_data.great} "
                f"good={result_data.good} bad={result_data.bad} miss={result_data.miss} "
                f"fast={result_data.fast} slow={result_data.slow} "
                f"timing_offset={timing_offset_ms}"
                f"->{effective_timing_offset_ms}->{suggestion}",
                flush=True,
            )
            # 正式演奏（非排练/校准/视觉评估）在结算稳定后把建议写回 Profile，
            # 修正会话输入延迟漂移；下一次开演即使用修正后的起始偏移。
            if (
                settings is not None
                and stats.engine_mode != "native"
                and not is_rehearsal
                and run_mode
                not in {
                    "calibration-rehearsal",
                    "calibration-formal",
                    "visual-evaluation",
                }
                and suggestion != timing_offset_ms
            ):
                _persist_profile_timing_offset(settings, suggestion)
            calibration_report = params.get("calibration_report")
            if calibration_report:
                from .calibration_action import current_song_id

                report_path = PROJECT_ROOT / str(calibration_report)
                report_path.parent.mkdir(parents=True, exist_ok=True)
                _write_calibration_report(
                    report_path,
                    result=result_data,
                    stats=stats,
                    timing_offset_ms=effective_timing_offset_ms,
                    song_id=current_song_id(),
                    run_context=live_run,
                )
        if stats.stopped:
            print("[任务][实时演奏][结束][INFO] 用户已停止任务", flush=True)
            return True
        success = not stats.aborted_for_life and not stats.cleanup_failed
        if params.get("require_completion"):
            success = success and stats.completed
        if not success:
            if (
                run_mode in {"calibration-rehearsal", "calibration-formal"}
                and not stats.cleanup_failed
            ):
                print(
                    "RealtimeProfilePlay calibration_round_retry=true "
                    f"reason={stats.terminal_reason or '实时演奏引擎未完成'}",
                    flush=True,
                )
                return True
            reason = stats.terminal_reason or "实时演奏引擎未完成"
            record_failure_reason(reason)
            print(f"[任务][实时演奏][演奏][ERROR] {reason}", flush=True)
        return success


@AgentServer.custom_action("RealtimeLifeSafetyAbortCheck")
class RealtimeLifeSafetyAbortCheck(CustomAction):
    """Route a protected abort to StopTask while ordinary failures may recover."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        return _LAST_LIFE_SAFETY_ABORT
