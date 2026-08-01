from __future__ import annotations

import json
import time
import traceback
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    from .foreground_guard import ForegroundAppMismatch, foreground_package, require_game_foreground
    from .screen_refresh import ScreenRefreshCancelled, capture_image
    from .task_reporting import log_task
except ImportError:  # AgentServer loads this module from the agent directory.
    from foreground_guard import ForegroundAppMismatch, foreground_package, require_game_foreground
    from screen_refresh import ScreenRefreshCancelled, capture_image
    from task_reporting import log_task


def _params(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        return json.loads(raw)
    return {}


def _wait_unless_stopping(context: Context, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if context.tasker.stopping:
            return False
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    return not context.tasker.stopping


def _package_running(controller: Any, package: str) -> bool | None:
    try:
        output = controller.post_shell(f"pidof {package}", 5000).wait().get()
    except Exception:
        return None
    return bool(str(output or "").strip())



def _prepare_game(
    context: Context,
    package: str,
) -> tuple[bool, bool]:
    """Return (ready, app_was_started_or_focused)."""

    controller = context.tasker.controller
    running = _package_running(controller, package)
    actual = foreground_package(controller)
    if context.tasker.stopping:
        return False, False

    if running is False:
        log_task("游戏启动", "进程", "INFO", "游戏未运行，正在启动")
        controller.post_start_app(package).wait()
        return True, True

    if actual == package:
        log_task(
            "游戏启动",
            "进程",
            "INFO",
            "游戏进程已运行，当前位于游戏前台",
        )
        return True, False

    if running is True:
        log_task(
            "游戏启动",
            "进程",
            "INFO",
            f"游戏已运行但位于后台，正在从 {actual or 'unknown'} 切回游戏",
        )
        controller.post_start_app(package).wait()
        return True, True

    if running is None:
        log_task(
            "游戏启动",
            "进程",
            "WARN",
            "无法查询游戏进程，尝试启动或切回游戏",
        )
        controller.post_start_app(package).wait()
        return True, True

@AgentServer.custom_action("CommonRecover")
class CommonRecover(CustomAction):
    """Recover an unknown page with BACK, then bounded app restarts."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            return self._run(context, argv)
        except ScreenRefreshCancelled:
            return False
        except Exception as exc:
            log_task(
                "游戏启动",
                "异常",
                "ERROR",
                f"主页恢复回调异常：{type(exc).__name__}: {exc}",
            )
            traceback.print_exc()
            return False

    def _run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        params = _params(argv.custom_action_param)
        home_node = str(params.get("home_node", "HomeMarker"))
        interval = int(params.get("escape_interval_ms", 1500)) / 1000
        timeout = int(params.get("escape_timeout_ms", 60000)) / 1000
        package = str(params.get("package", "com.bilibili.star.bili"))
        restart_limit = int(params.get("restart_limit", 2))
        restart_wait = int(params.get("restart_wait_ms", 5000)) / 1000
        startup_grace = int(params.get("startup_grace_ms", 0)) / 1000
        click_nodes = [str(node) for node in params.get("click_nodes", [])]
        back_only = bool(params.get("back_only", False))
        login_start_node = str(params.get("login_start_node", ""))
        login_start_target = params.get("login_start_target")
        login_tap_target = params.get("login_tap_target")
        login_marker_priority_attempts = max(
            1, int(params.get("login_marker_priority_attempts", 3))
        )
        escape_after_login_start = bool(params.get("escape_after_login_start", False))
        login_mode = (
            escape_after_login_start
            and bool(login_start_node)
            and isinstance(login_start_target, (list, tuple))
            and len(login_start_target) == 2
        )
        tap_anywhere_mode = (
            login_mode
            and isinstance(login_tap_target, (list, tuple))
            and len(login_tap_target) == 2
        )
        if login_mode and "startup_grace_ms" not in params:
            startup_grace = 12.0
        controller = context.tasker.controller

        if context.tasker.stopping:
            return True
        ready, app_started = _prepare_game(context, package)
        if not ready:
            if context.tasker.stopping:
                return True
            return False

        for restart in range(restart_limit + 1):
            login_started = not login_mode
            login_seen = False
            login_tap_attempted = False
            login_recovery_active = False
            login_marker_attempts = 0
            iteration_grace = max(startup_grace, 30.0) if app_started else startup_grace
            grace_deadline = time.monotonic() + iteration_grace
            deadline = time.monotonic() + timeout
            escape_count = 0
            while time.monotonic() < deadline:
                if context.tasker.stopping:
                    return True
                image = capture_image(context)
                # run_task() may replace/invalidate the remote Controller proxy.
                controller = context.tasker.controller
                if context.tasker.stopping:
                    return True
                try:
                    require_game_foreground(controller, package)
                except ForegroundAppMismatch as exc:
                    actual = foreground_package(controller) or ""
                    is_system_shell = actual.startswith("com.android.launcher") or actual == "com.android.systemui"
                    if time.monotonic() < grace_deadline or is_system_shell:
                        if not _wait_unless_stopping(context, interval):
                            return True
                        continue
                    print(f"CommonRecover {exc}", flush=True)
                    return False
                result = context.run_recognition(home_node, image)
                if result and result.hit:
                    login_status = "登录完成" if login_seen else "已登录"
                    log_task(
                        "游戏启动",
                        "登录",
                        "SUCCESS",
                        f"已识别主页，状态：{login_status}",
                    )
                    return True
                # Result recovery is ESC-only for the full initial window.
                # If that cannot escape a page whose BACK dialog toggles
                # between open/cancel (for example an unlocked story), a
                # bounded app restart must switch to the normal login state
                # machine instead of continuing to press BACK on the title
                # screen.
                if (back_only and restart == 0) or login_recovery_active:
                    if context.tasker.stopping:
                        return True
                    controller.post_click_key(4).wait()
                    escape_count += 1
                    if not _wait_unless_stopping(context, interval):
                        return True
                    continue
                clicked = False
                if login_mode and not login_started:
                    login_marker_attempts += 1
                    result = context.run_recognition(login_start_node, image)
                    if result and result.hit:
                        if context.tasker.stopping:
                            return True
                        x, y = (int(value) for value in login_start_target)
                        controller.post_click(x, y).wait()
                        login_started = True
                        login_seen = True
                        clicked = True
                        if not tap_anywhere_mode:
                            login_recovery_active = True
                            deadline = time.monotonic() + timeout
                            grace_deadline = time.monotonic()
                        log_task(
                            "游戏启动",
                            "登录",
                            "INFO",
                            f"识别到登录界面，已点击开始位置 ({x}, {y})",
                        )
                    elif (
                        login_marker_attempts
                        < login_marker_priority_attempts
                    ):
                        # The bottom-right menu marker appears later and is
                        # more stable than the animated "tap to start" text.
                        # Give it several fresh frames before allowing the
                        # generic login click nodes to take over.
                        if not _wait_unless_stopping(context, interval):
                            return True
                        continue
                pending_login_tap = (
                    tap_anywhere_mode
                    and login_started
                    and not login_tap_attempted
                )
                for node in ([] if pending_login_tap else click_nodes):
                    if clicked:
                        break
                    result = context.run_recognition(node, image)
                    if not result or not result.hit or not result.box:
                        continue
                    if context.tasker.stopping:
                        return True
                    box = result.box
                    controller.post_click(
                        box.x + box.w // 2,
                        box.y + box.h // 2,
                    ).wait()
                    login_started = True
                    login_seen = True
                    clicked = True
                    login_recovery_active = True
                    deadline = time.monotonic() + timeout
                    grace_deadline = time.monotonic()
                    log_task(
                        "游戏启动",
                        "登录",
                        "INFO",
                        f"处理登录或弹窗节点：{node}",
                    )
                    break
                if (
                    not clicked
                    and tap_anywhere_mode
                    and login_started
                    and not login_tap_attempted
                ):
                    if context.tasker.stopping:
                        return True
                    x, y = (int(value) for value in login_tap_target)
                    controller.post_click(x, y).wait()
                    login_tap_attempted = True
                    login_seen = True
                    clicked = True
                    login_recovery_active = True
                    deadline = time.monotonic() + timeout
                    grace_deadline = time.monotonic()
                    log_task(
                        "游戏启动",
                        "登录",
                        "INFO",
                        f"登录模板未命中，执行安全的“点击屏幕任意处” ({x}, {y})",
                    )
                if (
                    not clicked
                    and (
                        (login_mode and login_started)
                        or time.monotonic() >= grace_deadline
                    )
                ):
                    if context.tasker.stopping:
                        return True
                    controller.post_click_key(4).wait()
                    escape_count += 1
                    log_task(
                        "游戏启动",
                        "主页恢复",
                        "INFO",
                        f"未识别当前游戏页面，发送 ESC 返回（第 {escape_count} 次）",
                    )
                if not _wait_unless_stopping(context, interval):
                    return True
            if restart < restart_limit:
                if context.tasker.stopping:
                    return True
                controller.post_stop_app(package).wait()
                if context.tasker.stopping:
                    return True
                controller.post_start_app(package).wait()
                app_started = True
                log_task(
                    "游戏启动",
                    "重启",
                    "WARN",
                    f"{int(timeout)} 秒内未识别主页，"
                    f"正在重启游戏（第 {restart + 1}/{restart_limit} 次）",
                )
                if not _wait_unless_stopping(context, restart_wait):
                    return True
        log_task(
            "游戏启动",
            "结束",
            "ERROR",
            f"经过初次启动和 {restart_limit} 次重启仍未识别主页；"
            "可能停留在账号、验证码、实名或未收录页面",
        )
        return False
