"""每日免费抽卡任务：完成每日演出奖励后抽取“每日3次免费 演出招募”。

流程与用户录像一致：主页点“招募”，在左侧卡池列表内有界向上滑动找到
“每日3次免费 演出招募”并选中；未完成每日演出或今日次数已耗尽时直接
正常结束。可抽时循环最多 3 次单抽，每次“点免费按钮/再次招募 → 确认
弹窗点招募 → 等待结果页”，结果页出现的一次性弹窗（重复成员、新成员
等）用 BACK 关闭后继续，直到“剩余0回”点确定回主页。

坐标按固定 1280x720 布局；页面状态识别全部走仓库内模板，不依赖 OCR。
"""
from __future__ import annotations

import time
import traceback
from typing import Any

import numpy as np

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    from ..foreground_guard import require_game_foreground
    from ..task_reporting import log_task, record_failure_reason
except ImportError:  # AgentServer imports realtime as a top-level package.
    from foreground_guard import require_game_foreground
    from task_reporting import log_task, record_failure_reason


# 主页“招募”按钮（实测 (660, 647) 可稳定进入招募页）。
_HOME_GACHA_TARGET = (660, 647)
# 免费卡池页右下“1次招募 / 免费”按钮中心。
_FREE_PULL_TARGET = (1183, 648)
# 结果页/弹窗推进用的“右下角确定”按钮中心。
_RESULT_OK_TARGET = (1069, 646)
# 左侧卡池列表的滑动区域（自下而上）。
_POOL_SWIPE_NODE = "DailyGachaSwipe"
_MAX_POOL_SWIPES = 20
_MAX_PULLS = 3


def _images_identical(before: Any, after: Any, threshold: float = 1.2) -> bool:
    """用整图平均绝对差判断滑动后画面是否被弹窗冻结。"""
    if (
        before is None
        or after is None
        or getattr(before, "shape", None) != getattr(after, "shape", None)
    ):
        return False
    try:
        difference = np.abs(
            before[:, :, :3].astype("float32")
            - after[:, :, :3].astype("float32")
        ).mean()
    except Exception:
        return False
    return bool(difference < threshold)


