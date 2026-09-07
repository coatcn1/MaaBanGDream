from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


SONG_ID_METHOD = "song-jacket-phash-v2"
UNKNOWN_SONG_ID = "unknown"
# Exact square jacket interior on the 1280x720 song-selection screen.  This
# intentionally excludes the red selection border and the ranking text below.
# v1 accidentally hashed the scrolling list on the left, so different songs
# on the same page could look almost identical to the chart registry.
SONG_ID_ROI = (684, 120, 320, 320)
# 开演前最终歌曲信息页的居中封面内部，不包含外框和下方难度标签。
FINAL_SONG_JACKET_ROI = (476, 64, 328, 328)
MAX_SAME_SONG_DISTANCE = 8
# 选曲页封面带边框/缩放差异时，个别谱面的 pHash 会多翻转几 bit（实测
# FIRE BIRD 稳定在 12 bit、最近的其他歌曲在 18 bit）。仅在同时具备等级
# 硬约束时允许用更宽阈值重试，不能单独放宽匹配。
LOOSE_SAME_SONG_DISTANCE = 14


@dataclass(frozen=True, slots=True)
class SongIdentity:
    song_id: str
    method: str


def identify_song(image: np.ndarray) -> SongIdentity:
    """Return a versioned perceptual identity of the selected song jacket."""
    x, y, width, height = SONG_ID_ROI
    if (
        not isinstance(image, np.ndarray)
        or image.ndim != 3
        or image.shape[0] < y + height
        or image.shape[1] < x + width
        or image.shape[2] < 3
    ):
        return SongIdentity(UNKNOWN_SONG_ID, "unknown")
    return fingerprint_jacket(image[y:y + height, x:x + width])


def identify_final_song(image: np.ndarray) -> SongIdentity:
    """读取所有演出模式共用的最终歌曲信息页封面。"""
    x, y, width, height = FINAL_SONG_JACKET_ROI
    if (
        not isinstance(image, np.ndarray)
        or image.ndim != 3
        or image.shape[0] < y + height
        or image.shape[1] < x + width
        or image.shape[2] < 3
    ):
        return SongIdentity(UNKNOWN_SONG_ID, "unknown")
    return fingerprint_jacket(image[y:y + height, x:x + width])


def detect_full_badge(image: np.ndarray) -> bool:
    """检测最终封面右上角的 FULL 徽标（白边深灰底圆角矩形）。

    仅单人演出会出现 FULL 谱面；徽标是稳定的游戏 UI 元素，固定在封面
    右上角。用它比标题 OCR 更可靠，可用于共享封面（FULL/普通）复核。
    """
    x, y, width, height = FINAL_SONG_JACKET_ROI
    if (
        not isinstance(image, np.ndarray)
        or image.ndim != 3
        or image.shape[0] < y + height
        or image.shape[1] < x + width
    ):
        return False
    cover = image[y:y + height, x:x + width]
    gray = cv2.cvtColor(cover, cv2.COLOR_BGR2GRAY)
    region = gray[0:int(height * 0.12), int(width * 0.68):width]
    white = region > 180

    def longest_run(row: np.ndarray) -> int:
        if not row.any():
            return 0
        positions = np.flatnonzero(row)
        best = 1
        current = 1
        for left, right in zip(positions[:-1], positions[1:]):
            current = current + 1 if right == left + 1 else 1
            best = max(best, current)
        return best

    # 徽标上边框是覆盖顶部边缘的连续白线；普通封面顶部没有这么长的白线。
    if longest_run(white[0]) < 30:
        return False
    # 下边框：中下部行内还有一段长白线，中间深灰填充形成空心矩形。
    rows = white.shape[0]
    lower = [longest_run(row) for row in white[int(rows * 0.55):]]
    if max(lower, default=0) < 25:
        return False
    # 左竖白边框至少一列贯穿中段。
    columns = white[1:rows - 1].sum(axis=0)
    return bool((columns >= 6).any())


def fingerprint_jacket(image: np.ndarray) -> SongIdentity:
    """Return the runtime-compatible identity of a square source jacket."""
    if (
        not isinstance(image, np.ndarray)
        or image.ndim != 3
        or image.shape[0] < 8
        or image.shape[1] < 8
        or image.shape[2] < 3
    ):
        return SongIdentity(UNKNOWN_SONG_ID, "unknown")
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if float(gray.std()) < 2.0:
        return SongIdentity(UNKNOWN_SONG_ID, "unknown")
    normalized = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
    coefficients = cv2.dct(normalized.astype(np.float32))[:8, :8]
    threshold = float(np.median(coefficients.reshape(-1)[1:]))
    bits = coefficients >= threshold
    digest = f"{int(''.join('1' if bit else '0' for bit in bits.flat), 2):016x}"
    return SongIdentity(f"{SONG_ID_METHOD}-{digest}", SONG_ID_METHOD)


def same_song(left: str, right: str, *, max_distance: int = MAX_SAME_SONG_DISTANCE) -> bool:
    """Return whether two version-compatible perceptual identities match."""
    prefix = f"{SONG_ID_METHOD}-"
    if not left.startswith(prefix) or not right.startswith(prefix):
        return False
    left_digest = left.removeprefix(prefix)
    right_digest = right.removeprefix(prefix)
    if len(left_digest) != 16 or len(right_digest) != 16:
        return False
    try:
        left_hash = int(left_digest, 16)
        right_hash = int(right_digest, 16)
    except ValueError:
        return False
    return (left_hash ^ right_hash).bit_count() <= max_distance
