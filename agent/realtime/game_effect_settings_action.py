from __future__ import annotations

import json
import math
import time
import traceback

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

from .profile_action import PROJECT_ROOT
from .profile_store import RealtimeProfileStore


# 主页菜单和“选项”入口使用 1280×720 Bilibili 客户端的基准坐标。
DEFAULT_COORDINATES = {
    "home_menu": (1225, 55),
    "options": (755, 301),
    "settings_close": (640, 600),
    "menu_close": (640, 566),
}


class _StopRequested(RuntimeError):
    pass


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


def _capture(context: Context):
    _check_stopping(context)
    image = capture_image(context)
    _check_stopping(context)
    return image


def _click(context: Context, point: tuple[int, int]) -> None:
    _check_stopping(context)
    require_game_foreground(context.tasker.controller)
    detail = context.run_task(
        "RealtimeSettingsClick",
        {
            "RealtimeSettingsClick": {
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
    """通过 Maa 主进程发送滑动，供协力导航复用。"""
    _check_stopping(context)
    require_game_foreground(context.tasker.controller)
    segments = max(
        1,
        math.ceil(
            max(abs(end[0] - start[0]), abs(end[1] - start[1])) / 50
        ),
    )
    detail = context.run_task(
        "RealtimeSettingsSwipe",
        {
            "RealtimeSettingsSwipe": {
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


def _run_speed_settings_from_home(
    context: Context,
    *,
    params: dict,
) -> bool:
    """从主页进入“选项”，只读取、修正并复核音符流速。"""
    from .performance_settings_action import (
        DEFAULT_COORDINATES as PERFORMANCE_COORDINATES,
        _adjust_speed,
        _close_settings_dialog,
        _expected_speed,
        _read_speed,
        _select_first_tab_and_read,
        _speed_cents,
        _speed_settings_target,
        activate_speed_settings_target,
        publish_verified_performance_settings,
    )

    difficulty = str(params.get("difficulty", "Easy"))
    coordinates = dict(DEFAULT_COORDINATES)
    coordinates.update(params.get("coordinates", {}))
    coordinates = {
        key: tuple(int(value) for value in point)
        for key, point in coordinates.items()
    }
    performance_coordinates = dict(PERFORMANCE_COORDINATES)
    performance_coordinates.update(params.get("performance_coordinates", {}))
    performance_coordinates = {
        key: tuple(int(value) for value in point)
        for key, point in performance_coordinates.items()
    }
    delay = float(params.get("delay_seconds", 0.35))
    page_delay = float(params.get("page_delay_seconds", 0.7))
    menu_open = False
    settings_open = False
    actual_before: float | None = None
    confirmed: float | None = None
    expected_speed: float | None = None
    profile: str | None = None
    target: dict | None = None
    try:
        # 先沿用已验证的主页点击顺序；截图任务会刷新 RemoteController，
        # 因此不得在第一次点击前持有旧 Controller 再做设备查询。
        print(
            "RealtimeGameSpeedSettingsGate stage=home action=open-menu",
            flush=True,
        )
        _click(context, coordinates["home_menu"])
        menu_open = True
        _wait(context, page_delay)
        print(
            "RealtimeGameSpeedSettingsGate stage=home action=open-options",
            flush=True,
        )
        _click(context, coordinates["options"])
        settings_open = True
        _wait(context, page_delay)

        before = _capture(context)
        expected_speed, profile = _expected_speed(context, params, before)
        _speed_cents(expected_speed)
        target = _speed_settings_target(note_speed=expected_speed)
        read_current = lambda: _read_speed(
            _capture(context),
            performance_coordinates["speed_roi"],
        )
        actual_before = _select_first_tab_and_read(
            None,
            performance_coordinates,
            read_current,
            attempts=int(params.get("first_tab_attempts", 3)),
            settle_delay_seconds=float(
                params.get("first_tab_delay_seconds", 0.3)
            ),
            click_point=lambda point: _click(context, point),
        )
        completed, confirmed = _adjust_speed(
            context,
            None,
            performance_coordinates,
            actual_before,
            expected_speed,
            button_delay_seconds=float(
                params.get("button_delay_seconds", 0.15)
            ),
            settle_delay_seconds=float(
                params.get("adjust_delay_seconds", 0.35)
            ),
            round_limit=int(params.get("adjust_round_limit", 6)),
            read_current=read_current,
            click_point=lambda point: _click(context, point),
        )
        if not completed or confirmed is None:
            return True
        _close_settings_dialog(
            context,
            None,
            performance_coordinates,
            attempts=int(params.get("close_attempts", 3)),
            delay_seconds=float(params.get("close_delay_seconds", 0.5)),
            click_point=lambda point: _click(context, point),
            capture_current=lambda: _capture(context),
        )
        settings_open = False
        _click(context, coordinates["menu_close"])
        _wait(context, delay)
        menu_open = False
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

    if (
        actual_before is None
        or expected_speed is None
        or target is None
        or confirmed is None
    ):
        raise RuntimeError("主页流速设置流程未产生完整读回结果")
    publish_verified_performance_settings(
        difficulty=difficulty,
        actual_note_speed=confirmed,
        expected_note_speed=expected_speed,
        profile=profile,
    )
    activate_speed_settings_target(target)
    print(
        "RealtimeGameSpeedSettingsGate stage=home "
        f"difficulty={difficulty} speed={actual_before:.2f}->{confirmed:.2f} "
        f"expected={expected_speed:.2f} verification=current-task",
        flush=True,
    )
    return True


@AgentServer.custom_action("RealtimeGameSpeedSettingsGate")
class RealtimeGameSpeedSettingsGate(CustomAction):
    """按用户开关从主页读取、修正并复核音符流速。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        from .performance_settings_action import (
            clear_active_speed_settings_target,
            clear_verified_settings,
        )

        clear_verified_settings()
        clear_active_speed_settings_target()
        max_attempts = 3
        last_error: Exception | None = None
        try:
            decoded = json.loads(argv.custom_action_param or "{}")
            params = decoded if isinstance(decoded, dict) else {}
            max_attempts = max(1, min(3, int(params.get("max_attempts", 3))))
        except _StopRequested:
            print(
                "RealtimeGameSpeedSettingsGate stopped=true verified=false",
                flush=True,
            )
            return True
        except Exception:
            params = {}
        for attempt in range(1, max_attempts + 1):
            try:
                return self._run(context, dict(params))
            except _StopRequested:
                print(
                    "RealtimeGameSpeedSettingsGate stopped=true verified=false",
                    flush=True,
                )
                return True
            except RuntimeError as exc:
                last_error = exc
                if attempt >= max_attempts:
                    break
                print(
                    "RealtimeGameSpeedSettingsGate "
                    f"retry={attempt}/{max_attempts} "
                    f"failed={type(exc).__name__}: {exc}",
                    flush=True,
                )
                _wait(context, 1.0)
            except Exception as exc:
                last_error = exc
                break
        if last_error is not None:
            record_failure_reason(
                f"游戏流速设置失败："
                f"{type(last_error).__name__}: {last_error}"
            )
            print(
                "RealtimeGameSpeedSettingsGate "
                f"failed={type(last_error).__name__}: {last_error}",
                flush=True,
            )
        return False

    def _run(self, context: Context, params: dict) -> bool:
        if context.tasker.stopping:
            return True
        options = RealtimeProfileStore(
            PROJECT_ROOT / "profiles"
        ).runtime_options()
        if not bool(options["note_speed_settings_enabled"]):
            print(
                "RealtimeGameSpeedSettingsGate enabled=false skipped=true",
                flush=True,
            )
            return True
        entry_mode = str(params.get("entry_mode", "home"))
        if entry_mode == "home":
            return _run_speed_settings_from_home(context, params=params)
        raise ValueError(
            "流速设置只允许从主页检查，"
            f"不支持 entry_mode={entry_mode}"
        )
