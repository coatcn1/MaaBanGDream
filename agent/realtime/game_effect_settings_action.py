from __future__ import annotations

import json
import math
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass

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

from .performance_settings_action import (
    _classify_digit,
    _digit_templates,
    _normalise_glyph,
)
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
    "note_skin_scroll_start": (844, 500),
    "note_skin_scroll_end": (844, 400),
    "note_skin_radio_x": (205,),
    # TYPE labels stay in this strip while the skin page scrolls.  The last
    # glyph is the 1..7 digit; the selected radio is a compact magenta fill.
    "note_skin_digit_search_roi": (220, 180, 90, 390),
    "assist_enabled_x": (202,),
    "assist_disabled_x": (302,),
    "tap_effect_left": (206, 472),
    "tap_effect_right": (573, 472),
    # Unity's ScrollRect can settle at different vertical offsets even for
    # identical swipes. Search the narrow centre column for the TAP EFFECT
    # digit instead of assuming a fixed y coordinate.
    "tap_effect_search_roi": (360, 180, 100, 360),
}


@dataclass(frozen=True)
class VerifiedGameVisualSettings:
    note_skin_type: int
    tap_effect: int
    judgement_assist_effect: bool
    verified_at: float


@dataclass(frozen=True)
class NoteSkinRow:
    value: int
    row_y: int
    selected: bool


class _StopRequested(RuntimeError):
    pass


_VERIFIED_GAME_VISUAL_SETTINGS: VerifiedGameVisualSettings | None = None


def clear_verified_game_visual_settings() -> None:
    global _VERIFIED_GAME_VISUAL_SETTINGS
    _VERIFIED_GAME_VISUAL_SETTINGS = None


def verified_game_visual_settings(
    *,
    max_age_seconds: float | None = None,
    clock=time.monotonic,
) -> VerifiedGameVisualSettings | None:
    value = _VERIFIED_GAME_VISUAL_SETTINGS
    if value is None:
        return None
    if (
        max_age_seconds is not None
        and clock() - value.verified_at > max_age_seconds
    ):
        return None
    return value


def _publish_verified_game_visual_settings(
    *,
    note_skin_type: int,
    tap_effect: int,
    judgement_assist_effect: bool,
    clock=time.monotonic,
) -> VerifiedGameVisualSettings:
    global _VERIFIED_GAME_VISUAL_SETTINGS
    value = VerifiedGameVisualSettings(
        note_skin_type=note_skin_type,
        tap_effect=tap_effect,
        judgement_assist_effect=judgement_assist_effect,
        verified_at=clock(),
    )
    _VERIFIED_GAME_VISUAL_SETTINGS = value
    return value


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


def _classify_note_skin_digit(mask: np.ndarray) -> int:
    """Classify the 1..7 suffix in a TYPE label using fixed digit templates.

    TYPE labels use a slightly narrower font than the note-speed display, so
    the match threshold is deliberately wider.  Geometry and the four
    preceding TYPE glyphs are checked separately by ``_find_note_skin_rows``;
    this is not free-form OCR.
    """
    glyph = _normalise_glyph(mask)
    scores = np.asarray([
        np.mean(glyph != template)
        for template in _digit_templates()
    ])
    order = np.argsort(scores)
    best = int(order[0])
    margin = float(scores[order[1]] - scores[order[0]])
    if best not in range(1, 8) or float(scores[best]) > 0.43 or margin < 0.008:
        raise RuntimeError(
            "TYPE digit template is uncertain: "
            f"candidate={best} difference={scores[best]:.3f} margin={margin:.3f}"
        )
    return best


def _find_note_skin_rows(
    image: np.ndarray,
    *,
    search_roi: tuple[int, int, int, int],
    radio_x: int,
    classify: Callable[[np.ndarray], int] = _classify_note_skin_digit,
) -> list[NoteSkinRow]:
    """Find visible TYPE1..TYPE7 rows and their selected magenta radio."""
    x, y, width, height = search_roi
    display = image[y:y + height, x:x + width]
    if display.shape[:2] != (height, width):
        raise RuntimeError(f"TYPE search area is outside the frame: {search_roi}")
    gray = cv2.cvtColor(display, cv2.COLOR_BGR2GRAY)
    mask = (gray < 180).astype(np.uint8)
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    components = [tuple(int(value) for value in stat) for stat in stats[1:count]]
    rows: list[NoteSkinRow] = []
    for component_x, component_y, component_w, component_h, area in components:
        # On the 1280x720 dialog the TYPE suffix digit occupies x=282..300;
        # keep a small tolerance for anti-aliasing and settled scroll offsets.
        if not (
            62 <= component_x <= 82
            and 3 <= component_w <= 15
            and 12 <= component_h <= 20
            and 25 <= area <= 130
        ):
            continue
        row_y = y + component_y + component_h // 2
        prefix_glyphs = 0
        for other_x, other_y, other_w, other_h, other_area in components:
            other_center_y = y + other_y + other_h // 2
            if (
                5 <= other_x < 62
                and 3 <= other_w <= 18
                and 8 <= other_h <= 22
                and 20 <= other_area <= 150
                and abs(other_center_y - row_y) <= 5
            ):
                prefix_glyphs += 1
        if prefix_glyphs < 4:
            continue
        pad = 3
        left = max(0, component_x - pad)
        top = max(0, component_y - pad)
        right = min(width, component_x + component_w + pad)
        bottom = min(height, component_y + component_h + pad)
        try:
            value = int(classify(mask[top:bottom, left:right]))
        except RuntimeError:
            continue
        if not 1 <= value <= 7:
            continue
        rows.append(NoteSkinRow(
            value=value,
            row_y=row_y,
            selected=_pink_pixels(image, (radio_x, row_y)) >= 80,
        ))
    rows.sort(key=lambda row: row.row_y)
    if len({row.value for row in rows}) != len(rows):
        raise RuntimeError("duplicate TYPE rows were detected in one frame")
    return rows


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


