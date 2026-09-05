"""协力等待弹窗识别，防止“其他成员正在准备中”误触发首拍门控。

协力进入演奏场后、所有成员准备完成前，游戏会在轨道中下部叠加一个白色
圆角弹窗。该弹窗的突然出现/消失会让首拍门控在判定线附近观察到的整行
颜色发生大变化，被误当成第一颗音符，导致谱面时钟提前启动。本模块只做
轻量视觉判断：普通演奏场直接靠白色占比快速排除，疑似弹窗时才做连通域
与粉色图标复核，避免热路径阻塞。
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np


class CooperativePreparePopupDetector:
    """识别协力准备弹窗；返回 ``True`` 表示当前帧存在弹窗。"""

    _REFERENCE_WIDTH = 1280
    _REFERENCE_HEIGHT = 720

    # 弹窗位于演奏场中下部、判定线之上。ROI 保留余量以容纳分辨率与皮肤
    # 差异，但不能覆盖顶部计分板或底部判定线。
    _ROI_LEFT = 0.12
    _ROI_RIGHT = 0.88
    _ROI_TOP = 0.50
    _ROI_BOTTOM = 0.84

    # 快速路径：整块 ROI 的白色占比过低时直接返回 False，普通演奏场
    # 不必逐帧执行连通域分析。
    _FAST_LEFT = 0.25
    _FAST_RIGHT = 0.75
    _FAST_TOP = 0.52
    _FAST_BOTTOM = 0.82
    _MIN_WHITE_FRACTION = 0.02

    # 弹窗主体是横向白色圆角矩形；出现/消失时有缩放动画，可能只在
    # 几帧内达到全尺寸，因此宽度下限要比完整弹窗更宽松。粉色图标与
    # 居中位置继续提供特异性，避免把舞台角色当成弹窗。
    _MIN_BOX_WIDTH = 0.08
    _MAX_BOX_WIDTH = 0.62
    _MIN_BOX_HEIGHT = 0.025
    _MAX_BOX_HEIGHT = 0.24
    _MIN_BOX_ASPECT = 2.0
    _MAX_BOX_ASPECT = 9.0
    _MIN_BOX_CENTER_X = 0.32
    _MAX_BOX_CENTER_X = 0.68
    _MIN_BOX_CENTER_Y = 0.55
    _MAX_BOX_CENTER_Y = 0.80

    # 弹窗左侧八分音符图标的粉红渐变是强特征。只在白色主体内部左三分之
    # 一统计粉色，避免把舞台角色身上的粉色当成弹窗图标。
    _MIN_PINK_PIXELS = 10
    _PINK_HUE_MIN = 148
    _PINK_HUE_MAX = 178
    _PINK_SAT_MIN = 50
    _PINK_VAL_MIN = 70

    _WHITE_SAT_MAX = 70
    _WHITE_VAL_MIN = 190

    def __call__(self, image: Any) -> bool:
        if (
            not isinstance(image, np.ndarray)
            or image.ndim != 3
            or image.shape[2] < 3
            or image.shape[0] < 1
            or image.shape[1] < 1
        ):
            return False
        height, width = image.shape[:2]
        left = max(0, round(self._ROI_LEFT * width))
        right = min(width, round(self._ROI_RIGHT * width))
        top = max(0, round(self._ROI_TOP * height))
        bottom = min(height, round(self._ROI_BOTTOM * height))
        if right <= left or bottom <= top:
            return False
        roi = image[top:bottom, left:right, :3]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        # 快速路径只看弹窗最可能出现的中心区域，兼顾缩放动画中的小尺寸
        # 弹窗；普通等待场该区域几乎没有白色。
        fast_left = max(0, round(self._FAST_LEFT * width) - left)
        fast_right = min(
            roi.shape[1],
            round(self._FAST_RIGHT * width) - left,
        )
        fast_top = max(0, round(self._FAST_TOP * height) - top)
        fast_bottom = min(
            roi.shape[0],
            round(self._FAST_BOTTOM * height) - top,
        )
        if fast_right <= fast_left or fast_bottom <= fast_top:
            return False
        white = (
            (hsv[:, :, 1] <= self._WHITE_SAT_MAX)
            & (hsv[:, :, 2] >= self._WHITE_VAL_MIN)
        )
        if (
            float(
                white[fast_top:fast_bottom, fast_left:fast_right].mean()
            )
            < self._MIN_WHITE_FRACTION
        ):
            return False

        count, labels, stats, _centroids = (
            cv2.connectedComponentsWithStats(
                white.astype(np.uint8),
                connectivity=8,
            )
        )
        if count <= 1:
            return False
        # 弹窗是 ROI 内最大的白色连通块；小面积噪点不参与比较。
        largest = int(np.argmax(stats[1:, cv2.CC_STAT_AREA])) + 1
        box_x, box_y, box_w, box_h = (
            int(stats[largest, cv2.CC_STAT_LEFT]),
            int(stats[largest, cv2.CC_STAT_TOP]),
            int(stats[largest, cv2.CC_STAT_WIDTH]),
            int(stats[largest, cv2.CC_STAT_HEIGHT]),
        )
        box_area = box_w * box_h
        if (
            box_w < self._MIN_BOX_WIDTH * width
            or box_w > self._MAX_BOX_WIDTH * width
            or box_h < self._MIN_BOX_HEIGHT * height
            or box_h > self._MAX_BOX_HEIGHT * height
            or box_area <= 0
            or not (
                self._MIN_BOX_ASPECT
                <= box_w / box_h
                <= self._MAX_BOX_ASPECT
            )
        ):
            return False
        # 连通域坐标相对 ROI 左上角，比较前还原为全图坐标。
        center_x = left + box_x + box_w / 2
        center_y = top + box_y + box_h / 2
        if not (
            self._MIN_BOX_CENTER_X * width
            <= center_x
            <= self._MAX_BOX_CENTER_X * width
        ):
            return False
        if not (
            self._MIN_BOX_CENTER_Y * height
            <= center_y
            <= self._MAX_BOX_CENTER_Y * height
        ):
            return False

        # 只统计白色主体左三分之一内的粉色像素，保证“白底 + 粉图标”
        # 同时成立才视为弹窗。
        icon_left = max(0, box_x)
        icon_right = max(
            icon_left + 1,
            min(box_x + box_w, round(box_x + box_w / 3)),
        )
        icon_top = max(0, box_y)
        icon_bottom = min(roi.shape[0], box_y + box_h)
        if icon_right <= icon_left or icon_bottom <= icon_top:
            return False
        icon = hsv[
            icon_top:icon_bottom,
            icon_left:icon_right,
        ]
        pink = (
            (icon[:, :, 0] >= self._PINK_HUE_MIN)
            & (icon[:, :, 0] <= self._PINK_HUE_MAX)
            & (icon[:, :, 1] >= self._PINK_SAT_MIN)
            & (icon[:, :, 2] >= self._PINK_VAL_MIN)
        )
        minimum = max(
            1,
            round(
                self._MIN_PINK_PIXELS
                * (width / self._REFERENCE_WIDTH)
                * (height / self._REFERENCE_HEIGHT)
            ),
        )
        return int(pink.sum()) >= minimum
