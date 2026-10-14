from __future__ import annotations

import cv2
import numpy as np
import pytest

from agent.realtime.song_title_ocr import (
    CONFIG_PATH,
    MODEL_PATH,
    _runtime,
    normalize_song_title,
    normalize_song_title_for_matching,
    strict_title_similarity,
    title_similarity,
    is_final_score_layout,
    is_final_score_text,
    final_cover_ink_roi,
    recognize_final_cover_title,
    FINAL_COVER_TITLE_ROI,
    FINAL_COVER_WIDE_TITLE_ROI,
    TitleReading,
    is_final_title_completion,
)


def test_bundled_song_title_model_and_dictionary_exist():
    assert MODEL_PATH.is_file()
    assert MODEL_PATH.stat().st_size > 10_000_000


def test_title_noise_cannot_reduce_unrelated_text_to_one_letter():
    assert title_similarity("ERER", "R") < 0.68
    assert CONFIG_PATH.is_file()


def test_bundled_song_title_model_loads_in_fixed_runtime():
    session, characters = _runtime()

    assert session.get_inputs()[0].shape[1] == 3
    assert len(characters) > 18_000


def test_song_title_normalization_ignores_width_case_spacing_and_punctuation():
    assert normalize_song_title("ＳＡＶＩＯＲ　ＯＦ　ＳＯＮＧ！") == "saviorofsong"


def test_matching_folds_kana_without_changing_canonical_title_keys():
    assert normalize_song_title("ペっ") == "ペっ"
    assert normalize_song_title("ペっ") != normalize_song_title("ぺっ")
    assert normalize_song_title_for_matching("ペっ") == normalize_song_title_for_matching("ぺっ")
    assert normalize_song_title_for_matching("ﾊｯﾋﾟｰ") == normalize_song_title_for_matching("ハッピー")
    assert title_similarity("ペったん", "ぺったん") == 1.0
    assert strict_title_similarity("ペったん", "ぺったん") == 1.0


@pytest.mark.parametrize("left,right", [("ハ", "バ"), ("ハ", "パ"), ("バ", "パ"), ("ツ", "っ")])
def test_matching_preserves_voicing_and_small_kana(left, right):
    assert normalize_song_title_for_matching(left) != normalize_song_title_for_matching(right)
    assert title_similarity(left, right) < 1.0
    assert strict_title_similarity(left, right) < 1.0


@pytest.mark.parametrize("observed,expected", [("", "R"), ("R", "!?"), ("", "")])
def test_both_title_scorers_reject_empty_normalized_text(observed, expected):
    assert title_similarity(observed, expected) == 0.0
    assert strict_title_similarity(observed, expected) == 0.0


def test_strict_title_similarity_keeps_real_suffix_and_ocr_noise():
    assert title_similarity("灼熱 Bonfire!(平行歌曲)", "灼熱 Bonfire!") == 1.0
    assert strict_title_similarity("灼熱 Bonfire!(平行歌曲)", "灼熱 Bonfire!") < 1.0
    assert strict_title_similarity("今の「僕は…」", "「僕は...」") < 0.95


def test_song_title_similarity_tolerates_small_japanese_ocr_errors():
    assert title_similarity(
        "ハッピーシンセサィ女",
        "ハッピーシンセサイザ",
    ) >= 0.75


def test_song_title_similarity_ignores_trailing_ocr_junk():
    assert title_similarity("「僕は…」今の", "「僕は...」") >= 0.95


def test_song_title_similarity_ignores_leading_ocr_junk():
    assert title_similarity("今の「僕は…」", "「僕は...」") >= 0.95


def test_score_page_requires_badge_and_separate_pink_digits():
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.putText(image, "5082000", (738, 530), cv2.FONT_HERSHEY_SIMPLEX, .9,
                (180, 140, 250), 2)
    assert not is_final_score_layout(image)
    cv2.circle(image, (382, 515), 26, (90, 200, 240), -1)
    assert is_final_score_layout(image)
    image[485:550, 730:960] = 0
    assert not is_final_score_layout(image)


def _sparse_score_frame():
    image = np.full((720, 1280, 3), 30, dtype=np.uint8)
    image[500:510, 370:390] = (50, 120, 150)
    for x in (740, 760, 780):
        image[500:510, x:x + 7] = (180, 140, 250)
    return image