def _check_stopping(context: Context) -> None:
    if context.tasker.stopping:
        raise _StopRequested()


def _wait(context: Context, seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        _check_stopping(context)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.05, remaining))


def _capture(context: Context) -> np.ndarray:
    _check_stopping(context)
    image = capture_image(context)
    _check_stopping(context)
    return image


def _click(context: Context, point: tuple[int, int]) -> None:
    _check_stopping(context)
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
    _check_stopping(context)


def _swipe(
    context: Context,
    start: tuple[int, int],
    end: tuple[int, int],
    duration_ms: int,
) -> None:
    _check_stopping(context)
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
    _check_stopping(context)


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
            _wait(context, delay_seconds)


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
            _wait(context, delay_seconds)


def _visible_note_skin_rows(
    context: Context,
    coordinates: dict[str, tuple[int, ...]],
) -> tuple[np.ndarray, list[NoteSkinRow]]:
    image = _capture(context)
    rows = _find_note_skin_rows(
        image,
        search_roi=coordinates["note_skin_digit_search_roi"],
        radio_x=coordinates["note_skin_radio_x"][0],
    )
    return image, rows


def _find_note_skin_on_page(
    context: Context,
    coordinates: dict[str, tuple[int, ...]],
    *,
    target: int | None,
    reset_steps: int,
    max_scroll_steps: int,
    duration_ms: int,
    delay_seconds: float,
) -> tuple[int | None, int | None, np.ndarray]:
    """Read the selected TYPE and optionally locate a target row.

    The page is first reset to the top.  Scrolling advances by roughly one
    row and is bounded, so TYPE3..TYPE7 can be reached without depending on a
    remembered Unity ScrollRect offset.
    """
    _scroll_to_top(
        context,
        coordinates,
        steps=reset_steps,
        duration_ms=duration_ms,
        delay_seconds=delay_seconds,
    )
    selected_values: set[int] = set()
    target_y: int | None = None
    last_image: np.ndarray | None = None
    for step in range(max_scroll_steps + 1):
        last_image, rows = _visible_note_skin_rows(context, coordinates)
        selected_values.update(row.value for row in rows if row.selected)
        target_y = None
        if target is not None:
            match = next((row for row in rows if row.value == target), None)
            if match is not None:
                target_y = match.row_y
        if len(selected_values) > 1:
            raise RuntimeError(
                f"multiple selected TYPE rows detected: {sorted(selected_values)}"
            )
        if (target is None and selected_values) or (
            target is not None and target_y is not None
        ):
            break
        if step < max_scroll_steps:
            _swipe(
                context,
                coordinates["note_skin_scroll_start"],
                coordinates["note_skin_scroll_end"],
                duration_ms,
            )
            if delay_seconds:
                _wait(context, delay_seconds)
    if last_image is None:
        raise RuntimeError("TYPE page was not captured")
    if target is None and len(selected_values) != 1:
        raise RuntimeError("unable to read the selected TYPE1..TYPE7 radio")
    selected = next(iter(selected_values)) if selected_values else None
    return selected, target_y, last_image


