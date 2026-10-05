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
# 开演前最终歌曲信息页里，封面下方浅灰长条内的标题文字。该页面在正式
# 开演前只展示两三秒，且字体比协力房间准备页清晰，适合作为谱面身份
# 解析的补充证据（Little Busters! 实测置信度 0.93+）。
#
# 浅灰长条横跨整个屏幕、标题在条内居中排布，长曲名实测可宽达 715px。旧值
# (480, 485, 318, 70) 只框住中间 318px，会把长标题首尾一起切掉：
# 「[超高难易度 新SPECIAL] SENSENFUKOKU」只读到「新SPECIAL']' SENS」，
# 与曲库相似度 0.649，差 0.68 阈值一点点就被判 song title does not match
# final cover；「めきエクスペリエンス！（月岛麻里奈ver.）」同样被截断。
# 这类标题的墨迹会触及 ROI 四边，`final_cover_ink_roi` 按设计退回固定 ROI，
# 所以收紧墨迹无法补救，只能放宽固定框本身。
#
# 放宽必须以屏幕中心 x=640 对称。420 张去重后的真实 final-cover 帧回归
# （旧值命中 119/420）：非对称 (330, 470, 750, 100) 是 +7/-7 净零，居中
# (300, 470, 680, 100) 是 +7/-1。两侧多读进来的 UI 文字会弄坏本来正确的
# 曲目，所以不是越宽越好。
FINAL_COVER_TITLE_ROI = (300, 470, 680, 100)


def is_final_score_layout(image) -> bool:
    """仅接受目标得分条的黄色星章和右侧粉色数字共同出现的页面证据。"""
    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[0] < 550 or image.shape[1] < 960:
        return False
    hsv = cv2.cvtColor(image[485:550, 350:960, :3], cv2.COLOR_BGR2HSV)
    badge = hsv[:, :70]
    digits = hsv[:, 380:610]
    yellow = (badge[:, :, 0] > 10) & (badge[:, :, 0] < 45) & (badge[:, :, 1] > 35) & (badge[:, :, 2] > 140)
    pink = (digits[:, :, 0] > 150) & (digits[:, :, 0] < 180) & (digits[:, :, 1] > 40) & (digits[:, :, 2] > 160)
    if np.count_nonzero(yellow) < 200 or np.count_nonzero(pink) < 200:
        return False
    _, _, stats, _ = cv2.connectedComponentsWithStats(pink.astype(np.uint8))
    return sum(int(area) >= 8 and int(height) >= 8 for _, _, _, height, area in stats[1:]) >= 3


def is_final_score_text(text: str) -> bool:
    normalized = normalize_song_title(text)
    return (
        ("得分" in normalized or "targetscore" in normalized)
        and sum(character.isdigit() for character in normalized) >= 3
    )


@dataclass(frozen=True, slots=True)
class TitleReading:
    text: str
    confidence: float


def final_cover_ink_roi(image) -> tuple[int, int, int, int] | None:
    """仅裁切最终标题条内完整的深色墨迹，触边长标题保持既有固定 ROI。"""
    x, y, width, height = FINAL_COVER_TITLE_ROI
    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[0] < y + height or image.shape[1] < x + width:
        return None
    gray = cv2.cvtColor(image[y:y + height, x:x + width, :3], cv2.COLOR_BGR2GRAY)
    ink_y, ink_x = np.where(gray < 180)
    if not len(ink_x):
        return None
    left, top = int(ink_x.min()), int(ink_y.min())
    right, bottom = int(ink_x.max()) + 1, int(ink_y.max()) + 1
    if left == 0 or top == 0 or right == width or bottom == height or bottom - top < 8 or right - left < 4:
        return None
    left, top = max(0, left - 16), max(0, top - 10)
    right, bottom = min(width, right + 16), min(height, bottom + 10)
    return x + left, y + top, right - left, bottom - top


def recognize_final_cover_title(image, *, reader=None, diagnostics=None) -> TitleReading | None:
    """最终页低置信度短标题最多追加一次同模型识读，保持既有确认阈值。"""
    detail = diagnostics if diagnostics is not None else {}
    detail.update({"source": "fixed-roi", "source_roi": FINAL_COVER_TITLE_ROI, "attempts": []})
    if is_final_score_layout(image):
        detail["status"] = "excluded-score-layout"
        return None
    recognize = reader or recognize_song_title
    reading = recognize(image, roi=FINAL_COVER_TITLE_ROI)
    detail["attempts"].append({"roi": FINAL_COVER_TITLE_ROI, "text": reading.text if reading else None,
                               "confidence": reading.confidence if reading else None})
    if reading is not None and is_final_score_text(reading.text):
        detail["status"] = "excluded-score-text"
        return None
    if reading is None or reading.confidence < 0.7:
        ink_roi = final_cover_ink_roi(image)
        if ink_roi is not None:
            enhanced = recognize(image, roi=ink_roi)
            detail["attempts"].append({"roi": ink_roi, "text": enhanced.text if enhanced else None,
                                       "confidence": enhanced.confidence if enhanced else None})
            if enhanced is not None and is_final_score_text(enhanced.text):
                detail["status"] = "excluded-score-text"
                return None
            if enhanced is not None and (reading is None or enhanced.confidence > reading.confidence):
                reading = enhanced
                detail["source"] = "ink-roi"
                detail["source_roi"] = ink_roi
    detail.update({"status": "read" if reading else "missing", "text": reading.text if reading else None,
                   "confidence": reading.confidence if reading else None})
    return reading


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
    max_noise = max(2, len(right) // 2)
    # 固定宽度 OCR 常把标题右/左侧的难度或提示文字一并解码（例如把省略号
    # “…”读成“今の”），也可能裁掉首尾字符。裁剪长度必须受候选曲名限制，
    # 不能把任意乱码截成一个字母后确认成《R》这样的短曲名。
    for end in range(1, len(left)):
        if len(left) - end > max_noise:
            continue
        best = max(
            best,
            float(SequenceMatcher(None, left[:end], right).ratio()),
        )
    for start in range(1, len(left)):
        if start > max_noise:
            continue
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
