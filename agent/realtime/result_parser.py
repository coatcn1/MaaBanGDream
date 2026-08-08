from __future__ import annotations

from dataclasses import asdict, dataclass
import base64
import zlib

import cv2
import numpy as np

from .digit_templates import DIGIT_SAMPLES_ZLIB_BASE64
from .result_samples_v2 import (
    RESULT_CROPS_V2_LABELS,
    RESULT_CROPS_V2_ZLIB_BASE64,
)
from .result_samples_v3 import (
    PERFECT_377_CROP_ZLIB_BASE64,
    PERFECT_377_LABELS,
)


@dataclass(frozen=True)
class LiveResult:
    perfect: int
    great: int
    good: int
    bad: int
    miss: int
    fast: int
    slow: int
    confidence: float = 1.0

    @property
    def total(self) -> int:
        return self.perfect + self.great + self.good + self.bad + self.miss

    @property
    def hit_rate(self) -> float:
        return (self.total - self.miss) / self.total if self.total else 0.0

    def to_dict(self) -> dict:
        data = asdict(self)
        data["hit_rate"] = self.hit_rate
        return data


SEGMENTS = {
    0: (1, 1, 1, 0, 1, 1, 1),
    1: (0, 0, 1, 0, 0, 1, 0),
    2: (1, 0, 1, 1, 1, 0, 1),
    3: (1, 0, 1, 1, 0, 1, 1),
    4: (0, 1, 1, 1, 0, 1, 0),
    5: (1, 1, 0, 1, 0, 1, 1),
    6: (1, 1, 0, 1, 1, 1, 1),
    7: (1, 0, 1, 0, 0, 1, 0),
    8: (1, 1, 1, 1, 1, 1, 1),
    9: (1, 1, 1, 1, 0, 1, 1),
}


