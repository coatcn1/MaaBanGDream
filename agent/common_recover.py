from __future__ import annotations

import json
import subprocess
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


def _wait_for_adb(adb_path: str, serial: str, timeout: float = 90.0) -> bool:
    """Wait until ``serial`` is back online after an emulator reboot."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            output = subprocess.run(
                [adb_path, "-s", serial, "get-state"],
                capture_output=True,
                text=True,
                timeout=8,
            )
            if output.returncode == 0 and output.stdout.strip() == "device":
                return True
        except Exception:
            pass
        time.sleep(2.0)
    return False


def _reboot_ldplayer(
    console_path: str,
    index: int,
    adb_path: str,
    serial: str,
) -> bool:
    """Reboot the LDPlayer emulator instance via its console."""
    try:
        result = subprocess.run(
            [console_path, "reboot", "--index", str(index)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:
        log_task(
            "游戏启动",
            "模拟器",
            "ERROR",
            f"模拟器重启命令失败：{type(exc).__name__}: {exc}",
        )
        return False
    log_task(
        "游戏启动",
        "模拟器",
        "INFO",
        f"模拟器重启命令已发送：rc={result.returncode} {result.stdout.strip()}",
    )
    return _wait_for_adb(adb_path, serial)


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
        reboot_emulator_on_failure = bool(
            params.get("reboot_emulator_on_failure", False)
        )
        emulator_console = str(
            params.get(
                "emulator_console",
                "E:/leidian/mrfz/ldconsole.exe",
            )
        )
        emulator_index = int(params.get("emulator_index", 1000))
        adb_path = str(params.get("adb_path", "E:/leidian/mrfz/adb.exe"))
        adb_serial = str(params.get("adb_serial", "emulator-7554"))
        startup_grace = int(params.get("startup_grace_ms", 0)) / 1000
        click_nodes = [str(node) for node in params.get("click_nodes", [])]
        resource_download_click_node = str(
            params.get("resource_download_click_node", "ResourceDownloadConfirm")
        )
        configured_download_page_nodes = params.get(
            "resource_download_page_nodes"
        )
        if configured_download_page_nodes is None:
            configured_download_page_nodes = [
                params.get(
                    "resource_download_page_node",
                    "ResourceDownloadPageMarker",
                ),
                "ResourceDownloadProgressMarker",
            ]
        resource_download_page_nodes = [
            str(node)
            for node in configured_download_page_nodes
            if str(node).strip()
        ]
        resource_download_timeout = max(
            timeout,
            int(params.get("resource_download_timeout_ms", 1_200_000)) / 1000,
        )
        modal_cancel_nodes = [
            str(node) for node in params.get("modal_cancel_nodes", [])
        ]
        live_failed_continue_node = str(
            params.get("live_failed_continue_node", "LiveFailedContinue")
        )
        live_failed_exit_node = str(
            params.get("live_failed_exit_node", "LiveFailedExit")
        )
        quit_confirm_exit_node = str(
            params.get("quit_confirm_exit_node", "QuitConfirmExit")
        )
        back_only = bool(params.get("back_only", False))
        back_only_click_nodes = [
            str(node) for node in params.get("back_only_click_nodes", [])
        ]
        back_acceleration_click_point = params.get(
            "back_acceleration_click_point"
        )
        if not (
            isinstance(back_acceleration_click_point, (list, tuple))
            and len(back_acceleration_click_point) == 2
        ):
            back_acceleration_click_point = None
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

        emulator_rebooted = False
        restart = 0
        while restart <= restart_limit + (1 if emulator_rebooted else 0):
            restart_round = restart
            restart += 1
            login_started = not login_mode
            login_seen = False
            login_tap_attempted = False
            login_recovery_active = False
            login_marker_attempts = 0
            resource_download_clicked = False
            resource_download_visible = False
            resource_download_deadline: float | None = None
            exiting_failed_live = False
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
                # 生命归零的“演出失败”弹窗必须先点“退出”再确认退出，否则
                # ESC 会在弹窗与退出确认框之间来回切换，形成死循环。
                if not exiting_failed_live:
                    failed_confirm = context.run_recognition(
                        live_failed_continue_node, image
                    )
                    if failed_confirm and failed_confirm.hit:
                        failed_exit = context.run_recognition(
                            live_failed_exit_node, image
                        )
                        if failed_exit and failed_exit.hit and failed_exit.box:
                            if context.tasker.stopping:
                                return True
                            box = failed_exit.box
                            controller.post_click(
                                box.x + box.w // 2,
                                box.y + box.h // 2,
                            ).wait()
                            exiting_failed_live = True
                            log_task(
                                "游戏启动",
                                "演出失败",
                                "INFO",
                                "检测到演出失败弹窗，已点击退出",
                            )
                            if not _wait_unless_stopping(context, interval):
                                return True
                            continue
                if exiting_failed_live:
                    quit_exit = context.run_recognition(
                        quit_confirm_exit_node, image
                    )
                    if quit_exit and quit_exit.hit and quit_exit.box:
                        if context.tasker.stopping:
                            return True
                        box = quit_exit.box
                        controller.post_click(
                            box.x + box.w // 2,
                            box.y + box.h // 2,
                        ).wait()
                        exiting_failed_live = False
                        log_task(
                            "游戏启动",
                            "演出失败",
                            "INFO",
                            "已确认退出失败演出，正在返回主页",
                        )
                        if not _wait_unless_stopping(context, interval):
                            return True
                        continue
                modal_dismissed = False
                if not exiting_failed_live:
                    for node in modal_cancel_nodes:
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
                        modal_dismissed = True
                        log_task(
                            "游戏启动",
                            "弹窗",
                            "INFO",
                            f"检测到模态弹窗，已点击取消：{node}",
                        )
                        break
                if modal_dismissed:
                    if not _wait_unless_stopping(context, interval):
                        return True
                    continue
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

                # Resource updates are a recognised login phase, not an
                # unknown page. Click Download once, then keep the recovery
                # loop passive while the stable page title remains visible.
                # This must run before the ESC-only login recovery branch or
                # BACK can cancel/interfere with an in-progress download.
                if resource_download_click_node and not resource_download_clicked:
                    result = context.run_recognition(
                        resource_download_click_node,
                        image,
                    )
                    if result and result.hit and result.box:
                        if context.tasker.stopping:
                            return True
                        box = result.box
                        controller.post_click(
                            box.x + box.w // 2,
                            box.y + box.h // 2,
                        ).wait()
                        now = time.monotonic()
                        resource_download_clicked = True
                        resource_download_visible = True
                        resource_download_deadline = now + resource_download_timeout
                        login_started = True
                        login_seen = True
                        login_recovery_active = True
                        deadline = now + timeout
                        log_task(
                            "游戏启动",
                            "资源下载",
                            "INFO",
                            "识别到数据下载页面，已点击“下载”并等待完成",
                        )
                        if not _wait_unless_stopping(context, interval):
                            return True
                        continue

                download_page_visible = False
                for resource_download_page_node in resource_download_page_nodes:
                    result = context.run_recognition(
                        resource_download_page_node,
                        image,
                    )
                    if result and result.hit:
                        download_page_visible = True
                        break
                if download_page_visible:
                    now = time.monotonic()
                    if resource_download_deadline is None:
                        resource_download_deadline = now + resource_download_timeout
                    if now >= resource_download_deadline:
                        log_task(
                            "游戏启动",
                            "资源下载",
                            "ERROR",
                            "数据下载页面持续超过 20 分钟，停止自动等待",
                        )
                        return False
                    resource_download_visible = True
                    login_started = True
                    login_seen = True
                    login_recovery_active = True
                    # Keep the ordinary 60-second recovery deadline alive,
                    # while the independent 20-minute bound remains fixed.
                    deadline = min(
                        resource_download_deadline,
                        now + timeout,
                    )
                    if not _wait_unless_stopping(context, interval):
                        return True
                    continue
                if resource_download_visible:
                    # The known download page disappeared. Re-enter the
                    # normal title/login state machine instead of carrying
                    # ESC-only recovery state across the transition.
                    resource_download_visible = False
                    login_recovery_active = False
                    login_started = not login_mode
                    login_marker_attempts = 0
                    now = time.monotonic()
                    deadline = now + timeout
                    grace_deadline = now + startup_grace

                if back_only and restart_round == 0:
                    safe_story_clicked = False
                    for node in back_only_click_nodes:
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
                        safe_story_clicked = True
                        log_task(
                            "娓告垙鍚姩",
                            "涓婚〉鎭㈠",
                            "INFO",
                            f"璇嗗埆骞跺鐞嗗畨鍏ㄥ墽鎯呰妭鐐癸細{node}",
                        )
                        break
                    if safe_story_clicked:
                        if not _wait_unless_stopping(context, interval):
                            return True
                        continue
                # Result recovery is ESC-only for the full initial window.
                # If that cannot escape a page whose BACK dialog toggles
                # between open/cancel (for example an unlocked story), a
                # bounded app restart must switch to the normal login state
                # machine instead of continuing to press BACK on the title
                # screen.
                if (back_only and restart_round == 0) or login_recovery_active:
                    if context.tasker.stopping:
                        return True
                    accelerate_back = (
                        back_only
                        and restart_round == 0
                        and back_acceleration_click_point is not None
                    )
                    if accelerate_back:
                        x, y = (
                            int(value)
                            for value in back_acceleration_click_point
                        )
                        controller.post_click(x, y).wait()
                    controller.post_click_key(4).wait()
                    if accelerate_back:
                        controller.post_click(x, y).wait()
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
            if restart_round < restart_limit:
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
                    f"正在重启游戏（第 {restart_round + 1}/{restart_limit} 次）",
                )
                if not _wait_unless_stopping(context, restart_wait):
                    return True
            elif (
                not emulator_rebooted
                and reboot_emulator_on_failure
                and restart_round == restart_limit
            ):
                if context.tasker.stopping:
                    return True
                log_task(
                    "游戏启动",
                    "模拟器",
                    "WARN",
                    f"游戏重启 {restart_limit} 次仍未识别主页，正在重启模拟器",
                )
                if _reboot_ldplayer(
                    emulator_console,
                    emulator_index,
                    adb_path,
                    adb_serial,
                ):
                    emulator_rebooted = True
                    app_started = True
                    controller = context.tasker.controller
                    controller.post_start_app(package).wait()
                    if not _wait_unless_stopping(context, restart_wait):
                        return True
                else:
                    log_task(
                        "游戏启动",
                        "模拟器",
                        "ERROR",
                        "模拟器重启后 adb 未恢复；本次任务停止",
                    )
                    return False
        log_task(
            "游戏启动",
            "结束",
            "ERROR",
            f"经过初次启动和 {restart_limit} 次重启仍未识别主页；"
            "可能停留在账号、验证码、实名或未收录页面",
        )
        return False
