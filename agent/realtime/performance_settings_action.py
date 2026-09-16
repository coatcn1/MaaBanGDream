from __future__ import annotations

import json
import math
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

import cv2
import numpy as np
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    from ..foreground_guard import require_game_foreground
    from ..task_reporting import record_failure_reason
except ImportError:
    from foreground_guard import require_game_foreground
    from task_reporting import record_failure_reason

from .profile_action import PROJECT_ROOT
from .profile_store import (
    EnvironmentSignature,
    RealtimeProfileStore,
    engine_from_native_flag,
)
from .rehearsal_action import frame_resolution
from .live_session import (
    current_live_run,
    effective_difficulty_for_current_run,
    update_live_run,
)
from .native_prearm import (
    discard_prearmed_backend,
    prepare_native_for_settings_gate,
)
from .chart_repository import LocalChartRepository
from .run_reporting import (
    PreflightPerformanceSnapshot,
    write_preflight_terminal_result,
)
from .vision_io import imread_unicode, imwrite_unicode


@dataclass(frozen=True)
class VerifiedPerformanceSettings:
    difficulty: str
    actual_note_speed: float
    expected_note_speed: float
    profile: str | None
    verified_at: float


_VERIFIED: dict[str, VerifiedPerformanceSettings] = {}
_MINIMUM_NOTE_SPEED = 1.0
_MAXIMUM_NOTE_SPEED = 12.0
_DIGIT_TEMPLATE_PATH = (
    PROJECT_ROOT / "resource" / "image" / "performance_settings" / "speed_digits.png"
)

# Coordinates are in MaaFramework's canonical 1280x720 game frame.
DEFAULT_COORDINATES = {
    "gear": (960, 650),
    # First top tab "演出设定" of the settings dialog. The game remembers
    # the last-used tab, so the gear can open on any tab; always click the
    # first one before touching the speed controls. Measured on the live
    # dialog: tab text spans x 265-330 at y ~155; (430,155) is tab 2.
    "first_tab": (297, 155),
    "speed_roi": (360, 285, 120, 55),
    "decrease_050": (207, 312),
    "decrease_010": (268, 312),
    "decrease_001": (330, 312),
    "increase_001": (513, 312),
    "increase_010": (575, 312),
    "increase_050": (635, 312),
    "close": (640, 600),
}

_SPEED_SETTINGS_POLICY_VERSION = 1
_ACTIVE_SPEED_TARGET: dict | None = None


def _speed_settings_target(*, note_speed: float) -> dict:
    return {
        "policy_version": _SPEED_SETTINGS_POLICY_VERSION,
        "note_speed": round(float(note_speed), 2),
    }


def verified_settings(
    difficulty: str,
    *,
    max_age_seconds: float = 90.0,
    clock=time.monotonic,
) -> VerifiedPerformanceSettings | None:
    value = _VERIFIED.get(difficulty)
    if value is None or clock() - value.verified_at > max_age_seconds:
        return None
    return value


def clear_verified_settings() -> None:
    _VERIFIED.clear()


def clear_active_speed_settings_target() -> None:
    global _ACTIVE_SPEED_TARGET
    _ACTIVE_SPEED_TARGET = None


def publish_verified_performance_settings(
    *,
    difficulty: str,
    actual_note_speed: float,
    expected_note_speed: float,
    profile: str | None,
) -> VerifiedPerformanceSettings:
    value = VerifiedPerformanceSettings(
        difficulty=difficulty,
        actual_note_speed=actual_note_speed,
        expected_note_speed=expected_note_speed,
        profile=profile,
        verified_at=time.monotonic(),
    )
    _VERIFIED[difficulty] = value
    return value


def activate_speed_settings_target(target: dict) -> None:
    global _ACTIVE_SPEED_TARGET
    _ACTIVE_SPEED_TARGET = dict(target)


def _speed_cents(value: float) -> int:
    if not math.isfinite(value):
        raise ValueError(f"流速必须是有限数值，收到 {value!r}")
    cents = int(round(value * 100))
    if abs(value * 100 - cents) > 1e-6:
        raise ValueError(f"流速必须精确到 0.01，收到 {value!r}")
    if not 100 <= cents <= 1200:
        raise ValueError(
            f"流速 {value:.2f} 超出游戏范围 "
            f"{_MINIMUM_NOTE_SPEED:.2f}–{_MAXIMUM_NOTE_SPEED:.2f}"
        )
    return cents


