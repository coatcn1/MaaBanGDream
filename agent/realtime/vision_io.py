"""Unicode 安全的 OpenCV 图像读写。

便携运行时自带的 cv2（Windows 版）对含中文等非 ASCII 字符的路径仍然使用
窄字符文件 API，`cv2.imread` / `cv2.imwrite` 会直接失败（例如安装目录为
``D:\\下载\\MaaBanGDream-v1.2.3-win-x64`` 时，开演前证据截图保存报
“无法保存实时演奏阶段证据截图”）。这里统一改为字节级读写：Python 的
``Path.read_bytes`` / ``write_bytes`` 使用 Unicode API，编解码交给内存中的
``cv2.imdecode`` / ``cv2.imencode``，不再让 cv2 触碰路径。
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


def imread_unicode(path: str | Path, flags: int = cv2.IMREAD_COLOR):
    """等价于 ``cv2.imread(path, flags)``，但兼容非 ASCII 路径。"""
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    if not data:
        return None
    return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), flags)


def imwrite_unicode(
    path: str | Path,
    image: np.ndarray,
    params: list[int] | None = None,
) -> bool:
    """等价于 ``cv2.imwrite(path, image)``，但兼容非 ASCII 路径。"""
    suffix = Path(path).suffix
    if not suffix:
        raise ValueError(f"图像保存路径缺少扩展名：{path}")
    ok, encoded = cv2.imencode(suffix, image, params or [])
    if not ok:
        return False
    Path(path).write_bytes(encoded.tobytes())
    return True
