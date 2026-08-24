from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


SONG_ID_METHOD = "song-phash-v1"
UNKNOWN_SONG_ID = "unknown"
SONG_ID_ROI = (40, 110, 410, 490)
MAX_SAME_SONG_DISTANCE = 8


@dataclass(frozen=True, slots=True)
class SongIdentity:
    song_id: str
    method: str


def identify_song(image: np.ndarray) -> SongIdentity:
    """Return a versioned perceptual identity for the selected song screen."""
    x, y, width, height = SONG_ID_ROI
    if (
        not isinstance(image, np.ndarray)
        or image.ndim != 3
        or image.shape[0] < y + height
        or image.shape[1] < x + width
        or image.shape[2] < 3
    ):
        return SongIdentity(UNKNOWN_SONG_ID, "unknown")
    crop = image[y:y + height, x:x + width]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
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