@pytest.mark.parametrize("brightness", [0.2, 0.4, 0.6, 1.0])
def test_score_layout_survives_uniform_dimming_and_sparse_bright_foreground(brightness):
    image = (_sparse_score_frame().astype(np.float32) * brightness).astype(np.uint8)
    assert is_final_score_layout(image)
    calls, diagnostic = [], {}
    assert recognize_final_cover_title(
        image, reader=lambda *_a, **_k: calls.append(True), diagnostics=diagnostic,
    ) is None
    assert calls == []
    assert diagnostic["status"] == "excluded-score-layout"


def test_score_layout_keeps_old_result_when_dim_pink_connects_digit_components():
    image = _sparse_score_frame()
    for left, right in ((747, 760), (767, 780)):
        image[504:506, left:right] = (90, 70, 125)
    # 原 V 门槛排除暗色连接、保留三个数字；相对门槛可能把它们连成一块。
    assert is_final_score_layout(image)


def test_score_layout_handles_different_badge_and_digit_brightness():
    image = _sparse_score_frame()
    image[500:510, 370:390] = (25, 60, 75)
    assert is_final_score_layout(image)


@pytest.mark.parametrize("brightness", [0.4, 1.0])
@pytest.mark.parametrize("missing", ["badge", "digits", "black", "count", "height", "separation", "color", "position"])
def test_dim_score_layout_keeps_color_quantity_and_geometry_guards(brightness, missing):
    image = _sparse_score_frame()
    if missing == "badge":
        image[485:550, 350:420] = 30
    elif missing == "digits":
        image[485:550, 730:960] = 30
    elif missing == "black":
        image[:] = 0
    elif missing == "count":
        image[500:510, 780:787] = 30
    elif missing == "height":
        image[505:510, 730:960] = 30
    elif missing == "separation":
        image[500:510, 747:780] = (180, 140, 250)
    elif missing == "color":
        image[500:510, 370:390] = (150, 150, 150)
    elif missing == "position":
        image[500:510, 370:390] = 30
        image[500:510, 500:520] = (50, 120, 150)
    image = (image.astype(np.float32) * brightness).astype(np.uint8)
    assert not is_final_score_layout(image)


def test_score_text_exclusion_needs_label_and_digits():
    assert is_final_score_text("标得分'5''0''8'")
    assert is_final_score_text("目标得分 5082000")
    assert is_final_score_text("Target Score 5082000")
    assert not is_final_score_text("swim")
    assert not is_final_score_text("得分")
    assert not is_final_score_text("5082000")


def test_final_short_title_uses_bounded_ink_padding_after_low_confidence():
    image = np.full((720, 1280, 3), 220, dtype=np.uint8)
    image[502:530, 598:680] = 100
    ink_roi = (582, 492, 114, 48)
    assert final_cover_ink_roi(image) == ink_roi
    rois = []
    def reader(_image, *, roi):
        rois.append(roi)
        return TitleReading("swim", .53 if roi == FINAL_COVER_TITLE_ROI else .95)

    diagnostic = {}
    reading = recognize_final_cover_title(image, reader=reader, diagnostics=diagnostic)
    assert reading == TitleReading("swim", .95)
    assert rois == [FINAL_COVER_TITLE_ROI, ink_roi]
    assert diagnostic["source"] == "ink-roi"
    assert diagnostic["source_roi"] == ink_roi


def test_final_long_title_touching_band_edge_keeps_fixed_roi():
    image = np.full((720, 1280, 3), 220, dtype=np.uint8)
    x, _y, width, _height = FINAL_COVER_TITLE_ROI
    image[500:530, x:x + width] = 100
    assert final_cover_ink_roi(image) is None
    rois = []
    def reader(_image, *, roi):
        rois.append(roi)
        return TitleReading("魔法少女とチョコレゐト", .6)

    recognize_final_cover_title(image, reader=reader)
    assert rois == [FINAL_COVER_TITLE_ROI]


def test_final_title_enhancement_keeps_confidence_below_confirmation_threshold():
    image = np.full((720, 1280, 3), 220, dtype=np.uint8)
    image[502:530, 598:680] = 100
    reading = recognize_final_cover_title(
        image, reader=lambda _image, *, roi: TitleReading("swim", .5 if roi == FINAL_COVER_TITLE_ROI else .69),
    )
    assert reading.confidence == .69


def test_final_score_layout_is_excluded_before_ocr():
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.circle(image, (382, 515), 26, (90, 200, 240), -1)
    cv2.putText(image, "5082000", (738, 530), cv2.FONT_HERSHEY_SIMPLEX, .9,
                (180, 140, 250), 2)
    calls = []
    diagnostic = {}
    assert recognize_final_cover_title(image, reader=lambda *_a, **_k: calls.append(True),
                                       diagnostics=diagnostic) is None
    assert not calls
    assert diagnostic["status"] == "excluded-score-layout"


