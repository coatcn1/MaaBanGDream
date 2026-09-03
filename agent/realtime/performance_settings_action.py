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
from .profile_store import EnvironmentSignature, RealtimeProfileStore
from .rehearsal_action import frame_resolution
from .live_session import current_live_run
from .native_prearm import (
    discard_prearmed_backend,
    prepare_native_for_settings_gate,
)
from .run_reporting import (
    PreflightPerformanceSnapshot,
    write_preflight_terminal_result,
)


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
_TYPE_DIGIT_TEMPLATE_PATH = (
    PROJECT_ROOT / "resource" / "image" / "performance_settings" / "type_digits.png"
)
_TYPE_LABEL_DIR = (
    PROJECT_ROOT / "resource" / "image" / "performance_settings" / "type_labels"
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


@lru_cache(maxsize=1)
def _type_digit_templates() -> tuple[np.ndarray, ...]:
    """Return TYPE1..TYPE7 suffix templates captured from the real game UI.

    The TYPE labels use a narrower font than the note-speed display, so the
    shared speed templates misread TYPE5 as 3.  These templates are sampled
    from the 1280x720 演出皮肤设定 page rows.
    """
    sprite = cv2.imread(str(_TYPE_DIGIT_TEMPLATE_PATH), cv2.IMREAD_GRAYSCALE)
    if sprite is None or sprite.shape != (28, 200):
        raise RuntimeError(f"TYPE 数字模板损坏：{_TYPE_DIGIT_TEMPLATE_PATH}")
    return tuple(
        sprite[:, index * 20:(index + 1) * 20] >= 128
        for index in range(10)
    )


@lru_cache(maxsize=1)
def _type_label_templates() -> tuple[tuple[int, np.ndarray], ...]:
    """Return (TYPE value, label template) pairs for TYPE1..TYPE7.

    The whole ``TYPE<n>`` label is matched instead of classifying the narrow
    suffix digit alone, because the digit glyph is unstable at 20x28 after
    nearest-neighbour downsampling (TYPE5 could be read as 3, and TYPE1 can
    pick up serif pixels and read as 3 as well).
    """
    result = []
    for value in range(1, 8):
        path = _TYPE_LABEL_DIR / f"type_label_{value}.png"
        template = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if template is None or template.shape != (34, 95, 3):
            raise RuntimeError(f"TYPE 标签模板损坏：{path}")
        result.append((value, template))
    return tuple(result)


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
        # Imported lazily because the visual gate reuses this module's fixed
        # digit classifier.  At runtime the gate module is already registered.
        from .game_effect_settings_action import verified_game_visual_settings

        visual = verified_game_visual_settings()
        runtime_options = store.runtime_options()
        note_skin_type = (
            visual.note_skin_type
            if visual is not None
            else int(runtime_options["note_skin_type"])
        )
        tap_effect = (
            visual.tap_effect
            if visual is not None
            else int(runtime_options["tap_effect"])
        )
        judgement_assist_effect = (
            visual.judgement_assist_effect
            if visual is not None
            else bool(runtime_options["judgement_assist_effect"])
        )
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
            if bool(params.get("visual_evaluation", False))
            else store.resolve_latest_for_environment
        )
        settings = resolver(
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
) -> float:
    """Select 演出设定 with a visual readback loop.

    A coordinate click is not proof that the remembered settings tab changed:
    the dialog animation can silently drop the first input.  Re-click the tab
    only after the fixed-digit display remains unreadable for a full stable
    read cycle.
    """
    last_error: RuntimeError | None = None
    for _ in range(max(1, attempts)):
        _click(controller, coordinates["first_tab"])
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


def _close_settings_dialog(
    context: Context,
    controller,
    coordinates: dict[str, tuple[int, int]],
    *,
    attempts: int,
    delay_seconds: float,
) -> None:
    """Close the settings dialog and prove that the speed display vanished."""
    for attempt in range(1, max(1, attempts) + 1):
        if context.tasker.stopping:
            return
        _click(controller, coordinates["close"])
        time.sleep(delay_seconds)
        image = controller.post_screencap().wait().get()
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
                # Lazy import avoids the visual gate's dependency on this
                # module's fixed digit classifier.
                from .game_effect_settings_action import (
                    verified_game_visual_settings,
                )

                write_preflight_terminal_result(
                    output_dir=PROJECT_ROOT / "screencap",
                    params=params,
                    terminal_stage="performance_settings_gate",
                    reason=reason,
                    visual_settings=verified_game_visual_settings(),
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
        if context.tasker.stopping:
            return True
        difficulty = str(params.get("difficulty", "Easy"))
        if difficulty not in RealtimeProfileStore.DIFFICULTIES:
            raise ValueError(f"不支持的难度：{difficulty}")
        controller = context.tasker.controller
        before = controller.post_screencap().wait().get()
        expected, profile = _expected_speed(context, params, before)
        _speed_cents(expected)
        if on_expected is not None:
            on_expected(PreflightPerformanceSnapshot(
                expected_note_speed=expected,
                profile=profile,
            ))
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
            read_current = lambda: _read_speed(
                controller.post_screencap().wait().get(),
                coordinates["speed_roi"],
            )
            actual_before = _select_first_tab_and_read(
                controller,
                coordinates,
                read_current,
                attempts=int(params.get("first_tab_attempts", 3)),
                settle_delay_seconds=float(
                    params.get("first_tab_delay_seconds", 0.3)
                ),
            )
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
        finally:
            if opened:
                try:
                    if verified_successfully:
                        _close_settings_dialog(
                            context,
                            controller,
                            coordinates,
                            attempts=int(params.get("close_attempts", 3)),
                            delay_seconds=float(
                                params.get("close_delay_seconds", 0.5)
                            ),
                        )
                    else:
                        _click(controller, coordinates["close"])
                        time.sleep(
                            float(params.get("close_delay_seconds", 0.5))
                        )
                except Exception:
                    traceback.print_exc()
                    if verified_successfully:
                        raise
        if context.tasker.stopping:
            return True
        if bool(params.get("defer_native_prearm", False)):
            discard_prearmed_backend("deferred-until-final-cover")
            print(
                "RealtimePerformanceSettingsGate native_prearm=deferred "
                "reason=wait-final-cover",
                flush=True,
            )
        else:
            prepare_native_for_settings_gate(
                controller=controller,
                live_run=current_live_run(),
                difficulty=difficulty,
                project_root=PROJECT_ROOT,
                ready_timeout_s=float(
                    params.get(
                        "native_ready_timeout_seconds",
                        10.0,
                    )
                ),
                ttl_s=float(params.get("native_prearm_ttl_seconds", 30.0)),
            )
        if context.tasker.stopping:
            discard_prearmed_backend("user-stopped-after-prearm")
        return True
