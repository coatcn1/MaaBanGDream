"""生命值归零“演出失败”弹窗的检测与有界退出导航。

死亡弹窗出现后演奏场会被弹窗遮挡，旧的演奏场监控会把“演奏场消失”误判为
“已进入结算”。本模块用弹窗右侧粉色“继续”按钮在受限 ROI 内的模板匹配确认
弹窗，连续多帧命中才触发，避免开演转场或技能特效的瞬时误报；触发后由
``exit_failed_live`` 按“演出失败弹窗 → 退出确认 → 主页”的顺序有界退出，
绝不点击需要消耗星石的“继续”。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_DEFAULT_TEMPLATE = (
    PROJECT_ROOT / "resource" / "image" / "live_failed_continue.png"
)
# 1280x720 参考系下“演出失败”弹窗右侧粉色“继续”按钮区域。
_DEFAULT_ROI = (630, 390, 300, 130)
_REFERENCE_WIDTH = 1280
_REFERENCE_HEIGHT = 720


def _load_gray_template(path: Path) -> np.ndarray:
    raw = cv2.imdecode(
        np.fromfile(str(path), dtype=np.uint8),
        cv2.IMREAD_GRAYSCALE,
    )
    if raw is None:
        raise RuntimeError(f"无法读取演出失败弹窗模板：{path}")
    return raw


class LiveFailedPopupDetector:
    """约 5Hz 的低频终态监控；连续命中多帧后才确认死亡弹窗。"""

    def __init__(
        self,
        *,
        template_path: str | Path | None = None,
        roi: tuple[int, int, int, int] = _DEFAULT_ROI,
        threshold: float = 0.90,
        confirm_frames: int = 3,
        check_interval_seconds: float = 0.2,
    ) -> None:
        self._template = _load_gray_template(
            Path(template_path) if template_path else _DEFAULT_TEMPLATE
        )
        self._roi = tuple(int(value) for value in roi)
        self._threshold = float(threshold)
        self._confirm_frames = max(1, int(confirm_frames))
        self._interval = max(0.0, float(check_interval_seconds))
        self._streak = 0
        self._next_check_at = float("-inf")
        self.triggered = False
        self.last_score: float | None = None
        self.match_box: tuple[int, int, int, int] | None = None

    def observe(self, image: Any, now: float) -> bool:
        """命中并连续确认后返回 True；内部按固定间隔节流。"""
        if self.triggered:
            return True
        if (
            not isinstance(image, np.ndarray)
            or image.ndim != 3
            or image.shape[2] < 3
        ):
            return False
        if now < self._next_check_at:
            return False
        self._next_check_at = now + self._interval
        gray = cv2.cvtColor(image[:, :, :3], cv2.COLOR_BGR2GRAY)
        height, width = gray.shape[:2]
        x_scale = width / _REFERENCE_WIDTH
        y_scale = height / _REFERENCE_HEIGHT
        roi_x, roi_y, roi_w, roi_h = self._roi
        x0 = max(0, min(width - 1, round(roi_x * x_scale)))
        y0 = max(0, min(height - 1, round(roi_y * y_scale)))
        x1 = max(x0 + 1, min(width, round((roi_x + roi_w) * x_scale)))
        y1 = max(y0 + 1, min(height, round((roi_y + roi_h) * y_scale)))
        region = gray[y0:y1, x0:x1]
        template_h, template_w = self._template.shape[:2]
        if region.shape[0] < template_h or region.shape[1] < template_w:
            self._streak = 0
            self.match_box = None
            return False
        result = cv2.matchTemplate(
            region, self._template, cv2.TM_CCOEFF_NORMED
        )
        _, score, _, location = cv2.minMaxLoc(result)
        self.last_score = float(score)
        if score >= self._threshold:
            self._streak += 1
            self.match_box = (
                x0 + int(location[0]),
                y0 + int(location[1]),
                template_w,
                template_h,
            )
            if self._streak >= self._confirm_frames:
                self.triggered = True
                return True
        else:
            self._streak = 0
            self.match_box = None
        return False

    def reset(self) -> None:
        self._streak = 0
        self.triggered = False
        self.last_score = None
        self.match_box = None


def exit_failed_live(
    context: Any,
    *,
    home_node: str = "RealtimeLiveHomeMarker",
    live_failed_continue_node: str = "RealtimeLiveFailedContinue",
    live_failed_exit_node: str = "RealtimeLiveFailedExit",
    quit_confirm_exit_node: str = "RealtimeQuitConfirmExit",
    timeout_seconds: float = 45.0,
) -> bool:
    """有界点击退出死亡弹窗并回到主页；主页命中或用户停止时提前返回。"""
    deadline = time.monotonic() + timeout_seconds
    clicked_fail_exit = False
    while time.monotonic() < deadline:
        if context.tasker.stopping:
            return True
        image = context.tasker.controller.post_screencap().wait().get()
        if context.tasker.stopping:
            return True
        home = context.run_recognition(home_node, image)
        if home and home.hit:
            return True
        confirm = context.run_recognition(live_failed_continue_node, image)
        if confirm and confirm.hit:
            exit_button = context.run_recognition(live_failed_exit_node, image)
            if exit_button and exit_button.hit and exit_button.box:
                if context.tasker.stopping:
                    return True
                box = exit_button.box
                context.tasker.controller.post_click(
                    box.x + box.w // 2,
                    box.y + box.h // 2,
                ).wait()
                clicked_fail_exit = True
                time.sleep(1.5)
            else:
                time.sleep(0.5)
            # 第一层“继续”和第二层“退出”的模板可能互相命中。只要
            # 第一层仍在场，即使上一帧已点击退出，也绝不能发送右侧输入。
            continue
        if clicked_fail_exit:
            quit_confirm = context.run_recognition(quit_confirm_exit_node, image)
            if quit_confirm and quit_confirm.hit and quit_confirm.box:
                if context.tasker.stopping:
                    return True
                box = quit_confirm.box
                context.tasker.controller.post_click(
                    box.x + box.w // 2,
                    box.y + box.h // 2,
                ).wait()
                time.sleep(1.5)
                continue
        time.sleep(0.5)
    return False