def _speed_click_plan(actual: float, expected: float) -> list[tuple[str, int]]:
    difference = _speed_cents(expected) - _speed_cents(actual)
    increasing = difference > 0
    remaining = abs(difference)
    plan: list[tuple[str, int]] = []
    for amount, decrease, increase in (
        (50, "decrease_050", "increase_050"),
        (10, "decrease_010", "increase_010"),
        (1, "decrease_001", "increase_001"),
    ):
        clicks, remaining = divmod(remaining, amount)
        if clicks:
            plan.append((increase if increasing else decrease, clicks))
    return plan


def _normalise_glyph(mask: np.ndarray) -> np.ndarray:
    ys, xs = np.where(mask)
    if not len(xs):
        raise RuntimeError("流速数字字形为空")
    glyph = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.uint8)
    return cv2.resize(glyph, (20, 28), interpolation=cv2.INTER_NEAREST).astype(bool)


@lru_cache(maxsize=1)
def _digit_templates() -> tuple[np.ndarray, ...]:
    sprite = imread_unicode(_DIGIT_TEMPLATE_PATH, cv2.IMREAD_GRAYSCALE)
    if sprite is None or sprite.shape != (28, 200):
        raise RuntimeError(f"流速数字模板损坏：{_DIGIT_TEMPLATE_PATH}")
    return tuple(sprite[:, index * 20:(index + 1) * 20] >= 128 for index in range(10))


def _classify_digit(mask: np.ndarray) -> int:
    glyph = _normalise_glyph(mask)
    scores = np.asarray([
        np.mean(glyph != template)
        for template in _digit_templates()
    ])
    order = np.argsort(scores)
    best = int(order[0])
    margin = float(scores[order[1]] - scores[order[0]])
    if float(scores[best]) > 0.30 or margin < 0.02:
        raise RuntimeError(
            f"流速数字模板不确定：候选 {best}，差异 {scores[best]:.3f}，"
            f"区分度 {margin:.3f}"
        )
    return best


def _read_speed(image, roi: tuple[int, int, int, int]) -> float:
    x, y, width, height = roi
    display = image[y:y + height, x:x + width]
    if display.shape[:2] != (height, width):
        raise RuntimeError(f"流速数字区域越界：{roi}")
    gray = cv2.cvtColor(display, cv2.COLOR_BGR2GRAY)
    mask = gray < 180
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    dot_candidates = [
        int(component_x)
        for component_x, component_y, component_width, component_height, area
        in stats[1:]
        if (
            35 <= component_x <= 70
            and 30 <= component_y <= 42
            and component_width <= 5
            and component_height <= 5
            and 3 <= area <= 20
        )
    ]
    if len(dot_candidates) != 1:
        raise RuntimeError(f"无法定位流速小数点：候选 {dot_candidates}")
    dot_x = dot_candidates[0]
    patches = {
        "tens": mask[18:43, dot_x - 30:dot_x - 16],
        "units": mask[18:43, dot_x - 16:dot_x - 2],
        "tenths": mask[18:43, dot_x + 5:dot_x + 19],
        "hundredths": mask[18:43, dot_x + 20:dot_x + 34],
    }
    if any(patch.shape != (25, 14) for patch in patches.values()):
        raise RuntimeError("流速数字超出固定显示区域")
    tens = None if int(patches["tens"].sum()) < 15 else _classify_digit(patches["tens"])
    units = _classify_digit(patches["units"])
    tenths = _classify_digit(patches["tenths"])
    hundredths = _classify_digit(patches["hundredths"])
    value = (
        (0 if tens is None else tens * 10)
        + units
        + tenths / 10
        + hundredths / 100
    )
    _speed_cents(value)
    return value


def _expected_speed(context: Context, params: dict, image) -> tuple[float, str | None]:
    difficulty = str(params.get("difficulty", "Easy"))
    store = RealtimeProfileStore(PROJECT_ROOT / "profiles")
    if bool(params.get("require_profile", False)):
        runtime_options = store.runtime_options()
        signature = EnvironmentSignature(
            frame_resolution(image),
            int(params.get("dpi", 240)),
            int(params.get("game_fps", 60)),
            str(params.get("render_quality", "standard")),
            1.0,
            engine=engine_from_native_flag(
                runtime_options.get("native_realtime_enabled", False)
            ),
        )
        settings = store.resolve_latest_for_environment(
            difficulty=difficulty, current_signature=signature
        )
        return settings.note_speed, settings.profile_path.name
    speeds = store.runtime_options()["calibration_note_speeds"]
    return float(speeds[difficulty]), None


