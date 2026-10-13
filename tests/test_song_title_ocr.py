from __future__ import annotations

import cv2
import numpy as np
import pytest

from agent.realtime.song_title_ocr import (
    CONFIG_PATH,
    MODEL_PATH,
    _runtime,
    normalize_song_title,
    title_similarity,
    is_final_score_layout,
    is_final_score_text,
    final_cover_ink_roi,
    recognize_final_cover_title,
    FINAL_COVER_TITLE_ROI,
    FINAL_COVER_WIDE_TITLE_ROI,
    TitleReading,
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


def test_score_page_layout_survives_dimmed_transition_frame():
    # 开演前加载会经过压暗/淡入帧：实测目标得分帧数字区最大亮度只有 154（阈值
    # 160）、黄色星章几乎被压没，旧实现的固定 V 阈值会漏判，把目标得分当成曲名
    # 触发身份冲突。亮度归一化后仍应认出这是分数页。
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.putText(image, "5082000", (738, 530), cv2.FONT_HERSHEY_SIMPLEX, .9,
                (180, 140, 250), 2)
    cv2.circle(image, (382, 515), 26, (90, 200, 240), -1)
    dimmed = np.clip(image.astype(np.float32) * 0.62, 0, 255).astype(np.uint8)
    assert is_final_score_layout(dimmed)


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
    image[500:530, 480:798] = 100
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


@pytest.mark.parametrize("narrow,wide,status,source", [
    (TitleReading("Expected", .99), TitleReading("Long Song With Expected Ending", .8), "missing", "wide-roi"),
    (TitleReading("Unknown Song", .99), TitleReading("Long Song With Expected Ending", .8), "missing", "fixed-roi"),
    (TitleReading("Expected", .99), TitleReading("Long Song With Expected Ending", .69), "missing", "fixed-roi"),
    (TitleReading("Expected", .99), TitleReading("Long Song With Expected Ending", .8), "conflict", "fixed-roi"),
    (TitleReading("R", .99), TitleReading("Long Song With Expected Ending", .8), "missing", "fixed-roi"),
    (TitleReading("Expected", .83), TitleReading("Long Song With Expected Ending", .8), "confirmed", "fixed-roi"),
    # 片假名/平假名混淆不能改变「包含」判定：窄框是宽框的真子串，应采纳宽框
    # （实测曲目 94「わちゃ・もちゃ・ぺったん行進曲」窄框读成「・もちゃ・ペったん」）。
    (TitleReading("・もちゃ・ペったん", .93), TitleReading("わちゃ・もちゃ・ぺったん行進曲", .9), "missing", "wide-roi"),
    # 封面淡入时只读到短原文「怪」，下一帧读全「怪獣」，应视为补全采纳
    # （实测曲目 733「怪獣」；旧判据要求原文≥四字会把这次重读丢掉）。
    (TitleReading("怪", .99), TitleReading("怪獣", .9), "missing", "wide-roi"),
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