@AgentServer.custom_action("RealtimeGameEffectSettingsGate")
class RealtimeGameEffectSettingsGate(CustomAction):
    """Apply and verify detector-friendly game effect settings from Home."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        clear_verified_game_visual_settings()
        try:
            decoded = json.loads(argv.custom_action_param or "{}")
            return self._run(
                context,
                decoded if isinstance(decoded, dict) else {},
            )
        except _StopRequested:
            print(
                "RealtimeGameEffectSettingsGate stopped=true verified=false",
                flush=True,
            )
            return True
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
        apply_changes = bool(options["game_effect_settings_enabled"])
        if not apply_changes:
            print(
                "RealtimeGameEffectSettingsGate enabled=false readback_only=true",
                flush=True,
            )

        expected_assist = bool(options["judgement_assist_effect"])
        expected_note_skin = int(options["note_skin_type"])
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
        changed_note_skin = False
        changed_tap = False
        try:
            _click(context, coordinates["home_menu"])
            menu_open = True
            _wait(context, page_delay)
            _click(context, coordinates["options"])
            settings_open = True
            _wait(context, page_delay)

            _click(context, coordinates["performance_tab"])
            _wait(context, delay)
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
            _wait(context, delay)
            image = _capture(context)
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
            if apply_changes and actual_assist != expected_assist:
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
                _wait(context, delay)
                confirmed_image = _capture(context)
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
            _wait(context, delay)

            reset_steps = int(params.get("scroll_reset_steps", 5))
            note_skin_search_steps = int(
                params.get("note_skin_search_steps", 16)
            )
            actual_note_skin, _, note_skin_image = _find_note_skin_on_page(
                context,
                coordinates,
                target=None,
                reset_steps=reset_steps,
                max_scroll_steps=note_skin_search_steps,
                duration_ms=swipe_duration,
                delay_seconds=swipe_delay,
            )
            if actual_note_skin is None:
                capture_path = _save_readback_failure(
                    note_skin_image, "note-skin"
                )
                raise RuntimeError(
                    "unable to read selected TYPE1..TYPE7; "
                    f"readback={capture_path}"
                )
            if apply_changes and actual_note_skin != expected_note_skin:
                _, target_y, target_image = _find_note_skin_on_page(
                    context,
                    coordinates,
                    target=expected_note_skin,
                    reset_steps=reset_steps,
                    max_scroll_steps=note_skin_search_steps,
                    duration_ms=swipe_duration,
                    delay_seconds=swipe_delay,
                )
                if target_y is None:
                    capture_path = _save_readback_failure(
                        target_image, "note-skin-target"
                    )
                    raise RuntimeError(
                        f"unable to locate TYPE{expected_note_skin}; "
                        f"readback={capture_path}"
                    )
                _click(
                    context,
                    (coordinates["note_skin_radio_x"][0], target_y),
                )
                changed_note_skin = True
                _wait(context, delay)
                confirmed_note_image, confirmed_note_rows = (
                    _visible_note_skin_rows(context, coordinates)
                )
                selected_note_values = [
                    row.value for row in confirmed_note_rows if row.selected
                ]
                if selected_note_values != [expected_note_skin]:
                    capture_path = _save_readback_failure(
                        confirmed_note_image, "note-skin-confirmed"
                    )
                    raise RuntimeError(
                        f"TYPE readback mismatch: actual={selected_note_values} "
                        f"expected={expected_note_skin}; readback={capture_path}"
                    )

            _scroll_to_top(
                context,
                coordinates,
                steps=reset_steps,
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
            _wait(context, delay)
            tap_image = _capture(context)
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
            for _ in range(count if apply_changes else 0):
                _check_stopping(context)
                _click(
                    context,
                    (
                        coordinates[f"tap_effect_{direction}"][0],
                        tap_row_y,
                    ),
                )
                changed_tap = True
                _wait(context, delay)
            confirmed_tap_result = _find_tap_effect(
                _capture(context),
                search_roi=coordinates["tap_effect_search_roi"],
            )
            confirmed_tap = (
                confirmed_tap_result[0]
                if confirmed_tap_result is not None
                else None
            )
            required_tap = expected_tap if apply_changes else actual_tap
            if confirmed_tap != required_tap:
                raise RuntimeError(
                    f"TAP EFFECT 复核失败：实际 {confirmed_tap}，"
                    f"期望 {required_tap}"
                )

            final_assist = expected_assist if apply_changes else actual_assist
            final_note_skin = (
                expected_note_skin if apply_changes else actual_note_skin
            )
            _publish_verified_game_visual_settings(
                note_skin_type=final_note_skin,
                tap_effect=confirmed_tap,
                judgement_assist_effect=final_assist,
            )

            print(
                "RealtimeGameEffectSettingsGate "
                f"enabled={apply_changes} "
                f"judgement_assist={actual_assist}->{expected_assist} "
                f"note_skin_type={actual_note_skin}->{final_note_skin} "
                f"tap_effect={actual_tap}->{confirmed_tap} "
                f"changed_assist={changed_assist} "
                f"changed_note_skin={changed_note_skin} "
                f"changed_tap={changed_tap}",
                flush=True,
            )
            return True
        finally:
            if settings_open:
                try:
                    _click(context, coordinates["settings_close"])
                    _wait(context, delay)
                except _StopRequested:
                    pass
                except Exception:
                    traceback.print_exc()
            if menu_open:
                try:
                    _click(context, coordinates["menu_close"])
                    _wait(context, delay)
                except _StopRequested:
                    pass
                except Exception:
                    traceback.print_exc()