def _click(controller, point: tuple[int, int]) -> None:
    require_game_foreground(controller)
    controller.post_click(*point).wait()


def _read_speed_stable(
    read_current,
    *,
    attempts: int = 3,
    delay_seconds: float = 0.3,
) -> float:
    """Read the speed display, tolerating transient mid-animation frames."""
    last_error: RuntimeError | None = None
    for _ in range(attempts):
        try:
            return read_current()
        except RuntimeError as exc:
            last_error = exc
            time.sleep(delay_seconds)
    raise RuntimeError(f"流速读数不可识别：{last_error}")


def _select_first_tab_and_read(
    controller,
    coordinates: dict[str, tuple[int, int]],
    read_current,
    *,
    attempts: int,
    settle_delay_seconds: float,
    click_point: Callable[[tuple[int, int]], None] | None = None,
) -> float:
    """Select 演出设定 with a visual readback loop.

    A coordinate click is not proof that the remembered settings tab changed:
    the dialog animation can silently drop the first input.  Re-click the tab
    only after the fixed-digit display remains unreadable for a full stable
    read cycle.
    """
    last_error: RuntimeError | None = None
    click_point = click_point or (lambda point: _click(controller, point))
    for _ in range(max(1, attempts)):
        click_point(coordinates["first_tab"])
        time.sleep(settle_delay_seconds)
        try:
            return _read_speed_stable(read_current)
        except RuntimeError as exc:
            last_error = exc
    raise RuntimeError(
        f"无法进入“演出设定”流速页：重复点击页签后仍不可读取：{last_error}"
    )


def _adjust_speed(
    context: Context,
    controller,
    coordinates: dict[str, tuple[int, int]],
    actual: float,
    expected: float,
    *,
    button_delay_seconds: float,
    settle_delay_seconds: float,
    round_limit: int,
    read_current,
    click_point: Callable[[tuple[int, int]], None] | None = None,
) -> tuple[bool, float | None]:
    """Click in a closed read-click-reread loop until the display matches.

    The game silently drops clicks that arrive too fast, so a one-shot plan
    can land anywhere. Each round re-plans from the freshly read value, which
    makes dropped clicks self-correcting. Blind re-clicking on an unreadable
    display is forbidden: a persistent read failure blocks the run instead.
    """
    reading = actual
    click_point = click_point or (lambda point: _click(controller, point))
    for round_index in range(round_limit):
        plan = _speed_click_plan(reading, expected)
        if not plan:
            return True, reading
        for coordinate_name, count in plan:
            for _ in range(count):
                if context.tasker.stopping:
                    return False, None
                click_point(coordinates[coordinate_name])
                if button_delay_seconds > 0:
                    time.sleep(button_delay_seconds)
        time.sleep(settle_delay_seconds)
        new_reading = _read_speed_stable(read_current)
        if abs(new_reading - expected) <= 0.005:
            return True, new_reading
        if new_reading == reading and round_index > 0:
            raise RuntimeError(
                f"流速点击未生效：连续两轮读数均为 {reading:.2f}，"
                f"无法调到 {expected:.2f}"
            )
        reading = new_reading
    raise RuntimeError(
        f"流速调整未收敛：{round_limit} 轮后实际 {reading:.2f}，"
        f"期望 {expected:.2f}"
    )


def _close_settings_dialog(
    context: Context,
    controller,
    coordinates: dict[str, tuple[int, int]],
    *,
    attempts: int,
    delay_seconds: float,
    click_point: Callable[[tuple[int, int]], None] | None = None,
    capture_current: Callable[[], object] | None = None,
) -> None:
    """Close the settings dialog and prove that the speed display vanished."""
    click_point = click_point or (lambda point: _click(controller, point))
    capture_current = capture_current or (
        lambda: controller.post_screencap().wait().get()
    )
    for attempt in range(1, max(1, attempts) + 1):
        if context.tasker.stopping:
            return
        click_point(coordinates["close"])
        time.sleep(delay_seconds)
        image = capture_current()
        try:
            _read_speed(image, coordinates["speed_roi"])
        except (RuntimeError, StopIteration):
            return
        if attempt < max(1, attempts):
            print(
                "RealtimePerformanceSettingsGate close_retry "
                f"attempt={attempt + 1}/{max(1, attempts)}",
                flush=True,
            )
    raise RuntimeError(
        f"演出设置关闭按钮连续点击 {max(1, attempts)} 次后，"
        "流速显示仍然可见"
    )


