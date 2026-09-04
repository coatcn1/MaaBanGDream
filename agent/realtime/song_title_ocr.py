"""Local single-line OCR for song titles shown by the game UI."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = PROJECT_ROOT / "resource" / "models" / "song_title_ocr"
MODEL_PATH = MODEL_DIR / "inference.onnx"
CONFIG_PATH = MODEL_DIR / "inference.yml"

# Current-song title on the 1280x720 free-live selection screen.  The
# recognizer itself accepts any ROI so multiplayer can supply its own title
# location without duplicating model or matching logic.
SINGLE_LIVE_TITLE_ROI = (120, 260, 440, 90)


@dataclass(frozen=True, slots=True)
class TitleReading:
    text: str
    confidence: float


def normalize_song_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(
        character for character in normalized
        if unicodedata.category(character)[0] in {"L", "N"}
    )


@lru_cache(maxsize=8192)
def title_similarity(observed: str, expected: str) -> float:
    left = normalize_song_title(observed)
    right = normalize_song_title(expected)
    if not left or not right:
        return 0.0
    best = float(SequenceMatcher(None, left, right).ratio())
    # 固定宽度 OCR 常把标题右/左侧的难度或提示文字一并解码（例如把省略号
    # “…”读成“今の”），也可能裁掉首尾字符。用所有前缀/后缀与候选曲名的
    # 最高相似度容忍这类首尾噪声，避免标题本可确认却整局回退 Legacy。
    for end in range(1, len(left)):
        best = max(
            best,
            float(SequenceMatcher(None, left[:end], right).ratio()),
        )
    for start in range(1, len(left)):
        best = max(
            best,
            float(SequenceMatcher(None, left[start:], right).ratio()),
        )
    return best


def _load_characters(path: Path) -> tuple[str, ...]:
    characters: list[str] = []
    inside_dictionary = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() == "character_dict:":
            inside_dictionary = True
            continue
        if not inside_dictionary:
            continue
        if line.startswith("  - "):
            characters.append(line[4:])
            continue
        if line and not line.startswith(" "):
            break
    if not characters:
        raise ValueError(f"song title OCR dictionary is empty: {path}")
    # Paddle CTCLabelDecode adds a trailing space token after the configured
    # dictionary; class zero remains the blank token.
    characters.append(" ")
    return tuple(characters)


@lru_cache(maxsize=1)
def _runtime():
    if not MODEL_PATH.is_file() or not CONFIG_PATH.is_file():
        raise FileNotFoundError("song title OCR model is not deployed")
    try:
        import onnxruntime as ort
    except ImportError as exc:
        raise RuntimeError("onnxruntime is required for song title OCR") from exc
    options = ort.SessionOptions()
    options.log_severity_level = 3
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(
        str(MODEL_PATH),
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )
    return session, _load_characters(CONFIG_PATH)


def recognize_song_title(
    image,
    roi: tuple[int, int, int, int] = SINGLE_LIVE_TITLE_ROI,
) -> TitleReading | None:
    """Recognize one fixed title line without text detection or network I/O."""
    if not isinstance(image, np.ndarray) or image.ndim != 3:
        return None
    x, y, width, height = map(int, roi)
    if (
        x < 0 or y < 0 or width <= 0 or height <= 0
        or image.shape[0] < y + height
        or image.shape[1] < x + width
    ):
        return None
    crop = image[y:y + height, x:x + width]
    resized_width = max(
        32,
        min(1280, int(np.ceil(48.0 * width / height))),
    )
    resized = cv2.resize(
        crop,
        (resized_width, 48),
        interpolation=cv2.INTER_LINEAR,
    ).astype(np.float32)
    normalized = (resized / 255.0 - 0.5) / 0.5
    tensor = np.transpose(normalized, (2, 0, 1))[None]
    session, characters = _runtime()
    output = session.run(
        None,
        {session.get_inputs()[0].name: tensor},
    )[0]
    if output.ndim != 3 or output.shape[0] != 1:
        return None
    indexes = output[0].argmax(axis=1)
    probabilities = output[0].max(axis=1)
    decoded: list[str] = []
    scores: list[float] = []
    previous = -1
    for raw_index, probability in zip(indexes, probabilities):
        index = int(raw_index)
        if index != 0 and index != previous:
            character_index = index - 1
            if 0 <= character_index < len(characters):
                decoded.append(characters[character_index])
                scores.append(float(probability))
        previous = index
    text = re.sub(r"\s+", " ", "".join(decoded)).strip()
    confidence = float(sum(scores) / len(scores)) if scores else 0.0
    if not normalize_song_title(text) or confidence < 0.45:
        return None
    return TitleReading(text=text, confidence=confidence)
