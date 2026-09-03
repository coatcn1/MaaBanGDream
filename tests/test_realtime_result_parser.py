from __future__ import annotations

import base64
import zlib

import cv2
import numpy as np
import pytest

from agent.realtime.result_parser import LiveResult, ResultParser, adjusted_timing_offset
from agent.realtime.result_samples_v2 import RESULT_CROPS_V2_ZLIB_BASE64
from agent.realtime.result_samples_v6 import (
    RESULT_CROPS_V6_ZLIB_BASE64,
    RESULT_CROPS_V7_ZLIB_BASE64,
)


LOW_CONFIDENCE_REAL_CROPS = (
    "eNrtWtGOwzAI4/9/mtOkqQ1gDEk2abuFlyp3dZcmYMCNyLH/bqp2pGZwjfUy+188tDerB7OhvVkR9vl3ua9h9LgGqNzzGd81PEKvC1gnc7dcN8v9+wFqsfhN/Jzb2BsA3ldaWJnFSvKz110yNWez3AT7BA1YGbaMr5XGu4cteyVWCNb6fOWTIIAM1IHMxWysKFjnXgwq2l8W+5VfiQ8Q8CC8ZA0qs4HTRx77tVyniOGjKwIvzTw63gF4AvF7zGcOIAIzoUtgIP4db+VYxLMmH5ow1YIrOZblFcPlKnNYgXvi65oOFi/z+7ECy5UmdnGtsqk2sOnyJn7lsUqxPiU6rC/SaBzhGNSN+NWX8MYWXx079oqcmJMI5iccNFUUKe0maYNI+sOiCh67K5QHtdER4o6zYMlhiPJggR1eDM2VdkgfgpWPn3O9zr39rbFMO8j9irl1hqXqSCo48Bjkig5Vg9KaKCQzNhJNe7xjx75CVW1prJWqWsTRTvxu8EbKV9M5dI0n9/n55N/3zXlnnRv7q7y+WqnrdurJrTp2p37eqtv3+oVjfXE6szdij1X5Oa4hamtxAIBPfVUM88Y7S9HxqaikqJRgXHpPKMEJtqUEZyV/pQTHtdKDbWNlD6vb2EQJnsAGJbiMI6IE1zG4Gr+8BxaOLRNA2h0fO3bsV2tKYbpZlPZcQk6YKzvQgY4oUSUfYEGb5j714q9ttKUDXeNI2Ak2e5bheiYsBMnC0XuUV9ijbIppYgVlmHmsbmJlDavTWPBFdx6r+DBDyzfiN+e+T5KDH0Us4EMGrRhE1SSTA+KjGOekFSPurGbI7fD7sY9UcWjSrMJMWcAW4a2YNxR+AaxzaMJbCo9Cz+XQ0H31c6g7gzzF7eHnJvJRqJDWsLqD1RXsuFXTayX3EeEd7Mr+Wmz0K6wiIL9a8Wcnt0GdID/51YtfwWo9PT+G1cfFRPrlCvEfH1KGPw=="
)

MISREAD_377_PERFECT_CROP = (
    "eNrtlFEOwCAIQ7n/pbtkmRulFv01GV8yfBTBGfHb4Qbk9W1p+X5gN23MocrBueEy"
    "Wu4BBjeAmqZmHQV+eKTKxQvi4hWQnXNsj4uek1F4OeHAmJNL/pRzcpbDQq4/n5d"
    "TjrLa05U5lGZvcVpe00zw2HnuHQe+n+leL7j+T3Uzh7R9S65EIWmml2P62FCaEj"
    "zv8bwA3q8YBQ=="
)

