from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from maa.pipeline import JOCR, JRecognitionType

try:
    from .foreground_guard import require_game_foreground
    from .screen_refresh import ScreenRefreshCancelled, capture_image
    from .task_reporting import log_task
except ImportError:  # AgentServer loads modules from the agent directory.
    from foreground_guard import require_game_foreground
    from screen_refresh import ScreenRefreshCancelled, capture_image
    from task_reporting import log_task


DEFAULT_ROI = (0, 100, 1280, 620)


@dataclass(frozen=True)
class _Box:
    x: int
    y: int
    w: int
    h: int


def _find_free_live_card(image: Any) -> _Box | None:
    """Locate the stable cyan free-live card when artwork/OCR changes."""
    if not isinstance(image, np.ndarray) or image.ndim != 3:
        return None

    height, width = image.shape[:2]
    x_offset = int(width * 0.45)
    y_offset = int(height * 0.12)
    crop = image[y_offset:int(height * 0.88), x_offset:width]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (85, 70, 100), (115, 255, 255))
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        np.ones((7, 7), dtype=np.uint8),
    )
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask)

    candidates: list[tuple[int, _Box]] = []
    for x, y, box_width, box_height, area in stats[1:count]:
        if not (
            width * 0.09 <= box_width <= width * 0.30
            and height * 0.24 <= box_height <= height * 0.70
        ):
            continue
        if area < box_width * box_height * 0.45:
            continue
        candidates.append(
            (
                int(area),
                _Box(
                    x=int(x + x_offset),
                    y=int(y + y_offset),
                    w=int(box_width),
                    h=int(box_height),
                ),
            )
        )

    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _find_cooperative_live_card(image: Any) -> _Box | None:
    """Locate the large pink cooperative card in its default state."""
    if not isinstance(image, np.ndarray) or image.ndim != 3:
        return None

    height, width = image.shape[:2]
    x_offset = int(width * 0.55)
    y_offset = int(height * 0.12)
    crop = image[y_offset:int(height * 0.88), x_offset:width]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hue = hsv[:, :, 0]
    mask = (
        (((hue <= 12) | (hue >= 165))
         & (hsv[:, :, 1] >= 70)
         & (hsv[:, :, 2] >= 100))
    ).astype(np.uint8) * 255
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        np.ones((7, 7), dtype=np.uint8),
    )
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask)

    candidates: list[tuple[int, _Box]] = []
    for x, y, box_width, box_height, area in stats[1:count]:
        if not (
            width * 0.09 <= box_width <= width * 0.30
            and height * 0.24 <= box_height <= height * 0.70
        ):
            continue
        if area < box_width * box_height * 0.45:
            continue
        candidates.append(
            (
                int(area),
                _Box(
                    x=int(x + x_offset),
                    y=int(y + y_offset),
                    w=int(box_width),
                    h=int(box_height),
                ),
            )
        )

    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _find_tour_live_card(image: Any) -> _Box | None:
    """定位选择演出页中央紫色“巡回演出”卡片。"""
    if not isinstance(image, np.ndarray) or image.ndim != 3:
        return None
    height, width = image.shape[:2]
    x_offset = int(width * 0.45)
    y_offset = int(height * 0.18)
    crop = image[y_offset:int(height * 0.72), x_offset:int(width * 0.82)]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (125, 70, 100), (160, 255, 255))
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        np.ones((7, 7), dtype=np.uint8),
    )
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    candidates: list[tuple[int, _Box]] = []
    for x, y, box_width, box_height, area in stats[1:count]:
        if not (
            width * 0.12 <= box_width <= width * 0.24
            and height * 0.14 <= box_height <= height * 0.32
        ):
            continue
        if area < box_width * box_height * 0.35:
            continue
        candidates.append((
            int(area),
            _Box(
                x=int(x + x_offset),
                y=int(y + y_offset),
                w=int(box_width),
                h=int(box_height),
            ),
        ))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _params(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        return json.loads(raw)
    return {}


def _roi(value: Any) -> tuple[int, int, int, int]:
    if isinstance(value, (list, tuple)) and len(value) == 4:
        return tuple(int(item) for item in value)
    return DEFAULT_ROI


def _click_entry(context: Context, box: Any, *, deadline: float | None = None) -> bool:
    """取消时返回 False；输入任务失败时保留明确错误。"""
    if context.tasker.stopping:
        return False
    require_game_foreground(context.tasker.controller)
    if context.tasker.stopping:
        return False
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("演出入口送达确认已超时，停止重试点击")
    # 前台保护也可能使远程 Controller 代理失效，输入前必须重新取得。
    job = context.tasker.controller.post_click(
        box.x + box.w // 2,
        box.y + box.h // 2,
    ).wait()
    if context.tasker.stopping:
        return False
    if not job.succeeded:
        raise RuntimeError("演出入口点击失败：控制器未确认输入成功")
    return True


def _same_source_box(context, image, params, expected, template_node, source):
    """仅使用原识别来源重新定位当前帧的入口。"""
    if source == "模板":
        result = context.run_recognition(template_node, image)
        return result.box if result and result.hit and result.box else None
    if source == "OCR":
        result = context.run_recognition_direct(
            JRecognitionType.OCR,
            JOCR(
                expected=[expected],
                roi=_roi(params.get("roi")),
                threshold=float(params.get("threshold", 0.3)),
            ),
            image,
        )
        return result.box if result and result.hit and result.box else None
    finder = {
        "自由演出": _find_free_live_card,
        "协力演出": _find_cooperative_live_card,
        "巡回演出": _find_tour_live_card,
    }.get(expected)
    return finder(image) if finder else None


def _confirm_delivery(context, params, expected, template_node, source, delivery_node):
    timeout = max(0, int(params.get("delivery_timeout_ms", 12000))) / 1000
    retry_interval = max(1000, int(params.get("delivery_retry_interval_ms", 1500))) / 1000
    max_clicks = min(3, max(1, int(params.get("delivery_max_clicks", 3))))
    poll_interval = max(0.05, int(params.get("interval_ms", 500)) / 1000)
    deadline = time.monotonic() + timeout
    next_click = time.monotonic() + retry_interval
    clicks = 1
    while True:
        if context.tasker.stopping:
            return True
        image = capture_image(context)
        if context.tasker.stopping:
            return True
        delivered = context.run_recognition(delivery_node, image)
        if context.tasker.stopping:
            return True
        if delivered and delivered.hit:
            log_task("界面导航", "选择演出", "INFO", f"已确认进入“{expected}”房间选择页（点击 {clicks} 次）")
            return True
        now = time.monotonic()
        if now >= deadline:
            log_task("界面导航", "选择演出", "ERROR", f"点击“{expected}”入口后未确认房间选择页（点击 {clicks} 次，等待 {timeout:g} 秒）")
            return False
        if clicks < max_clicks and now >= next_click:
            box = _same_source_box(context, image, params, expected, template_node, source)
            if context.tasker.stopping:
                return True
            if box is not None and time.monotonic() < deadline:
                if not _click_entry(context, box, deadline=deadline):
                    return True
                clicks += 1
                next_click = time.monotonic() + retry_interval
        time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))


