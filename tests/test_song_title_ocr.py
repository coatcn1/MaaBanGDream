from __future__ import annotations

from agent.realtime.song_title_ocr import (
    CONFIG_PATH,
    MODEL_PATH,
    _runtime,
    normalize_song_title,
    title_similarity,
)


def test_bundled_song_title_model_and_dictionary_exist():
    assert MODEL_PATH.is_file()
    assert MODEL_PATH.stat().st_size > 10_000_000
    assert CONFIG_PATH.is_file()


def test_bundled_song_title_model_loads_in_fixed_runtime():
    session, characters = _runtime()

    assert session.get_inputs()[0].shape[1] == 3
    assert len(characters) > 18_000


def test_song_title_normalization_ignores_width_case_spacing_and_punctuation():
    assert normalize_song_title("ＳＡＶＩＯＲ　ＯＦ　ＳＯＮＧ！") == "saviorofsong"


def test_song_title_similarity_tolerates_small_japanese_ocr_errors():
    assert title_similarity(
        "ハッピーシンセサィ女",
        "ハッピーシンセサイザ",
    ) >= 0.75


def test_song_title_similarity_ignores_trailing_ocr_junk():
    assert title_similarity("「僕は…」今の", "「僕は...」") >= 0.95


def test_song_title_similarity_ignores_leading_ocr_junk():
    assert title_similarity("今の「僕は…」", "「僕は...」") >= 0.95