MISREAD_HARD_RESULT_CROPS = (
    "eNrtWsuC4zAI0///tPawjwSQwG62M7M75tKkqRLHBgSqgWP/v5EMJ7x/cTtOV/KP"
    "+cduV82l21fx+q9fpKME/nnI64PX4Mpdypiuz/h8yBHEF7wOJmydAcQHI4+9wf"
    "ZrwfBSecyoA1DY+33z0zIWEsslrHpuWV6Iu4gpku5qndr4pIiD3wuKZjXrywvfg"
    "D6IU976ZMXq4BUxELA0cdTEYI8dAq3HhjW0MQqDBYzXBQfACD323WiuI7PmsuAm"
    "yWUmV13nNBx5jxYdOwLLe+iLDyZySikFFaTIiXFsisrQYaGeJ7F1zBmLHsvynl"
    "zCoizRK1jqOmYaM8T6ztic30X9tIZl/dWAvTNqV4FYv0oLPGDjqkSfLAWHjCMOc"
    "fQofh/ljY18dZjs2PtYsXzTnZmw6eNopVdU19s6VZTADQ+mfmvMV/VsnQcRG7B"
    "dLAylrvBCxu5wCjSl4iOxD8as6hwszrP1iXF95zoHGhtZESs8aHpFtDwoHbnOl"
    "YvBpV4R7MrrPvZj21bPwKbfO3bsK7Jqd1YlCqILo4FC+YBCd9MGDNTzLmoVr1W"
    "zNfo12C49wwjIy/SbFuEVKtvF8gl1b9Eg9cUXKNQomvtYLlModijU0S+lVD+Xk"
    "+sUWmOQnkLn8lnKoqtl+zNZ9LDvM/nR2Buxx1oX/ko6sCDoBezRgf+6DsyP14H"
    "5SAfm5+rAhXVSxTPowCsacqoCv48OzKMDHzt2TOcGIzvY/mLWGUR56U8aZcEQo"
    "0j1bq9IqDzanq6m3VxEKKzZJFRIxisLZWYzFhOWT7F4CSv3cb0fy6dYvAErSxX"
    "oTVca63xSt+jBodn4ZvXOUoDonl62NDkizd94Zo+nUWugZ8fImX0yG9PbsWOfo"
    "9qgq+vRF/3yb6817c92HyUT2Q2XpekZRPu622YgYNFy+SZPJ+gJOxJhGLPLai0B"
    "11txkQhV/bGOrcPYGHPZjcy9ucp1H/o1yhOtt+xK6SBi9Z8x1a9UZaW3bMt7UP"
    "lxisGyz9vEkS1r0YsFWutX3K7zxq7SyzYH/Wv2A+g1Nqs="
)

MISREAD_HARD_GREAT_81_CROPS = (
    "eNrtW9tWwzAM0///tHjgnC225UvaAQOSF9aL1sy1LVsJwBl/fZAMJ7h8pLmw3kx/"
    "YrmfXK/SHvrLGZbmgGbCK/h5+fODwbpvdr+ZFmtBxhoPjJ8m7M1XsRhiWWOd3Y"
    "PNCI0N78hieRcLXJmz+6EWS/F+Pfb53QJr3N3YmXFYLF6ODe7ubKbmDP98BGdc"
    "bWRspWLQYlUMMo196NiPUBe87oTOE00G08lsAD3jn1EdM2cJl2u+Er4fIwsyAG"
    "LIwaf9JHhEGIksoOhMhqB9vEvYGGAfd6dYCCxSrKPgYKsWm9uZJRYqvQ2xd55"
    "Lw2Nbv1dywRUsFbZ8v5hgK7+aYe0fNr5RxVHCxa+J3628sUVIDPnqpPAzvoIUi"
    "xNZk6aKsyKKnDd3DWHSPqJoCFsWHDSEOnmpzNJkSZhWiLtY0QGt00GfnX8ci7"
    "efc2/n2fvtsdDYxq8qt86w+rwEOa0ij0FMBB2wYMFBU1cdQUtMZ5zxvqRaHQU"
    "GbcKooVDeoNBraSNCc95FrOGTRmNEv6MmRfQduntvaUFVKlM6UrbaorJCy+2fm"
    "+qiLda3lVMKTRu5XSzHFApLYftlHTMKHZSTvaaaxyBzTbUvnweaal62c6Splj3"
    "pye+XxcdkfCH2jB1NuFRzukCaBHDewxLielw5SvWovl/gjf6XN/vfq8Twkl6S3"
    "4vFbfK+iIXQOXcI2FcA0wJLLQGuFU9T2OFi/xs14XI1pSlk6/htFzXH/e+txH"
    "1S/hlnnCEaJ6szMO34OZQZZKtCzLqWFIt094pcpFu3wjRyA/N2RbNRpizYUq/k"
    "vaQKf5gdI6zUN6dYSb9TLGNh22MVu6HHwi/+OuZGX18Eudo5/hRLiS0UKejl3"
    "+CLYsEazLBdLFg7q0Kl0eb1ivWo6ElXu/P30e+S2VuIPFXOGe+i2mj9a9ICTPT"
    "4LgrjIcp9w/n+aY5Y1MkEz0OlFajpUMyuYZUK2zASoHs9uWk8k5wvYqlsNcJm"
    "1VmPTbN9j2Va6XV2RrJoG94v0ooj1KmdX4nKLm4v7PzZhcISpXJDti5cpbIyUP"
    "eSKlH+I4WQenflgmRN8Ndm3w/uR7sY"
)

