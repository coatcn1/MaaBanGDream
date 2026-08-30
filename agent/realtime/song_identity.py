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
MAX_SAME_SONG_DISTANCE = 8


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
