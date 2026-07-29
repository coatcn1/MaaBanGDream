from __future__ import annotations

import json
import math
import time
import traceback
from collections.abc import Callable

import cv2
import numpy as np
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    from ..foreground_guard import require_game_foreground
    from ..screen_refresh import capture_image
    from ..task_reporting import record_failure_reason
except ImportError:
    from foreground_guard import require_game_foreground
    from screen_refresh import capture_image
    from task_reporting import record_failure_reason

from .performance_settings_action import _classify_digit
from .profile_action import PROJECT_ROOT
from .profile_store import RealtimeProfileStore


# Canonical MaaFramework coordinates for the 1280x720 Bilibili client.
# Every state-changing setting click is followed by a visual readback.
DEFAULT_COORDINATES = {
    "home_menu": (1225, 55),
    "options": (755, 301),
    "performance_tab": (296, 155),
    "skin_tab": (750, 156),
    "settings_close": (640, 600),
    "menu_close": (640, 566),
    "scroll_start": (700, 500),
    "scroll_end": (700, 200),
    "skin_scroll_start": (844, 320),
    "skin_scroll_end": (844, 180),
    "assist_enabled_x": (202,),
    "assist_disabled_x": (302,),
    "tap_effect_left": (206, 472),
    "tap_effect_right": (573, 472),
    # Unity's ScrollRect can settle at different vertical offsets even for
    # identical swipes. Search the narrow centre column for the TAP EFFECT
    # digit instead of assuming a fixed y coordinate.
    "tap_effect_search_roi": (360, 180, 100, 360),
}


def _pink_pixels(image: np.ndarray, point: tuple[int, int]) -> int:
    x, y = point
    patch = image[max(0, y - 15):y + 16, max(0, x - 19):x + 20]
    if patch.size == 0:
        return 0
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    return int(np.count_nonzero(
        (((hsv[:, :, 0] <= 12) | (hsv[:, :, 0] >= 145))
         & (hsv[:, :, 1] >= 100)
         & (hsv[:, :, 2] >= 150))
    ))


def _read_binary_choice(
    image: np.ndarray,
    *,
    enabled_point: tuple[int, int],
    disabled_point: tuple[int, int],
) -> bool | None:
    enabled = _pink_pixels(image, enabled_point)
    disabled = _pink_pixels(image, disabled_point)
    if max(enabled, disabled) < 80 or abs(enabled - disabled) < 60:
        return None
    return enabled > disabled


def _find_bottom_binary_choice(
    image: np.ndarray,
    *,
    enabled_x: int,
    disabled_x: int,
) -> tuple[bool, int] | None:
    """Find the lowest visible selected two-choice radio button.

    判定辅助效果 is the last radio row on 演出设定. Its selected button is
    a compact magenta 23x23 component; finding the bottommost such component
    avoids tying the action to a scroll-dependent y coordinate.
    """
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = (
        (((hsv[:, :, 0] <= 12) | (hsv[:, :, 0] >= 145))
         & (hsv[:, :, 1] >= 100)
         & (hsv[:, :, 2] >= 150))
    ).astype(np.uint8)
    _, _, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    minimum_x = min(enabled_x, disabled_x) - 20
    maximum_x = max(enabled_x, disabled_x) + 20
    candidates: list[tuple[float, float]] = []
    for index, (x, y, width, height, area) in enumerate(stats[1:], 1):
        center_x, center_y = centroids[index]
        if (
            minimum_x <= center_x <= maximum_x
            and 150 <= center_y <= 570
            and 15 <= width <= 30
            and 15 <= height <= 30
            and area >= 250
        ):
            candidates.append((float(center_y), float(center_x)))
    if not candidates:
        return None
    center_y, center_x = max(candidates)
    return (
        abs(center_x - enabled_x) < abs(center_x - disabled_x),
        round(center_y),
    )


