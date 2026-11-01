"""公共选择演出页的体力消耗门禁；身份不明时不发送设置输入。"""

from functools import lru_cache
from pathlib import Path
import time

import cv2
import numpy as np

from .profile_store import RealtimeProfileStore
from .vision_io import imread_unicode

try:
    from ..screen_refresh import capture_image, ScreenRefreshCancelled
    from ..foreground_guard import require_game_foreground
except ImportError:
    from screen_refresh import capture_image, ScreenRefreshCancelled
    from foreground_guard import require_game_foreground


TEMPLATE_REGIONS = {
    "page_title": (128, 56, 105, 27),
    "page_settings": (1080, 130, 77, 30),
    "panel_title": (270, 43, 290, 29),
    "label_0": (254, 111, 94, 44),
    "label_1": (254, 188, 94, 44),
    "label_2": (254, 265, 94, 44),
    "label_3": (254, 342, 94, 44),
    "label_all": (238, 438, 105, 29),
    "ok": (542, 621, 195, 37),
}
ROWS = {0: 133, 1: 210, 2: 287, 3: 364, 10: 518}


class BoostDetector:
    def __init__(self):
        root = Path(__file__).resolve().parents[2]
        self.templates = {}
        for name in TEMPLATE_REGIONS:
            for base in (root / "resource/image", root / "resource/resource/image"):
                image = imread_unicode(base / "live_boost" / f"{name}.png")
                if image is not None:
                    self.templates[name] = image
                    break
            else:
                raise RuntimeError(f"体力门禁模板缺失：{name}")

    def matches(self, image, name):
        if getattr(image, "shape", ())[:2] != (720, 1280):
            return False
        x, y, w, h = TEMPLATE_REGIONS[name]
        region = image[y - 3 : y + h + 3, x - 3 : x + w + 3, :3]
        return (
            float(
                cv2.matchTemplate(
                    region, self.templates[name], cv2.TM_CCOEFF_NORMED
                ).max()
            )
            >= 0.92
        )

    def page(self, image):
        # 面板压暗后的背景仍可能命中模板，标题仍可见时不能当作已经关闭。
        return (
            not self.matches(image, "panel_title")
            and self.matches(image, "page_title")
            and self.matches(image, "page_settings")
        )

    def panel_selection(self, image):
        # 只核验消耗数字所在左栏，不读取奖励倍数或活动加成。
        if not all(
            self.matches(image, name)
            for name in TEMPLATE_REGIONS
            if not name.startswith("page")
        ):
            return None
        selected = []
        for value, y in ROWS.items():
            hsv = cv2.cvtColor(image[y - 8 : y + 9, 994:1011, :3], cv2.COLOR_BGR2HSV)
            pink = (
                (hsv[:, :, 0] >= 140)
                & (hsv[:, :, 0] <= 179)
                & (hsv[:, :, 1] >= 90)
                & (hsv[:, :, 2] >= 100)
            )
            if float(pink.mean()) >= 0.4:
                selected.append(value)
        return selected[0] if len(selected) == 1 else None


@lru_cache(maxsize=1)
def detector():
    return BoostDetector()


class LiveBoostGate:
    def __init__(
        self,
        context,
        *,
        detector=None,
        capture=capture_image,
        foreground=require_game_foreground,
        sleep=time.sleep,
    ):
        self.context = context
        self.detector = detector or globals()["detector"]()
        self.capture = capture
        self.foreground = foreground
        self.sleep = sleep

    def stopped(self):
        if self.context.tasker.stopping:
            raise ScreenRefreshCancelled("用户已停止体力消耗检查")

    def click(self, x, y):
        self.stopped()
        self.foreground(self.context.tasker.controller)
        self.stopped()
        # 前台检查可能撤销代理，每次输入都重新取得当前控制器。
        job = self.context.tasker.controller.post_click(x, y).wait()
        self.stopped()
        if not job.succeeded:
            raise RuntimeError("体力设置输入未得到成功回执")

    def stable(self, panel, expected=None):
        previous = object()
        streak = 0
        for _ in range(8):
            self.stopped()
            image = self.capture(self.context)
            self.stopped()
            reading = (
                self.detector.panel_selection(image)
                if panel
                else (True if self.detector.page(image) else None)
            )
            if reading is not None and reading == previous:
                streak += 1
            else:
                previous = reading
                streak = 1 if reading is not None else 0
            if streak >= 2:
                if expected is not None and reading != expected:
                    raise RuntimeError(
                        f"体力消耗复核不一致：目标 {expected}，实读 {reading}"
                    )
                return reading
            self.sleep(0.1)
        raise RuntimeError("体力消耗页面或唯一选中状态未能连续稳定确认")

    def close_panel(self, attempts: int = 3) -> None:
        """点 OK 关闭体力面板，确认回到选择演出页。

        面板开启动画尚未结束时，OK 点击可能被游戏吞掉（点击回执成功但面板
        不关）。这里按「面板是否已消失」有界重试，而不是一次点击后直接
        等待页面稳定；2026-10-23 22:40 竞演实测就卡在这一步。
        """
        for _ in range(max(1, attempts)):
            self.click(640, 640)
            for _ in range(15):
                self.stopped()
                if self.detector.page(self.capture(self.context)):
                    return
                self.sleep(0.1)
        raise RuntimeError("体力消耗页面关闭失败：OK 点击未生效")

    def ensure(self, target):
        if type(target) is not int or not 0 <= target <= 3:
            raise ValueError("live_boost_target 必须是 0..3 的整数")
        self.stable(False)
        self.click(1110, 145)
        current = self.stable(True)
        if current != target:
            self.click(1002, ROWS[target])
            self.stable(True, target)
        self.close_panel()
        # 关闭后重开读回，避免把仅改变高亮、尚未保存当成设置完成。
        self.click(1110, 145)
        self.stable(True, target)
        self.close_panel()
        print(f"LiveBoostGate current={current} target={target} corrected={str(current != target).lower()} verified=true", flush=True)
        return True


def ensure_live_boost(context):
    options = RealtimeProfileStore(
        Path(__file__).resolve().parents[2] / "profiles"
    ).runtime_options()
    if not options["live_boost_settings_enabled"]:
        return False
    return LiveBoostGate(context).ensure(options["live_boost_target"])