@AgentServer.custom_action("LiveSelectFind")
class LiveSelectFind(CustomAction):
    """Find a live-select entry by template, then exact OCR."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            params = _params(argv.custom_action_param)
            expected = str(params.get("expected", "自由演出"))
            template_node = str(params.get("template_node", "")).strip()
            should_click = bool(params.get("click", True))
            missing_reason = str(
                params.get("missing_reason", f"未找到“{expected}”入口")
            )
            timeout = max(0, int(params.get("timeout_ms", 0))) / 1000
            interval = max(0, int(params.get("interval_ms", 500))) / 1000
            deadline = time.monotonic() + timeout

            box = None
            source = ""
            while True:
                if context.tasker.stopping:
                    return True
                image = capture_image(context)

                if template_node:
                    template_result = context.run_recognition(
                        template_node,
                        image,
                    )
                    if (
                        template_result
                        and template_result.hit
                        and template_result.box
                    ):
                        box = template_result.box
                        source = "模板"

                if box is None:
                    ocr_result = context.run_recognition_direct(
                        JRecognitionType.OCR,
                        JOCR(
                            expected=[expected],
                            roi=_roi(params.get("roi")),
                            threshold=float(params.get("threshold", 0.3)),
                        ),
                        image,
                    )
                    if ocr_result and ocr_result.hit and ocr_result.box:
                        box = ocr_result.box
                        source = "OCR"

                if box is None and expected == "自由演出":
                    box = _find_free_live_card(image)
                    if box is not None:
                        source = "颜色与形状"

                if box is None and expected == "协力演出":
                    box = _find_cooperative_live_card(image)
                    if box is not None:
                        source = "颜色与形状"

                if box is None and expected == "巡回演出":
                    box = _find_tour_live_card(image)
                    if box is not None:
                        source = "颜色与形状"

                if box is not None or time.monotonic() >= deadline:
                    break
                time.sleep(interval)

            if box is None:
                log_task("界面导航", "选择演出", "ERROR", missing_reason)
                return False

            log_task(
                "界面导航",
                "选择演出",
                "INFO",
                f"{source}识别到“{expected}”"
                f"（x={box.x}, y={box.y}, w={box.w}, h={box.h}）",
            )
            if not should_click:
                return True

            if not _click_entry(context, box):
                return True
            delivery_node = str(params.get("delivery_node", "")).strip()
            if delivery_node:
                return _confirm_delivery(
                    context, params, expected, template_node, source, delivery_node,
                )
            return True
        except ScreenRefreshCancelled:
            return True
        except Exception as exc:
            traceback.print_exc()
            log_task(
                "界面导航",
                "选择演出",
                "ERROR",
                f"{type(exc).__name__}: {exc}",
            )
            return False
