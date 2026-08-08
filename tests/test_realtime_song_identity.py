from __future__ import annotations

import cv2
import numpy as np

from agent.realtime.song_identity import (
    SONG_ID_METHOD,
    UNKNOWN_SONG_ID,
    identify_song,
    same_song,
)


def song_screen(seed: int = 7) -> np.ndarray:
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    rng = np.random.default_rng(seed)
    image[110:600, 40:450] = rng.integers(
        0, 256, size=(490, 410, 3), dtype=np.uint8,
    )
    return image


def test_song_identity_is_stable_and_versioned():
    first = identify_song(song_screen())
    second = identify_song(song_screen())

    assert first == second
    assert first.method == SONG_ID_METHOD
    assert first.song_id.startswith(f"{SONG_ID_METHOD}-")
    assert len(first.song_id.removeprefix(f"{SONG_ID_METHOD}-")) == 16


def test_song_identity_matches_after_brightness_and_compression_changes():
    original = song_screen()
    brighter = cv2.convertScaleAbs(original, alpha=0.96, beta=8)
    encoded, jpeg = cv2.imencode(
        ".jpg", original, [cv2.IMWRITE_JPEG_QUALITY, 90],
    )
    assert encoded
    compressed = cv2.imdecode(jpeg, cv2.IMREAD_COLOR)

    identity = identify_song(original)
    assert same_song(identity.song_id, identify_song(brighter).song_id)
    assert same_song(identity.song_id, identify_song(compressed).song_id)


def test_visually_distinct_song_screens_do_not_match():
    first = identify_song(song_screen(7))
    second = identify_song(song_screen(83))

    assert not same_song(first.song_id, second.song_id)


def test_low_information_screen_returns_unknown_instead_of_a_false_identity():
    identity = identify_song(np.zeros((720, 1280, 3), dtype=np.uint8))

    assert identity.song_id == UNKNOWN_SONG_ID
    assert identity.method == "unknown"
    assert not same_song(identity.song_id, identity.song_id)
    assert not same_song("song-phash-v1-1", "song-phash-v1-1")