MISREAD_EXPERT_810_BAD_TEN_CROPS = (
    "eNrtmt12wyAMg/X+L+3dbCeAJdmh6c5+4GZNl68EsC1jApz2x1vE8PGrTRcx3ILr"
    "YvrXfHNQmNy+x35eJzRfX999fh7GsAyM9LQMcZiANirY1DdDHbssyU12XvLpZ9"
    "LI8qKo5XTsvAy5W/BnxmgiUB8Fm8fIJuqyHctGm829ZWvM7DhVY28FG8sSTfPMr"
    "JT7YFpeauDPsGJMsKyKXYsx2/B22j8Ut2DGEjqOhrH2gJUnFhpraVvCn3pKxg43"
    "Lo40PS2YbngWSR8ZO8M+5IGyy1zR24l/s6CcFjDusFoWl5RHskG7bT4zj34Vq/"
    "SpN89MbSqWSm8wjwvayytsuEcekzrGUiNNq1D4r8gZyyzXsDpecXc97bQ3bPpMs"
    "mauyEaG+BGN5mIrwhzPyOLqN+xP/hkuZkuES9IGEj6SEOIZlmbxEwvDpmyC5Qw1"
    "C5kzvJelOVKPDbq+N1ih9x0WmsUuS826eOZFCD0bJN1SZZpijVbfb6TTYXZqPCH"
    "JP3vaaT9+60msO7Q9d/0Iagur/Dcq/c1hSelv2uGhrb9aB/GohtLU+yld2Gbju1"
    "g81++3aejLbDOvo6UKYtCVhqLWUFHvgavl7+XPL+Xt/f0CjvxuC4Jub2RPk1Op"
    "pFrIr6vBMs9SpyT0nOSq8ib/TqxICdQfUoIGmmGSFJFFnZBNSbzKNurAXOJ0MXe"
    "bbUmoCpTvZrHHhmejYlGx00HlLhtuuGAvKMyiLqvkuJE+69p7FTcabFd8+RennX"
    "baKTPkcyJfkVWvdulqQbA4KUIhT7dyfGYplOoWorCQRuTPFuXhg2ChivOUjaIe3"
    "NwrenZ+5YqccW6xUbFL9Ye8aABb2J+/mO4o2RAHTmyNJBtQeTP8Jv1rztrsOrH"
    "ZvtYza50n5DPz0gfl+hK3K94rULl7O+aY+KVzm7O5Pe2nlAsgd6X6bEoIbtT/1"
    "IIbJGyQiMyLByRckVpBjtRX/ILYj/GaaL4abjZsGhk8C4itXpIUV7bZY2ObnZfo"
    "DqvfuCtZnud05hmmjL2e4pijGMNmuzIVCm6Ttva1slheWp8WMrwf8bM1Weuj+ss"
    "rB7xO2JRC+5b+L2wfNYgEzw=="
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
    assert result.confidence >= .47


def test_result_parser_reads_three_hundred_seventy_seven_variant():
    binary = np.frombuffer(
        zlib.decompress(base64.b64decode(MISREAD_377_PERFECT_CROP)),
        dtype=np.uint8,
    ).reshape(32, 56)
    crop = (255 - binary)[:, :, None].repeat(3, axis=2)

    value, _confidence = ResultParser._read_digits(crop)

    assert value == 377


def test_result_parser_reads_hard_result_nine_variants():
    image = _result_image(MISREAD_HARD_RESULT_CROPS)

    result = ResultParser().parse(image)

    assert result == LiveResult(
        perfect=312,
        great=89,
        good=8,
        bad=5,
        miss=89,
        fast=70,
        slow=32,
        confidence=result.confidence,
    )


def test_result_parser_reads_hard_great_eighty_one_variant():
    image = _result_image(MISREAD_HARD_GREAT_81_CROPS)

    result = ResultParser().parse(image)

    assert result == LiveResult(
        perfect=408,
        great=81,
        good=0,
        bad=5,
        miss=9,
        fast=57,
        slow=29,
        confidence=result.confidence,
    )


def test_expected_chart_total_resolves_bad_ten_vote_regression():
    image = _result_image(MISREAD_EXPERT_810_BAD_TEN_CROPS)
    parser = ResultParser()

    uncorrected = parser.parse(image)
    corrected = parser.resolve_expected_total(
        image,
        expected_notes=810,
        fallback=uncorrected,
    )

    assert uncorrected.total == 800
    assert uncorrected.bad == 0
    assert corrected == LiveResult(
        perfect=574,
        great=137,
        good=18,
        bad=10,
        miss=71,
        fast=145,
        slow=20,
        confidence=corrected.confidence,
    )
    assert corrected.total == 810


PERFECT_856_READ_AS_556_CROP = (
    "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"
)


def test_expected_total_repair_includes_far_glyph_eight():
    # 真机实测：真值 8 与样本距离为最近标签的 2.94 倍、排第 6 近。
    # 候选必须按距离倍数收集，否则 856 会被读成 556，866 谱面会
    # 因总数不符在结算页卡到超时。
    crop = cv2.imdecode(
        np.frombuffer(
            base64.b64decode(PERFECT_856_READ_AS_556_CROP),
            dtype=np.uint8,
        ),
        cv2.IMREAD_COLOR,
    )
    candidates = ResultParser._field_candidates(crop, expected_notes=866)
    by_value = {value: changes for value, _cost, changes in candidates}

    assert 856 in by_value
    assert by_value[856] <= 1


@pytest.mark.parametrize(
    ("payload", "expected"),
    (
        (
            RESULT_CROPS_V6_ZLIB_BASE64,
            (568, 95, 6, 3, 41, 64, 40),
        ),
        (
            RESULT_CROPS_V7_ZLIB_BASE64,
            (565, 85, 4, 5, 54, 43, 51),
        ),
    ),
)
def test_result_parser_reads_expert_713_note_variants(payload, expected):
    result = ResultParser().parse(_result_image(payload))

    assert (
        result.perfect,
        result.great,
        result.good,
        result.bad,
        result.miss,
        result.fast,
        result.slow,
    ) == expected
    assert result.total == 713


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


def test_timing_adjustment_uses_small_step_for_sparse_feedback():
    sparse_slow = LiveResult(760, 6, 0, 0, 0, 0, 7)
    sparse_fast = LiveResult(760, 6, 0, 0, 0, 6, 0)

    assert adjusted_timing_offset(39, sparse_slow) == 42
    assert adjusted_timing_offset(41, sparse_fast) == 38


def test_timing_adjustment_takes_large_step_for_heavy_one_sided_bias():
    # 2026-09-04 排练实测 slow=506/fast=51：整局几乎全慢，必须一次追平
    # 会话级延迟，不能再用 ±12ms 小步长让正式验证带着旧偏移死亡。
    heavily_slow = LiveResult(60, 384, 169, 4, 64, 51, 506)
    assert adjusted_timing_offset(0, heavily_slow) == 39
    heavily_fast = LiveResult(60, 384, 169, 4, 64, 506, 51)
    assert adjusted_timing_offset(0, heavily_fast) == -39


def test_result_parser_rejects_impossible_fast_slow_total():
    result = LiveResult(10, 0, 0, 0, 0, 8, 5)
    with pytest.raises(ValueError, match="结算统计不一致"):
        ResultParser._validate_result(result)