def _read_tap_effect(
    image: np.ndarray,
    *,
    roi: tuple[int, int, int, int],
    classify: Callable[[np.ndarray], int] = _classify_digit,
) -> int:
    x, y, width, height = roi
    display = image[y:y + height, x:x + width]
    if display.shape[:2] != (height, width):
        raise RuntimeError(f"TAP EFFECT 数字区域越界：{roi}")
    gray = cv2.cvtColor(display, cv2.COLOR_BGR2GRAY)
    value = int(classify(gray < 180))
    if not 1 <= value <= 5:
        raise RuntimeError(f"TAP EFFECT 读数越界：{value}")
    return value


def _find_tap_effect(
    image: np.ndarray,
    *,
    search_roi: tuple[int, int, int, int],
    classify: Callable[[np.ndarray], int] = _classify_digit,
) -> tuple[int, int] | None:
    """Locate the topmost TAP EFFECT digit and return (value, row_y).

    The digit is the first numeric selector in the skin-settings page. Its
    x coordinate is stable, while the Unity scroll position is not.
    """
    x, y, width, height = search_roi
    display = image[y:y + height, x:x + width]
    if display.shape[:2] != (height, width):
        raise RuntimeError(f"TAP EFFECT 搜索区域越界：{search_roi}")
    gray = cv2.cvtColor(display, cv2.COLOR_BGR2GRAY)
    mask = (gray < 180).astype(np.uint8)
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    candidates: list[tuple[int, int]] = []
    for component_x, component_y, component_w, component_h, area in stats[
        1:count
    ]:
        if not (
            4 <= component_w <= 25
            and 18 <= component_h <= 28
            and 30 <= area <= 300
        ):
            continue
        pad = 4
        left = max(0, component_x - pad)
        top = max(0, component_y - pad)
        right = min(width, component_x + component_w + pad)
        bottom = min(height, component_y + component_h + pad)
        try:
            value = int(classify(mask[top:bottom, left:right]))
        except RuntimeError:
            continue
        if 1 <= value <= 5:
            candidates.append(
                (int(component_y + component_h // 2 + y), value)
            )
    if not candidates:
        return None
    row_y, value = min(candidates)
    return value, row_y


def _tap_effect_click_plan(actual: int, expected: int) -> tuple[str, int]:
    if not 1 <= actual <= 5 or not 1 <= expected <= 5:
        raise ValueError("TAP EFFECT 必须在 1..5 之间")
    right = (expected - actual) % 5
    left = (actual - expected) % 5
    return ("right", right) if right <= left else ("left", left)


def _save_readback_failure(image: np.ndarray, stage: str) -> str:
    path = PROJECT_ROOT / "debug" / f"game-effect-{stage}-readback.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), image)
    return str(path)


def _click(context: Context, point: tuple[int, int]) -> None:
    controller = context.tasker.controller
    require_game_foreground(controller)
    detail = context.run_task(
        "RealtimeGameEffectClick",
        {
            "RealtimeGameEffectClick": {
                "target": [point[0], point[1], 1, 1],
            }
        },
    )
    if not detail or not detail.status.succeeded:
        raise RuntimeError("MaaFramework click task did not complete")


def _swipe(
    context: Context,
    start: tuple[int, int],
    end: tuple[int, int],
    duration_ms: int,
) -> None:
    controller = context.tasker.controller
    require_game_foreground(controller)
    # Execute the gesture in MaaFramework's main process. Direct reverse
    # controller swipe/shell jobs are not ABI-safe in Agent callbacks on this
    # LDPlayer runtime (native access violation / unsigned job-id overflow).
    segments = max(
        1,
        math.ceil(
            max(abs(end[0] - start[0]), abs(end[1] - start[1])) / 50
        ),
    )
    detail = context.run_task(
        "RealtimeGameEffectSwipe",
        {
            "RealtimeGameEffectSwipe": {
                "begin": [start[0], start[1], 1, 1],
                "end": [
                    [
                        round(start[0] + (end[0] - start[0]) * index / segments),
                        round(start[1] + (end[1] - start[1]) * index / segments),
                        1,
                        1,
                    ]
                    for index in range(1, segments + 1)
                ],
                "duration": [
                    max(1, round(duration_ms / segments))
                    for _ in range(segments)
                ],
            }
        },
    )
    if not detail or not detail.status.succeeded:
        raise RuntimeError("MaaFramework swipe task did not complete")


def _scroll_to_top(
    context: Context,
    coordinates: dict[str, tuple[int, ...]],
    *,
    steps: int,
    duration_ms: int,
    delay_seconds: float,
) -> None:
    for _ in range(steps):
        _swipe(
            context,
            coordinates["scroll_end"],
            coordinates["scroll_start"],
            duration_ms,
        )
        if delay_seconds:
            time.sleep(delay_seconds)


def _scroll_down(
    context: Context,
    coordinates: dict[str, tuple[int, ...]],
    *,
    steps: int,
    duration_ms: int,
    delay_seconds: float,
    start_key: str = "scroll_start",
    end_key: str = "scroll_end",
) -> None:
    for _ in range(steps):
        _swipe(
            context,
            coordinates[start_key],
            coordinates[end_key],
            duration_ms,
        )
        if delay_seconds:
            time.sleep(delay_seconds)


@AgentServer.custom_action("RealtimeGameEffectSettingsGate")
class RealtimeGameEffectSettingsGate(CustomAction):
    """Apply and verify detector-friendly game effect settings from Home."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            decoded = json.loads(argv.custom_action_param or "{}")
            return self._run(
                context,
                decoded if isinstance(decoded, dict) else {},
            )
        except Exception as exc:
            record_failure_reason(
                f"游戏演出特效设置失败：{type(exc).__name__}: {exc}"
            )
            traceback.print_exc()
            print(
                "RealtimeGameEffectSettingsGate "
                f"failed={type(exc).__name__}: {exc}",
                flush=True,
            )
            return False

    def _run(self, context: Context, params: dict) -> bool:
        if context.tasker.stopping:
            return True
        options = RealtimeProfileStore(
            PROJECT_ROOT / "profiles"
        ).runtime_options()
        if not options["game_effect_settings_enabled"]:
            print(
                "RealtimeGameEffectSettingsGate enabled=false skipped=true",
                flush=True,
            )
            return True

        expected_assist = bool(options["judgement_assist_effect"])
        expected_tap = int(options["tap_effect"])
        coordinates = dict(DEFAULT_COORDINATES)
        coordinates.update(params.get("coordinates", {}))
        coordinates = {
            key: tuple(int(value) for value in point)
            for key, point in coordinates.items()
        }
        delay = float(params.get("delay_seconds", 0.35))
        page_delay = float(params.get("page_delay_seconds", 0.7))
        swipe_delay = float(params.get("swipe_delay_seconds", 0.35))
        swipe_duration = int(params.get("swipe_duration_ms", 250))
        controller = context.tasker.controller
        menu_open = False
        settings_open = False
        changed_assist = False
        changed_tap = False
        try:
            _click(context, coordinates["home_menu"])
            menu_open = True
            time.sleep(page_delay)
            _click(context, coordinates["options"])
            settings_open = True
            time.sleep(page_delay)

            _click(context, coordinates["performance_tab"])
            time.sleep(delay)
            _scroll_to_top(
                context,
                coordinates,
                steps=int(params.get("scroll_reset_steps", 5)),
                duration_ms=swipe_duration,
                delay_seconds=swipe_delay,
            )
            _scroll_down(
                context,
                coordinates,
                steps=int(params.get("assist_scroll_steps", 2)),
                duration_ms=swipe_duration,
                delay_seconds=swipe_delay,
            )
            time.sleep(delay)
            image = capture_image(context)
            selected = _find_bottom_binary_choice(
                image,
                enabled_x=coordinates["assist_enabled_x"][0],
                disabled_x=coordinates["assist_disabled_x"][0],
            )
            if selected is None:
                capture_path = _save_readback_failure(image, "assist")
                raise RuntimeError(
                    "无法定位“判定辅助效果”的开关状态；"
                    f"读回截图={capture_path}"
                )
            actual_assist, assist_y = selected
            if actual_assist != expected_assist:
                _click(
                    context,
                    (
                        coordinates[
                            "assist_enabled_x" if expected_assist
                            else "assist_disabled_x"
                        ][0],
                        assist_y,
                    ),
                )
                changed_assist = True
                time.sleep(delay)
                confirmed_image = capture_image(context)
                confirmed_selected = _find_bottom_binary_choice(
                    confirmed_image,
                    enabled_x=coordinates["assist_enabled_x"][0],
                    disabled_x=coordinates["assist_disabled_x"][0],
                )
                if (
                    confirmed_selected is None
                    or confirmed_selected[0] != expected_assist
                ):
                    capture_path = _save_readback_failure(
                        confirmed_image, "assist-confirmed"
                    )
                    raise RuntimeError(
                        "“判定辅助效果”点击后读回状态不一致；"
                        f"读回截图={capture_path}"
                    )

            _click(context, coordinates["skin_tab"])
            time.sleep(delay)
            _scroll_to_top(
                context,
                coordinates,
                steps=int(params.get("scroll_reset_steps", 5)),
                duration_ms=swipe_duration,
                delay_seconds=swipe_delay,
            )
            _scroll_down(
                context,
                coordinates,
                steps=int(params.get("tap_effect_scroll_steps", 4)),
                duration_ms=swipe_duration,
                delay_seconds=swipe_delay,
                start_key="skin_scroll_start",
                end_key="skin_scroll_end",
            )
            time.sleep(delay)
            tap_image = capture_image(context)
            try:
                located_tap = _find_tap_effect(
                    tap_image,
                    search_roi=coordinates["tap_effect_search_roi"],
                )
            except RuntimeError as exc:
                capture_path = _save_readback_failure(
                    tap_image, "tap-effect"
                )
                raise RuntimeError(
                    f"{exc}；读回截图={capture_path}"
                ) from exc
            if located_tap is None:
                capture_path = _save_readback_failure(
                    tap_image, "tap-effect"
                )
                raise RuntimeError(
                    "无法定位 TAP EFFECT 数字；"
                    f"读回截图={capture_path}"
                )
            actual_tap, tap_row_y = located_tap
            direction, count = _tap_effect_click_plan(
                actual_tap, expected_tap
            )
            for _ in range(count):
                if context.tasker.stopping:
                    return True
                _click(
                    context,
                    (
                        coordinates[f"tap_effect_{direction}"][0],
                        tap_row_y,
                    ),
                )
                changed_tap = True
                time.sleep(delay)
            confirmed_tap_result = _find_tap_effect(
                capture_image(context),
                search_roi=coordinates["tap_effect_search_roi"],
            )
            confirmed_tap = (
                confirmed_tap_result[0]
                if confirmed_tap_result is not None
                else None
            )
            if confirmed_tap != expected_tap:
                raise RuntimeError(
                    f"TAP EFFECT 复核失败：实际 {confirmed_tap}，"
                    f"期望 {expected_tap}"
                )

            print(
                "RealtimeGameEffectSettingsGate "
                f"judgement_assist={actual_assist}->{expected_assist} "
                f"tap_effect={actual_tap}->{confirmed_tap} "
                f"changed_assist={changed_assist} changed_tap={changed_tap}",
                flush=True,
            )
            return True
        finally:
            if settings_open:
                try:
                    _click(context, coordinates["settings_close"])
                    time.sleep(delay)
                except Exception:
                    traceback.print_exc()
            if menu_open:
                try:
                    _click(context, coordinates["menu_close"])
                    time.sleep(delay)
                except Exception:
                    traceback.print_exc()