@AgentServer.custom_action("DailyFreeGachaRun")
class DailyFreeGacha(CustomAction):
    """驱动每日免费三抽的状态机；每步都检查用户停止。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            return self._run(context)
        except Exception as exc:
            record_failure_reason(
                f"每日免费抽卡异常：{type(exc).__name__}: {exc}"
            )
            print(
                f"DailyFreeGacha failed={type(exc).__name__}: {exc}",
                flush=True,
            )
            traceback.print_exc()
            return False

    def _run(self, context: Context) -> bool:
        controller = context.tasker.controller

        def capture() -> Any:
            return controller.post_screencap().wait().get()

        def recognize(node: str, image: Any) -> Any:
            return context.run_recognition(node, image)

        def hit(result: Any) -> bool:
            return bool(result and result.hit)

        def click_result(result: Any) -> None:
            box = result.box
            controller.post_click(
                box.x + box.w // 2,
                box.y + box.h // 2,
            ).wait()

        def wait_for(node: str, timeout: float) -> Any:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if context.tasker.stopping:
                    return None
                result = recognize(node, capture())
                if hit(result):
                    return result
                time.sleep(0.5)
            return None

        def clear_popups() -> None:
            """关闭“超值商品上架”等一次性弹窗（点击“稍后再确认”）。"""
            for _attempt in range(3):
                if context.tasker.stopping:
                    return
                result = recognize("DailyGachaPopupLater", capture())
                if not hit(result):
                    return
                log_task("每日免费抽卡", "弹窗", "INFO", "检测到一次性弹窗，点击稍后再确认")
                click_result(result)
                time.sleep(1.2)

        # 1. 主页点击“招募”，进入招募页。
        require_game_foreground(controller)
        image = capture()
        if not hit(recognize("HomeMarker", image)):
            record_failure_reason("每日免费抽卡：未能识别主页")
            return False
        clear_popups()
        controller.post_click(*_HOME_GACHA_TARGET).wait()
        # 招募页加载较慢，且可能先出现活动介绍页；等页面稳定后再开始滑动，
        # 避免在加载页上滑动并误触发 BACK。
        time.sleep(3.0)
        clear_popups()

        # 2. 左侧卡池列表有界向上滑动，直到找到免费卡池入口。
        selected = False
        search_recoveries = 0
        for _attempt in range(_MAX_POOL_SWIPES):
            if context.tasker.stopping:
                return True
            clear_popups()
            image = capture()
            entry = recognize("DailyGachaPoolEntry", image)
            if hit(entry):
                click_result(entry)
                selected = True
                time.sleep(1.2)
                break
            context.run_task(_POOL_SWIPE_NODE)
            time.sleep(1.0)
            # 每滑动 8 次仍未找到入口，可能误入了生日服装商店等页面：
            # 有界 BACK 一次；若因此回到主页，就重新进入招募页继续查找。
            if (_attempt + 1) % 8 == 0:
                if search_recoveries >= 2:
                    break
                search_recoveries += 1
                log_task("每日免费抽卡", "恢复", "INFO", "未找到免费卡池入口，BACK 一次后继续")
                controller.post_click_key(4).wait()
                time.sleep(1.2)
                if hit(recognize("HomeMarker", capture())):
                    clear_popups()
                    controller.post_click(*_HOME_GACHA_TARGET).wait()
                    time.sleep(3.0)
                    clear_popups()
        if not selected:
            record_failure_reason(
                "每日免费抽卡：滑动整个卡池列表仍未找到免费卡池入口"
            )
            return False

        # 3. 不再用状态模板区分“未完成每日演出”和“次数已耗尽”：两种情况
        # 都表现为点免费按钮后没有确认弹窗，由后面的确认等待统一处理，
        # 避免状态模板互相误匹配。
        time.sleep(1.0)
        image = capture()
        if not hit(recognize("DailyGachaPoolEntry", image)):
            record_failure_reason("每日免费抽卡：免费卡池未选中或页面异常")
            return False

        # 4. 最多三抽：点免费按钮 → 确认弹窗点招募 → 结果页清理弹窗。
        pulled = 0
        while pulled < _MAX_PULLS:
            if context.tasker.stopping:
                return True
            clear_popups()
            controller.post_click(*_FREE_PULL_TARGET).wait()
            time.sleep(1.0)
            confirm = wait_for("DailyGachaConfirmYes", 8.0)
            if confirm is None:
                if context.tasker.stopping:
                    return True
                # 点击免费按钮后没有出现确认弹窗：今日次数已耗尽或已不可抽。
                # 不再依赖“剩余0回”模板，避免把可抽状态的按钮误判成已抽完。
                break
            if confirm is not None and confirm.box:
                click_result(confirm)
                time.sleep(1.0)
            # 9.4.3 免费单抽先出现“TOUCH TO CUT”剪票引导，点一下剪票。
            cut = wait_for("DailyGachaTouchCut", 12.0)
            if cut is not None and cut.box:
                click_result(cut)
                time.sleep(1.2)
            # 通用推进：点右下角确定 + ESC 交替，能应付结果展示、重复成员
            # 道具弹窗等大多数页面，直到回到卡池页或主页。
            for _advance in range(10):
                if context.tasker.stopping:
                    return True
                image = capture()
                if hit(recognize("DailyGachaPoolEntry", image)) or hit(
                    recognize("HomeMarker", image)
                ):
                    break
                controller.post_click(*_RESULT_OK_TARGET).wait()
                time.sleep(1.0)
                image = capture()
                if hit(recognize("DailyGachaPoolEntry", image)) or hit(
                    recognize("HomeMarker", image)
                ):
                    break
                controller.post_click_key(4).wait()
                time.sleep(1.0)
            if context.tasker.stopping:
                return True
            pulled += 1

        # 5. 回主页（最多 4 次 BACK）。
        for _attempt in range(4):
            if context.tasker.stopping:
                return True
            image = capture()
            if hit(recognize("HomeMarker", image)):
                break
            controller.post_click_key(4).wait()
            time.sleep(1.0)
        log_task(
            "每日免费抽卡",
            "结束",
            "SUCCESS",
            f"✅ 今日免费抽卡结束：已抽 {pulled} 次"
            f"{'' if pulled else '（无剩余次数或不可用）'}",
        )
        return True