class ResultParser:
    """Parse the fixed 1280x720 result panel without an external OCR runtime."""

    # A field is the mean of four independently classified digits.  Values below
    # this are genuinely ambiguous in captured samples; 0.30-0.55 is common for
    # correctly read 8/5 glyphs because their antialiasing changes with the card.
    MIN_FIELD_CONFIDENCE = .30

    # x1, y1, x2, y2. Each field contains four monospaced digital glyphs.
    FIELDS = {
        "perfect": (860, 279, 916, 311),
        "great": (860, 320, 916, 352),
        "good": (860, 362, 916, 394),
        "bad": (860, 402, 916, 434),
        "miss": (860, 439, 916, 471),
        "fast": (1105, 279, 1165, 311),
        "slow": (1105, 320, 1165, 352),
    }
    _raw_samples = np.frombuffer(
        zlib.decompress(base64.b64decode(DIGIT_SAMPLES_ZLIB_BASE64)), dtype=np.uint8
    ).reshape(-1, 641)
    _labels = _raw_samples[:, 0]
    _samples = _raw_samples[:, 1:].reshape(-1, 32, 20)

    def parse(self, image: np.ndarray) -> LiveResult:
        if image.shape[:2] != (720, 1280):
            raise ValueError(f"结算截图尺寸必须为1280x720，实际为{image.shape[:2]}")
        values: dict[str, int] = {}
        confidences: list[float] = []
        for name, (x1, y1, x2, y2) in self.FIELDS.items():
            value, confidence = self._read_digits(image[y1:y2, x1:x2])
            values[name] = value
            confidences.append(confidence)
        result = LiveResult(**values, confidence=min(confidences))
        self._validate_result(result)
        return result

    @staticmethod
    def _validate_result(result: LiveResult) -> None:
        if result.total <= 0 or result.confidence < ResultParser.MIN_FIELD_CONFIDENCE:
            raise ValueError(f"结算数字识别置信度不足: {result.confidence:.2f}")
        if result.fast + result.slow > result.total:
            raise ValueError(
                f"结算统计不一致: FAST+SLOW={result.fast + result.slow}, 总音符={result.total}"
            )

    @staticmethod
    def _read_digits(crop: np.ndarray) -> tuple[int, float]:
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        # Result digits are light gray on an almost-white card.
        binary = cv2.threshold(gray, 238, 255, cv2.THRESH_BINARY_INV)[1]
        width = binary.shape[1]
        digits: list[str] = []
        confidences: list[float] = []
        for index in range(4):
            left = round(index * width / 4)
            right = round((index + 1) * width / 4)
            cell = binary[:, left:right]
            normalised = ResultParser._normalise_glyph(cell)
            digit, confidence = ResultParser._classify_glyph(normalised)
            digits.append(str(digit))
            confidences.append(confidence)
        return int("".join(digits)), float(np.mean(confidences))

    @staticmethod
    def _classify_glyph(normalised: np.ndarray) -> tuple[int, float]:
        distances = np.mean(
            (ResultParser._samples.astype(np.float32) - normalised.astype(np.float32)) ** 2,
            axis=(1, 2),
        )
        best_index = int(np.argmin(distances))
        digit = int(ResultParser._labels[best_index])
        other = distances[ResultParser._labels != digit]
        second = float(np.min(other)) if len(other) else float(distances[best_index] + 1)
        confidence = max(0.0, 1.0 - float(distances[best_index]) / max(second, 1.0))
        return digit, confidence

    @staticmethod
    def _normalise_glyph(glyph: np.ndarray) -> np.ndarray:
        ys, xs = np.nonzero(glyph)
        if len(xs):
            glyph = glyph[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        output = np.zeros((32, 20), dtype=np.uint8)
        scale = min(18 / glyph.shape[1], 30 / glyph.shape[0])
        resized = cv2.resize(
            glyph,
            (max(1, round(glyph.shape[1] * scale)), max(1, round(glyph.shape[0] * scale))),
        )
        y = (32 - resized.shape[0]) // 2
        x = (20 - resized.shape[1]) // 2
        output[y:y + resized.shape[0], x:x + resized.shape[1]] = resized
        return output.astype(np.float32)

    @staticmethod
    def _segment_scores(glyph: np.ndarray) -> np.ndarray:
        regions = (
            glyph[1:5, 5:15],    # top
            glyph[6:14, 1:5],    # upper left
            glyph[6:14, 15:19],  # upper right
            glyph[14:18, 5:15],  # middle
            glyph[19:27, 1:5],   # lower left
            glyph[19:27, 15:19], # lower right
            glyph[27:31, 5:15],  # bottom
        )
        return np.asarray([np.count_nonzero(region) / region.size for region in regions])


def _install_result_samples_v2() -> None:
    """Add the current game's thin-digit rendering to the nearest-neighbour set."""
    crops = np.frombuffer(
        zlib.decompress(base64.b64decode(RESULT_CROPS_V2_ZLIB_BASE64)), dtype=np.uint8
    ).reshape(7, 32, 60)
    samples: list[np.ndarray] = []
    labels: list[int] = []
    for crop, field, field_labels in zip(
        crops, ResultParser.FIELDS.values(), RESULT_CROPS_V2_LABELS
    ):
        width = field[2] - field[0]
        binary = crop[:, :width]
        for index, label in enumerate(field_labels):
            left = round(index * width / 4)
            right = round((index + 1) * width / 4)
            samples.append(ResultParser._normalise_glyph(binary[:, left:right]))
            labels.append(label)
    ResultParser._samples = np.concatenate(
        (ResultParser._samples, np.asarray(samples, dtype=np.float32)), axis=0
    )
    ResultParser._labels = np.concatenate(
        (ResultParser._labels, np.asarray(labels, dtype=np.uint8)), axis=0
    )


_install_result_samples_v2()


def _install_result_samples_v3() -> None:
    crop = np.frombuffer(
        zlib.decompress(base64.b64decode(PERFECT_377_CROP_ZLIB_BASE64)),
        dtype=np.uint8,
    ).reshape(32, 56)
    samples: list[np.ndarray] = []
    for index in range(4):
        left = round(index * crop.shape[1] / 4)
        right = round((index + 1) * crop.shape[1] / 4)
        samples.append(ResultParser._normalise_glyph(crop[:, left:right]))
    ResultParser._samples = np.concatenate(
        (ResultParser._samples, np.asarray(samples, dtype=np.float32)), axis=0
    )
    ResultParser._labels = np.concatenate(
        (ResultParser._labels, np.asarray(PERFECT_377_LABELS, dtype=np.uint8)),
        axis=0,
    )


_install_result_samples_v3()


def adjusted_timing_offset(current: int, result: LiveResult) -> int:
    feedback = result.fast + result.slow
    error = result.slow - result.fast
    threshold = max(2, round(feedback * .10))
    if feedback == 0 or abs(error) <= threshold:
        return int(current)
    delta = round(12 * error / feedback)
    delta = max(-15, min(15, delta or (1 if error > 0 else -1)))
    return max(-250, min(250, int(current) + delta))
