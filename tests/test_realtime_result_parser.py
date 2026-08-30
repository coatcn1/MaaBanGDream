from __future__ import annotations

import base64
import zlib

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


def test_result_parser_rejects_impossible_fast_slow_total():
    result = LiveResult(10, 0, 0, 0, 0, 8, 5)
    with pytest.raises(ValueError, match="结算统计不一致"):
        ResultParser._validate_result(result)
