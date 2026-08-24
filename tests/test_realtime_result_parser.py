from __future__ import annotations

import base64
import zlib

import numpy as np
import pytest

from agent.realtime.result_parser import LiveResult, ResultParser, adjusted_timing_offset
from agent.realtime.result_samples_v2 import RESULT_CROPS_V2_ZLIB_BASE64


LOW_CONFIDENCE_REAL_CROPS = (
    "eNrtWtGOwzAI4/9/mtOkqQ1gDEk2abuFlyp3dZcmYMCNyLH/bqp2pGZwjfUy+188tDerB7OhvVkR9vl3ua9h9LgGqNzzGd81PEKvC1gnc7dcN8v9+wFqsfhN/Jzb2BsA3ldaWJnFSvKz110yNWez3AT7BA1YGbaMr5XGu4cteyVWCNb6fOWTIIAM1IHMxWysKFjnXgwq2l8W+5VfiQ8Q8CC8ZA0qs4HTRx77tVyniOGjKwIvzTw63gF4AvF7zGcOIAIzoUtgIP4db+VYxLMmH5ow1YIrOZblFcPlKnNYgXvi65oOFi/z+7ECy5UmdnGtsqk2sOnyJn7lsUqxPiU6rC/SaBzhGNSN+NWX8MYWXx079oqcmJMI5iccNFUUKe0maYNI+sOiCh67K5QHtdER4o6zYMlhiPJggR1eDM2VdkgfgpWPn3O9zr39rbFMO8j9irl1hqXqSCo48Bjkig5Vg9KaKCQzNhJNe7xjx75CVW1prJWqWsTRTvxu8EbKV9M5dI0n9/n55N/3zXlnnRv7q7y+WqnrdurJrTp2p37eqtv3+oVjfXE6szdij1X5Oa4hamtxAIBPfVUM88Y7S9HxqaikqJRgXHpPKMEJtqUEZyV/pQTHtdKDbWNlD6vb2EQJnsAGJbiMI6IE1zG4Gr+8BxaOLRNA2h0fO3bsV2tKYbpZlPZcQk6YKzvQgY4oUSUfYEGb5j714q9ttKUDXeNI2Ak2e5bheiYsBMnC0XuUV9ijbIppYgVlmHmsbmJlDavTWPBFdx6r+DBDyzfiN+e+T5KDH0Us4EMGrRhE1SSTA+KjGOekFSPurGbI7fD7sY9UcWjSrMJMWcAW4a2YNxR+AaxzaMJbCo9Cz+XQ0H31c6g7gzzF7eHnJvJRqJDWsLqD1RXsuFXTayX3EeEd7Mr+Wmz0K6wiIL9a8Wcnt0GdID/51YtfwWo9PT+G1cfFRPrlCvEfH1KGPw=="
)

MISREAD_377_PERFECT_CROP = (
    "eNrtlFEOwCAIQ7n/pbtkmRulFv01GV8yfBTBGfHb4Qbk9W1p+X5gN23MocrBueEy"
    "Wu4BBjeAmqZmHQV+eKTKxQvi4hWQnXNsj4uek1F4OeHAmJNL/pRzcpbDQq4/n5d"
    "TjrLa05U5lGZvcVpe00zw2HnuHQe+n+leL7j+T3Uzh7R9S65EIWmml2P62FCaEj"
    "zv8bwA3q8YBQ=="
)


def _result_image(payload: str) -> np.ndarray:
    crops = np.frombuffer(
        zlib.decompress(base64.b64decode(payload)), dtype=np.uint8
    ).reshape(7, 32, 60)
    image = np.full((720, 1280, 3), 255, dtype=np.uint8)
    for crop, (x1, y1, x2, y2) in zip(crops, ResultParser.FIELDS.values()):
        binary = crop[:, :x2 - x1]
        image[y1:y2, x1:x2] = (255 - binary)[:, :, None]
    return image


def test_result_parser_embedded_samples_classify_all_digits():
    for expected in range(10):
        samples = ResultParser._samples[ResultParser._labels == expected]
        assert len(samples) > 0
        for sample in samples:
            digit, confidence = ResultParser._classify_glyph(sample)
            assert digit == expected
            assert confidence >= .55


def test_result_parser_reads_latest_real_result_rendering():
    assert ResultParser().parse(_result_image(RESULT_CROPS_V2_ZLIB_BASE64)) == LiveResult(
        perfect=117, great=11, good=0, bad=0, miss=8, fast=4, slow=7,
        confidence=1.0,
    )


def test_result_parser_accepts_consistent_real_result_with_soft_glyphs():
    result = ResultParser().parse(_result_image(LOW_CONFIDENCE_REAL_CROPS))
    assert (result.perfect, result.great, result.good, result.bad, result.miss) == (
        85, 13, 0, 0, 11,
    )
    assert (result.fast, result.slow) == (7, 6)
    assert result.confidence == pytest.approx(.4739495)


def test_result_parser_reads_three_hundred_seventy_seven_variant():
    binary = np.frombuffer(
        zlib.decompress(base64.b64decode(MISREAD_377_PERFECT_CROP)),
        dtype=np.uint8,
    ).reshape(32, 56)
    crop = (255 - binary)[:, :, None].repeat(3, axis=2)

    value, _confidence = ResultParser._read_digits(crop)

    assert value == 377


def test_timing_adjustment_moves_toward_balanced_feedback():
    slow = LiveResult(80, 20, 0, 0, 5, 5, 25)
    fast = LiveResult(80, 20, 0, 0, 5, 25, 5)
    balanced = LiveResult(80, 20, 0, 0, 5, 11, 10)

    assert adjusted_timing_offset(10, slow) > 10
    assert adjusted_timing_offset(10, fast) < 10
    assert adjusted_timing_offset(10, balanced) == 10


def test_timing_adjustment_step_uses_wider_bounds():
    slow = LiveResult(80, 20, 0, 0, 5, 0, 20)
    fast = LiveResult(80, 20, 0, 0, 5, 20, 0)
    extreme = LiveResult(80, 20, 0, 0, 5, 30, 0)

    assert adjusted_timing_offset(0, slow) == 12
    assert adjusted_timing_offset(0, fast) == -12
    assert adjusted_timing_offset(0, extreme) == -12


def test_result_parser_rejects_impossible_fast_slow_total():
    result = LiveResult(10, 0, 0, 0, 0, 8, 5)
    with pytest.raises(ValueError, match="结算统计不一致"):
        ResultParser._validate_result(result)
