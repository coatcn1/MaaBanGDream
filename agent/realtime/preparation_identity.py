"""在单人开演按钮出现的准备页复核歌曲身份。"""

from __future__ import annotations

import cv2
import numpy as np

from .difficulty_action import read_song_level, resolve_chart_for_selected_song
from .live_session import current_live_run, update_live_run
from .song_title_ocr import recognize_song_title


def read_preparation_title(image):
    # 标题行与下方等级/星级分开。去掉空白后再送 OCR，避免长空白把短标题压坏。
    x, y, width, height = 218, 540, 560, 38
    crop = image[y:y + height, x:x + width]
    if crop.shape[:2] != (height, width):
        return None
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    ys, xs = np.where(gray < 180)
    if len(xs) < 20:
        return None
    left, top = max(0, int(xs.min()) - 3), max(0, int(ys.min()) - 3)
    right, bottom = min(width, int(xs.max()) + 4), min(height, int(ys.max()) + 4)
    return recognize_song_title(image, roi=(x + left, y + top, right - left, bottom - top))


def confirm_preparation_identity(image, difficulty: str):
    run = current_live_run()
    if run is None:
        raise RuntimeError("准备页歌曲复核缺少本局上下文")
    # OCR 失败时也保留原始现场，避免失败报告只有空身份而无法复现。
    update_live_run(preparation_identity_image=image.copy())
    title = read_preparation_title(image)
    level = read_song_level(image, roi=(247, 582, 45, 32))
    badge = recognize_song_title(image, roi=(112, 560, 100, 37))
    if title is None or title.confidence < 0.7 or level is None or badge is None or badge.confidence < 0.7:
        raise RuntimeError("准备页歌曲身份读取失败：需要清晰的标题、难度与等级")
    if badge.text.strip().casefold() != difficulty.casefold():
        raise RuntimeError(f"准备页难度冲突：期望 {difficulty}，实际 {badge.text}")
    if run.song_level is not None and run.song_level != level:
        raise RuntimeError(f"准备页等级冲突：选曲页 {run.song_level}，准备页 {level}")
    resolution = resolve_chart_for_selected_song(run.song_id, difficulty, level, title.text)
    if resolution.selection is None:
        raise RuntimeError(f"准备页歌曲身份未确认：{resolution.reason}")
    updated = update_live_run(
        song_title=title.text, song_title_confidence=title.confidence,
        song_level=level, preparation_identity_image=image.copy(),
    )
    print(f"RealtimePreparationIdentity confirmed=true title={title.text!r} "
          f"level={level} difficulty={difficulty} "
          f"bestdori_song_id={resolution.selection.bestdori_song_id}", flush=True)
    return updated