def require_special_chart_for_settings_gate(difficulty: str):
    """Special 必须在点击开始前持有本局可信本地谱面。"""
    if difficulty.casefold() != "special":
        return None
    run = current_live_run()
    if (
        run is None
        or not run.prepared_for_play
        or run.difficulty.casefold() != "special"
    ):
        raise RuntimeError("Special 开演前缺少本局实际难度证据")
    resolution = LocalChartRepository(
        PROJECT_ROOT / "resource" / "charts"
    ).resolve(
        run.song_id,
        difficulty,
        level=run.song_level,
        title=run.song_title,
    )
    if resolution.selection is None:
        raise RuntimeError(
            "Special 必须先确认可信本地谱面，禁止按视觉回退开演："
            f"{resolution.reason}"
        )
    print(
        "RealtimePerformanceSettingsGate special_chart=confirmed "
        f"bestdori_song_id={resolution.selection.bestdori_song_id} "
        f"difficulty={resolution.selection.difficulty}",
        flush=True,
    )
    return resolution.selection


@AgentServer.custom_action("RealtimePerformanceSettingsGate")
class RealtimePerformanceSettingsGate(CustomAction):
    """Read and adjust note speed on the explicitly selected first settings tab."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        params: dict = {}
        run_context = None
        performance_snapshot = None

        def capture_snapshot(snapshot: PreflightPerformanceSnapshot) -> None:
            nonlocal performance_snapshot
            performance_snapshot = snapshot

        try:
            if context.tasker.stopping:
                return True
            params = json.loads(argv.custom_action_param or "{}")
            run_context = current_live_run()
            return self._run(
                context,
                params,
                on_expected=capture_snapshot,
            )
        except Exception as exc:
            if context.tasker.stopping:
                print(
                    "RealtimePerformanceSettingsGate stopped=true",
                    flush=True,
                )
                return True
            reason = f"{type(exc).__name__}: {exc}"
            record_failure_reason(f"开演前流速设置失败：{reason}")
            try:
                latest_run = current_live_run()
                if latest_run is not None and latest_run.preparation_identity_image is not None:
                    run_context = latest_run
                    evidence_dir = PROJECT_ROOT / "screencap"
                    evidence_dir.mkdir(parents=True, exist_ok=True)
                    evidence_path = evidence_dir / f"preparation-identity-{latest_run.run_id}.png"
                    if not imwrite_unicode(evidence_path, latest_run.preparation_identity_image):
                        print("RealtimePreparationIdentity evidence_save_failed=true", flush=True)
                write_preflight_terminal_result(
                    output_dir=PROJECT_ROOT / "screencap",
                    params=params,
                    terminal_stage="performance_settings_gate",
                    reason=reason,
                    performance_snapshot=performance_snapshot,
                    run_context=run_context,
                )
            except Exception as artifact_error:
                print(
                    "RealtimePerformanceSettingsGate artifact_failed="
                    f"{type(artifact_error).__name__}: {artifact_error}",
                    flush=True,
                )
                traceback.print_exc()
            traceback.print_exc()
            print(
                "RealtimePerformanceSettingsGate "
                f"failed={type(exc).__name__}: {exc}",
                flush=True,
            )
            return False

    def _run(
        self,
        context: Context,
        params: dict,
        *,
        on_expected: Callable[[PreflightPerformanceSnapshot], None] | None = None,
    ) -> bool:
        global _ACTIVE_SPEED_TARGET
        if context.tasker.stopping:
            return True
        requested_difficulty = str(params.get("difficulty", "Easy"))
        difficulty = effective_difficulty_for_current_run(
            requested_difficulty
        )
        if difficulty not in RealtimeProfileStore.DIFFICULTIES:
            raise ValueError(f"不支持的难度：{difficulty}")
        effective_params = dict(params)
        effective_params["difficulty"] = difficulty
        if difficulty != requested_difficulty:
            print(
                "RealtimePerformanceSettingsGate difficulty_fallback=true "
                f"requested={requested_difficulty} effective={difficulty}",
                flush=True,
            )
        controller = context.tasker.controller
        before = controller.post_screencap().wait().get()
        if context.tasker.stopping:
            return True
        if params.get("confirm_preparation_identity", False):
            # 单人/校准在“演出开始”按钮出现的准备页左下角复核标题、等级
            # 与难度；共享封面（如 FIRE BIRD 与 [FULL]FIRE BIRD）必须靠
            # 这条标题才能安全区分，不再只由 OrderedStartupTrial 启用。
            from .preparation_identity import confirm_preparation_identity
            confirm_preparation_identity(before, difficulty)
            if context.tasker.stopping:
                return True
        run = current_live_run()
        identity_pending_final_cover = bool(
            run is not None
            and (
                run.preparation_title_pending_final_cover
                or run.preparation_identity_pending_final_cover
            )
        )
        if identity_pending_final_cover:
            # 延迟身份只能由最终封面补全；设置门不能以选曲页遗留身份预武装。
            effective_params["defer_native_prearm"] = True
            print(
                "RealtimePerformanceSettingsGate native_prearm=deferred "
                "reason=preparation-identity-pending-final-cover",
                flush=True,
            )
        if difficulty.casefold() == "special" and identity_pending_final_cover:
            print(
                "RealtimePerformanceSettingsGate special_chart=deferred "
                "reason=preparation-identity-pending-final-cover",
                flush=True,
            )
        else:
            require_special_chart_for_settings_gate(difficulty)
        expected, profile = _expected_speed(
            context, effective_params, before
        )
        _speed_cents(expected)
        if on_expected is not None:
            on_expected(PreflightPerformanceSnapshot(
                expected_note_speed=expected,
                profile=profile,
            ))
        store = RealtimeProfileStore(PROJECT_ROOT / "profiles")
        options = store.runtime_options()

        def finish_without_dialog(source: str) -> bool:
            publish_verified_performance_settings(
                difficulty=difficulty,
                actual_note_speed=expected,
                expected_note_speed=expected,
                profile=profile,
            )
            print(
                "RealtimePerformanceSettingsGate settings_dialog=skipped "
                f"source={source} difficulty={difficulty} "
                f"expected={expected:.2f} "
                f"profile={profile or 'calibration-setting'}",
                flush=True,
            )
            if bool(effective_params.get("cache_preparation_image", False)):
                update_live_run(cooperative_prestart_image=before.copy())
                print(
                    "RealtimePerformanceSettingsGate preparation_image=cached "
                    f"reason={source}",
                    flush=True,
                )
            if bool(effective_params.get("defer_native_prearm", False)):
                discard_prearmed_backend("deferred-until-final-cover")
                print(
                    "RealtimePerformanceSettingsGate native_prearm=deferred "
                    f"reason={source}",
                    flush=True,
                )
            else:
                prepare_native_for_settings_gate(
                    controller=controller,
                    live_run=current_live_run(),
                    difficulty=difficulty,
                    project_root=PROJECT_ROOT,
                    ready_timeout_s=float(
                        effective_params.get("native_ready_timeout_seconds", 10.0)
                    ),
                    ttl_s=float(
                        effective_params.get("native_prearm_ttl_seconds", 30.0)
                    ),
                )
            if context.tasker.stopping:
                discard_prearmed_backend("user-stopped-after-prearm")
            return True

        if not bool(options.get("note_speed_settings_enabled", True)):
            # 用户关闭流速自动检查后，准备页直接信任声明值；Native 预武装
            # 与准备页身份校验仍必须照常完成。
            print(
                "RealtimePerformanceSettingsGate enabled=false skipped=true "
                f"difficulty={difficulty} expected={expected:.2f} "
                f"source=configured-values profile={profile or 'calibration-setting'}",
                flush=True,
            )
            return finish_without_dialog("configured-values")

        speed_target = _speed_settings_target(note_speed=expected)
        if _ACTIVE_SPEED_TARGET == speed_target:
            _ACTIVE_SPEED_TARGET = dict(speed_target)
            return finish_without_dialog("home-task-speed-result")
        if _ACTIVE_SPEED_TARGET is not None:
            raise RuntimeError(
                "主页流速校验与本局目标不一致；"
                "为避免在开演前打开设置页，本局已阻止开演"
            )
        raise RuntimeError(
            "开关已开启，但本任务必须先在主页完成流速校验；"
            "准备页禁止打开设置齿轮"
        )
