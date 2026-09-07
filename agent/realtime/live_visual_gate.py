"""演出表现（3D演出 / FILM LIVE MV / OFF）关闭检查。

国服 1280x720 客户端在演出准备页左下角有一个圆形循环箭头切换按钮，
点击会在 3D演出 → FILM LIVE MV → OFF 之间循环；按钮右侧的标签实时
显示当前模式。3D 演出与 MV 都会改变演奏场背景，可能让首拍门控把背景
变化误判成第一颗音符，因此在开演前必须确认模式已回到 OFF。
"""

from __future__ import annotations

import cv2
import numpy as np

# 模式标签区域（红底“3D演出”/粉底 MV 标签所在位置）。
MODE_TAG_REGION = (170, 250, 605, 690)
# 循环箭头切换按钮圆心。
MODE_TOGGLE_POINT = (141, 649)
# 标签区域内强饱和像素数低于该值时视为 OFF。
MODE_OFF_MAX_SATURATED = 45


def live_performance_mode_is_off(image: np.ndarray) -> bool:
    """模式标签区域无强饱和色时视为演出表现已关闭（OFF）。"""
    x0, x1, y0, y1 = MODE_TAG_REGION
    hsv = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    saturated = np.count_nonzero((hsv[..., 1] >= 90) & (hsv[..., 2] >= 130))
    return saturated < MODE_OFF_MAX_SATURATED
