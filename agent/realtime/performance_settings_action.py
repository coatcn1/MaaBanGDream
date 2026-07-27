from __future__ import annotations

import json
import math
import time
import traceback
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
from .profile_store import EnvironmentSignature, RealtimeProfileStore
from .rehearsal_action import frame_resolution


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
    sprite = cv2.imread(str(_DIGIT_TEMPLATE_PATH), cv2.IMREAD_GRAYSCALE)
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
        signature = EnvironmentSignature(
            frame_resolution(image),
            int(params.get("dpi", 240)),
            int(params.get("game_fps", 60)),
            str(params.get("render_quality", "standard")),
            1.0,
        )
        settings = store.resolve_latest_for_environment(
            difficulty=difficulty,
            current_signature=signature,
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
) -> tuple[bool, float | None]:
    """Click in a closed read-click-reread loop until the display matches.

    The game silently drops clicks that arrive too fast, so a one-shot plan
    can land anywhere. Each round re-plans from the freshly read value, which
    makes dropped clicks self-correcting. Blind re-clicking on an unreadable
    display is forbidden: a persistent read failure blocks the run instead.
    """
    reading = actual
    for round_index in range(round_limit):
        plan = _speed_click_plan(reading, expected)
        if not plan:
            return True, reading
        for coordinate_name, count in plan:
            for _ in range(count):
                if context.tasker.stopping:
                    return False, None
                _click(controller, coordinates[coordinate_name])
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


@AgentServer.custom_action("RealtimePerformanceSettingsGate")
class RealtimePerformanceSettingsGate(CustomAction):
    """Read and adjust note speed on the explicitly selected first settings tab."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            return self._run(context, json.loads(argv.custom_action_param or "{}"))
        except Exception as exc:
            record_failure_reason(f"开演前流速设置失败：{type(exc).__name__}: {exc}")
            traceback.print_exc()
            print(
                "RealtimePerformanceSettingsGate "
                f"failed={type(exc).__name__}: {exc}",
                flush=True,
            )
            return False

    def _run(self, context: Context, params: dict) -> bool:
        if context.tasker.stopping:
            return True
        difficulty = str(params.get("difficulty", "Easy"))
        if difficulty not in RealtimeProfileStore.DIFFICULTIES:
            raise ValueError(f"不支持的难度：{difficulty}")
        controller = context.tasker.controller
        before = controller.post_screencap().wait().get()
        expected, profile = _expected_speed(context, params, before)
        _speed_cents(expected)
        coordinates = dict(DEFAULT_COORDINATES)
        coordinates.update(params.get("coordinates", {}))
        coordinates = {
            key: tuple(int(value) for value in point)
            for key, point in coordinates.items()
        }
        opened = False
        verified_successfully = False
        try:
            _click(controller, coordinates["gear"])
            opened = True
            time.sleep(float(params.get("open_delay_seconds", 0.6)))
            # The game reopens the settings dialog on the last-used tab, so
            # the speed display is not guaranteed to be visible. Land on the
            # first "演出设定" tab explicitly before every read/adjust loop.
            _click(controller, coordinates["first_tab"])
            time.sleep(float(params.get("first_tab_delay_seconds", 0.3)))
            read_current = lambda: _read_speed(
                controller.post_screencap().wait().get(),
                coordinates["speed_roi"],
            )
            actual_before = _read_speed_stable(read_current)
            completed, confirmed = _adjust_speed(
                context,
                controller,
                coordinates,
                actual_before,
                expected,
                button_delay_seconds=float(params.get("button_delay_seconds", 0.15)),
                settle_delay_seconds=float(params.get("adjust_delay_seconds", 0.35)),
                round_limit=int(params.get("adjust_round_limit", 6)),
                read_current=read_current,
            )
            if not completed:
                return True
            _VERIFIED[difficulty] = VerifiedPerformanceSettings(
                difficulty=difficulty,
                actual_note_speed=confirmed,
                expected_note_speed=expected,
                profile=profile,
                verified_at=time.monotonic(),
            )
            print(
                "RealtimePerformanceSettingsGate "
                f"difficulty={difficulty} before={actual_before:.2f} "
                f"actual={confirmed:.2f} expected={expected:.2f} "
                f"method=fixed-digit-template "
                f"profile={profile or 'calibration-setting'}",
                flush=True,
            )
            verified_successfully = True
            return True
        finally:
            if opened:
                try:
                    _click(controller, coordinates["close"])
                    time.sleep(float(params.get("close_delay_seconds", 0.35)))
                except Exception:
                    traceback.print_exc()
                    if verified_successfully:
                        raise