def test_default_final_title_frame_contains_complete_real_long_title_without_trial():
    image = np.full((720, 1280, 3), 220, dtype=np.uint8)
    calls, diagnostic = [], {}
    def reader(_image, *, roi):
        calls.append(roi)
        if roi == (480, 485, 318, 70):
            return TitleReading("・もちゃ・ペったん", .928743)
        if roi == (300, 485, 680, 70):
            return TitleReading("わちゃ・もちゃ・ぺったん行進曲", .97)
        pytest.fail("默认标题条不能请求试验宽框或下方乐队信息")
    reading = recognize_final_cover_title(image, reader=reader, diagnostics=diagnostic)
    assert reading.text == "わちゃ・もちゃ・ぺったん行進曲"
    assert calls == [(300, 485, 680, 70)]
    assert diagnostic["source"] == "fixed-roi"
    assert diagnostic["wide_title_trial"] is False


@pytest.mark.parametrize("narrow,wide,status,source", [
    (TitleReading("Expected", .99), TitleReading("Long Song With Expected Ending", .8), "missing", "wide-roi"),
    (TitleReading("Unknown Song", .99), TitleReading("Long Song With Expected Ending", .8), "missing", "fixed-roi"),
    (TitleReading("Expected", .99), TitleReading("Long Song With Expected Ending", .69), "missing", "fixed-roi"),
    (TitleReading("Expected", .99), TitleReading("Long Song With Expected Ending", .8), "conflict", "fixed-roi"),
    (TitleReading("R", .99), TitleReading("Long Song With Expected Ending", .8), "missing", "fixed-roi"),
    (TitleReading("怪", .99), TitleReading("怪獣", .95), "missing", "fixed-roi"),
    (TitleReading("ABYXEYGH", .99), TitleReading("ABCXEXGH", .95), "missing", "fixed-roi"),
    (TitleReading("Expected", .83), TitleReading("Long Song With Expected Ending", .8), "confirmed", "fixed-roi"),
])
def test_wide_title_requires_identity_and_safe_high_confidence_completion(narrow, wide, status, source):
    image = np.full((720, 1280, 3), 220, dtype=np.uint8)
    rois, diagnostic = [], {}
    def reader(_image, *, roi):
        rois.append(roi)
        return narrow if roi == FINAL_COVER_TITLE_ROI else wide
    result = recognize_final_cover_title(
        image, reader=reader, diagnostics=diagnostic, wide_title_trial=True,
        identity_validator=lambda reading: status if reading is narrow else "confirmed",
    )
    assert result is (wide if source == "wide-roi" else narrow)
    assert diagnostic["source"] == source
    assert rois == ([FINAL_COVER_TITLE_ROI] if status != "missing" else
                    [FINAL_COVER_TITLE_ROI, FINAL_COVER_WIDE_TITLE_ROI])


def test_wide_title_is_default_off_even_when_identity_is_missing():
    rois = []
    reading = TitleReading("Expected", .99)
    assert recognize_final_cover_title(
        np.full((720, 1280, 3), 220, dtype=np.uint8),
        reader=lambda _image, *, roi: rois.append(roi) or reading,
        identity_validator=lambda _reading: "missing",
    ) is reading
    assert rois == [FINAL_COVER_TITLE_ROI]


def test_wide_title_completes_kana_truncation_without_changing_canonical_normalization():
    narrow = TitleReading("わちゃ・もちゃ・ペっ", .96)
    wide = TitleReading("わちゃ・もちゃ・ぺったん行進曲", .974853754)
    diagnostic = {}
    result = recognize_final_cover_title(
        np.full((720, 1280, 3), 220, dtype=np.uint8),
        reader=lambda _image, *, roi: narrow if roi == FINAL_COVER_TITLE_ROI else wide,
        diagnostics=diagnostic, wide_title_trial=True,
        identity_validator=lambda reading: "missing" if reading is narrow else "confirmed",
    )
    assert result is wide
    assert diagnostic["source"] == "wide-roi"
    assert title_similarity(narrow.text, wide.text) >= .68
    assert normalize_song_title(narrow.text) == "わちゃもちゃペっ"


@pytest.mark.parametrize("text", ["別の曲わちゃもちゃ", "わちゃ・もちゃ・べったん行進曲"])
def test_kana_completion_cannot_replace_different_text_or_voicing(text):
    assert not is_final_title_completion(TitleReading("わちゃ・もちゃ・ペっ", .96), TitleReading(text, .99))
