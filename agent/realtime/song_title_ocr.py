"""Local single-line OCR for song titles shown by the game UI."""

from __future__ import annotations

import re
import os
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
FINAL_COVER_TITLE_ROI = (480, 485, 318, 70)
FINAL_COVER_WIDE_TITLE_ROI = (300, 470, 680, 100)
FINAL_COVER_WIDE_TITLE_TRIAL_ENV = "MAABANGDREAM_FINAL_COVER_WIDE_TITLE_TRIAL"


def final_cover_wide_title_trial_enabled() -> bool:
    return os.environ.get(FINAL_COVER_WIDE_TITLE_TRIAL_ENV) == "1"


def is_final_score_layout(image) -> bool:
    """仅接受目标得分条的黄色星章和右侧粉色数字共同出现的页面证据。"""
    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[0] < 550 or image.shape[1] < 960:
        return False
    roi = image[485:550, 350:960, :3]
    # 开演前加载会经过压暗/淡入帧：实测目标得分帧数字区最大亮度仅 154（阈值
    # 160）、黄色星章几乎被压没，固定 V 阈值会漏判，把分数当曲名。先按 ROI 亮度
    # 归一到峰值——缩放只改 V，不改色相/饱和度——把亮度门槛变成相对判据。
    scale = 255.0 / max(1.0, float(np.percentile(roi.max(axis=2), 99)))
    if scale > 1.0:
        roi = np.clip(roi.astype(np.float32) * scale, 0, 255).astype(np.uint8)
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
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


def is_final_title_completion(previous: TitleReading | None, reading: TitleReading) -> bool:
    """高置信未知原文只能被完整包含它的读数替换（补全或未截断的重读）。"""
    if previous is None or previous.confidence < 0.9:
        return True
    original = normalize_song_title(previous.text)
    if not original:
        return True
    # 不再要求原文至少四字：封面淡入时可能只读到 1~3 字的截断原文（实测曲目 733
    # 「怪獣」首帧只读到「怪」，下一帧才读全），此时新读数完整包含原文就该允许替换；
    # 不含原文的无关标题仍被挡住（如 "R" 不属于 "Long Song With Expected Ending"）。
    return original in normalize_song_title(reading.text)


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


def recognize_final_cover_title(
    image, *, reader=None, diagnostics=None, identity_validator=None, wide_title_trial=False,
) -> TitleReading | None:
    """窄框优先；候选宽框只补身份未确认的标题，不能覆盖真实冲突。"""
    detail = diagnostics if diagnostics is not None else {}
    detail.update({"source": "fixed-roi", "source_roi": FINAL_COVER_TITLE_ROI,
                   "wide_title_trial": bool(wide_title_trial), "attempts": []})
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
    if wide_title_trial and identity_validator is not None:
        identity_status = identity_validator(reading) if reading is not None and reading.confidence >= 0.7 else "missing"
        detail["narrow_identity_status"] = identity_status
        if identity_status == "missing":
            wide = recognize(image, roi=FINAL_COVER_WIDE_TITLE_ROI)
            detail["attempts"].append({"roi": FINAL_COVER_WIDE_TITLE_ROI,
                                       "text": wide.text if wide else None,
                                       "confidence": wide.confidence if wide else None})
            if wide is not None and not is_final_score_text(wide.text) and wide.confidence >= 0.7:
                wide_status = identity_validator(wide)
                detail["wide_identity_status"] = wide_status
                # 走到这里说明窄框读数匹配不到任何本地曲目（identity_status ==
                # "missing"），它只是未被验证的候选。窄框比标题条窄，切掉首字是常见
                # 成因：实测曲目 324「るんっ♪ていていー！」标题宽约 348px，窄框只有
                # 318px，首个假名被切成半个读成「ら」。此时若宽框读数已通过完整身份
                # 交叉校验，且与窄框读数高度相似（同一标题的单字误读），应当替换。
                # 相似度门槛把「R 这类合法短标题被无关宽框读数顶掉」挡在外面：实测
                # 324 误读对 0.857，而 "R" 对无关长标题是 0.000。
                completes_narrow = is_final_title_completion(reading, wide)
                similar_narrow = (
                    reading is not None
                    and title_similarity(reading.text, wide.text) >= 0.75
                )
                if wide_status == "confirmed" and (completes_narrow or similar_narrow):
                    reading = wide
                    detail["source"] = "wide-roi"
                    detail["source_roi"] = FINAL_COVER_WIDE_TITLE_ROI
                elif not completes_narrow:
                    detail["wide_rejection"] = "high-confidence-title-is-not-a-completion"
    detail.update({"status": "read" if reading else "missing", "text": reading.text if reading else None,
                   "confidence": reading.confidence if reading else None})
    return reading


def _fold_katakana(character: str) -> str:
    """把片假名折成平假名，抵消 OCR 常见的假名混淆。"""
    code = ord(character)
    if 0x30A1 <= code <= 0x30F6:
        return chr(code - 0x60)
    return character


def normalize_song_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(
        _fold_katakana(character)
        for character in normalized
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


@lru_cache(maxsize=8192)
def strict_title_similarity(observed: str, expected: str) -> float:
    """不做首尾噪声裁剪的相似度。

    title_similarity 会用裁剪容忍 OCR 噪声，代价是：当候选标题是较长标题的
    真前缀时（如「灼熱 Bonfire!」之于「灼熱 Bonfire!(平行歌曲)」），它也能把
    较短者裁到满分，制造假歧义。裁剪切歧义时改用本函数复核。
    """
    left = normalize_song_title(observed)
    right = normalize_song_title(expected)
    if not left or not right:
        return 0.0
    return float(SequenceMatcher(None, left, right).ratio())


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
