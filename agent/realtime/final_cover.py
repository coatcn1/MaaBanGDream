"""开演前最终封面确认门控。"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
import unicodedata

import cv2
import numpy as np

from .chart_repository import LocalChartRepository
from .song_identity import (
    LOOSE_SAME_SONG_DISTANCE,
    MAX_SAME_SONG_DISTANCE,
    UNKNOWN_SONG_ID,
    detect_full_badge,
    identify_final_song,
    same_song,
)
from .song_title_ocr import (
    FINAL_COVER_TITLE_ROI, recognize_song_title, title_similarity,
    is_final_score_layout, is_final_score_text,
    recognize_final_cover_title,
    TitleReading, final_cover_wide_title_trial_enabled,
    is_final_title_completion,
)
from .vision_io import imread_unicode


MEMBER_LOADING_ICON_TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "resource" / "image" / "cooperative" / "member_loading_icon.png"
)
MEMBER_LOADING_ICON_THRESHOLD = 0.90


@lru_cache(maxsize=1)
def member_loading_icon() -> np.ndarray:
    template = imread_unicode(MEMBER_LOADING_ICON_TEMPLATE, cv2.IMREAD_COLOR)
    if template is None:
        # 启用保护后模板缺失必须失败，不能退回未经保护的身份确认。
        raise RuntimeError("协力成员加载页模板缺失或损坏")
    return template


def is_member_loading_screen(image: Any) -> bool:
    """全图寻找等待页表情图标，兼容玩家展开表情面板后图标上移。"""
    return bool(member_loading_score(image) >= MEMBER_LOADING_ICON_THRESHOLD)


def member_loading_score(image: Any) -> float:
    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] < 3:
        return 0.0
    template = member_loading_icon()
    if image.shape[0] < template.shape[0] or image.shape[1] < template.shape[1]:
        return 0.0
    result = cv2.matchTemplate(image[:, :, :3], template, cv2.TM_CCOEFF_NORMED)
    return float(cv2.minMaxLoc(result)[1])


@dataclass(frozen=True, slots=True)
class FinalCoverConfirmation:
    song_id: str
    song_id_method: str
    bestdori_song_id: int


@dataclass(frozen=True, slots=True)
class FinalCoverResolution:
    confirmation: FinalCoverConfirmation
    selection: Any
    observed_title: str | None = None
    observed_title_confidence: float = 0.0
    final_title_confirmed: bool = False


def _is_full_song(selection: Any) -> bool:
    """标题（任意语言/全角变体）是否带 [FULL] 前缀。"""
    titles = tuple(getattr(selection, "titles", ()))
    if not titles:
        titles = (str(getattr(selection, "title", "")),)
    for title in titles:
        normalized = unicodedata.normalize("NFKC", str(title)).casefold()
        if normalized.startswith("[full]"):
            return True
    return False


class FinalCoverGate:
    """封面只收窄候选，准备页等级、标题和难度负责消除歧义。"""

    def __init__(
        self,
        selection: Any,
        *,
        difficulty: str,
        observed_level: int | None,
        observed_title: str | None,
        allow_missing_level: bool = False,
        require_matching_title: bool = False,
    ) -> None:
        self.selection = selection
        self.difficulty = str(difficulty).strip().lower()
        self.observed_level = (
            None if observed_level is None else int(observed_level)
        )
        self.observed_title = (
            None if observed_title is None else str(observed_title).strip()
        )
        self.allow_missing_level = bool(allow_missing_level)
        self.require_matching_title = bool(require_matching_title)
        self.confirmed = False
        self.frames = 0
        self.last_reason = "final cover has not been observed"
        self._pending_full_badge = False

    def evidence_reason(self) -> str | None:
        expected_difficulty = str(
            getattr(self.selection, "difficulty", "")
        ).strip().lower()
        if expected_difficulty != self.difficulty:
            return "difficulty conflicts with selected chart"
        expected_level = getattr(self.selection, "level", None)
        if self.observed_level is None:
            if not self.allow_missing_level:
                return "preparation song level is missing"
        elif (
            expected_level is None
            or int(expected_level) != self.observed_level
        ):
            return "preparation song level conflicts with selected chart"
        if self.require_matching_title or bool(
            getattr(self.selection, "level_drift_tolerated", False)
        ):
            if not self.observed_title:
                return "final cover requires confirmed song title"
            titles = tuple(getattr(self.selection, "titles", ())) or (
                str(getattr(self.selection, "title", "")),
            )
            if max(
                (title_similarity(self.observed_title, title) for title in titles),
                default=0.0,
            ) < 0.68:
                return "final cover song title conflicts with selected chart"
        if bool(getattr(self.selection, "shared_jacket", False)):
            level_unique = bool(
                getattr(
                    self.selection,
                    "shared_jacket_level_unique",
                    False,
                )
            )
            if not level_unique:
                # 共享封面组内同等级还有别的谱面（如 HELL! or HELL? 与其
                # SPECIAL 版本同为 28），等级无法区分，必须依赖标题。
                if not self.observed_title:
                    return "shared jacket requires preparation song title"
                titles = tuple(getattr(self.selection, "titles", ()))
                if not titles:
                    titles = (str(getattr(self.selection, "title", "")),)
                score = max(
                    (
                        title_similarity(self.observed_title, title)
                        for title in titles
                    ),
                    default=0.0,
                )
                if score < 0.68:
                    # 标题 OCR 失败时，若是 FULL 谱面，留给封面右上角的
                    # FULL 徽标复核；非 FULL 仍按标题硬失败。
                    if _is_full_song(self.selection):
                        self._pending_full_badge = True
                    else:
                        return (
                            "preparation song title conflicts "
                            "with shared jacket"
                        )
        fingerprints = tuple(getattr(self.selection, "fingerprints", ()))
        if not fingerprints:
            return "selected chart has no confirmed jacket fingerprints"
        return None

    def observe(self, image: Any) -> FinalCoverConfirmation | None:
        self.frames += 1
        evidence_reason = self.evidence_reason()
        if evidence_reason is not None:
            self.last_reason = evidence_reason
            return None
        if self._pending_full_badge:
            if not detect_full_badge(image):
                self.last_reason = (
                    "shared jacket FULL badge not detected after "
                    "title OCR failure"
                )
                return None
            self._pending_full_badge = False
        identity = identify_final_song(image)
        if identity.song_id == UNKNOWN_SONG_ID:
            self.last_reason = "final cover jacket is not visible"
            return None
        fingerprints = tuple(getattr(self.selection, "fingerprints", ()))
        # 等级已放宽时不能再叠加封面宽容差；既有严格等级路径保留裁切兼容。
        max_distance = (
            MAX_SAME_SONG_DISTANCE
            if bool(getattr(self.selection, "level_drift_tolerated", False))
            else LOOSE_SAME_SONG_DISTANCE
        )
        if not any(
            same_song(
                identity.song_id,
                item,
                max_distance=max_distance,
            )
            for item in fingerprints
        ):
            self.last_reason = "final cover jacket does not match selected chart"
            return None
        self.confirmed = True
        self.last_reason = "confirmed"
        return FinalCoverConfirmation(
            song_id=identity.song_id,
            song_id_method=identity.method,
            bestdori_song_id=int(self.selection.bestdori_song_id),
        )


class FinalCoverResolver:
    """用准备页证据和最终封面解析或复核本地谱面。"""

    def __init__(
        self,
        *,
        difficulty: str,
        observed_level: int | None,
        observed_title: str | None,
        observed_title_confidence: float = 0.0,
        selection: Any | None = None,
        repository: LocalChartRepository | None = None,
        require_observed_title: bool = False,
        allow_missing_level: bool = False,
        reject_member_loading: bool = False,
    ) -> None:
        if selection is None and repository is None:
            raise ValueError("缺少最终封面谱面解析器")
        self.reject_member_loading = bool(reject_member_loading)
        self.wide_title_trial = final_cover_wide_title_trial_enabled()
        if self.reject_member_loading:
            member_loading_icon()
            print("FinalCover member_loading_guard=enabled", flush=True)
        self.difficulty = str(difficulty).strip().lower()
        self.observed_level = (
            None if observed_level is None else int(observed_level)
        )
        self.require_observed_title = bool(require_observed_title)
        self.allow_missing_level = bool(allow_missing_level)
        trusted_initial_title = (
            observed_title is not None
            and float(observed_title_confidence or 0.0) >= 0.7
        )
        self.observed_title = (
            str(observed_title).strip()
            if trusted_initial_title or not self.require_observed_title
            else None
        )
        self._observed_title_confidence = (
            float(observed_title_confidence or 0.0)
            if self.observed_title else 0.0
        )
        if bool(getattr(selection, "level_drift_tolerated", False)):
            # 准备页容忍出的谱面只是候选，最终封面必须独立解析并取得本页标题。
            repository = repository or LocalChartRepository(
                Path(__file__).resolve().parents[2] / "resource" / "charts",
            )
            selection = None
        self.repository = repository
        self._final_title_confirmed = False
        self.gate = (
            FinalCoverGate(
                selection,
                difficulty=self.difficulty,
                observed_level=self.observed_level,
                observed_title=self.observed_title,
                allow_missing_level=self.allow_missing_level,
            )
            if selection is not None else None
        )
        self.frames = 0
        self.last_reason = "final cover has not been observed"
        self._candidate_song_id = UNKNOWN_SONG_ID
        self._candidate_frames = 0
        self.last_member_loading_detected = False
        self.last_member_loading_score = None
        self.last_title_diagnostic = {}
        self._final_title_observed_frame = None
        # 退化诊断：每个新指纹只打一条日志，避免逐帧刷屏。
        self._logged_fingerprints: set[str] = set()

    @property
    def observed_title_confidence(self) -> float:
        return self._observed_title_confidence

    @property
    def independent_final_title_enabled(self) -> bool:
        return bool(getattr(self.repository, "regional_level_drift_enabled", False))

    def refresh_observed_title(self, text: str, confidence: float, *, identity_confirmed: bool = False) -> bool:
        """用最终封面页自身的标题 OCR 刷新准备页标题。

        协力房间准备页的标题行字体小且常被读乱；最终歌曲信息页封面下方
        的标题字体更清晰。该刷新只用于“准备页没有可信谱面、开演前按封面
        解析”的延迟路径（此时才有 repository）；准备页已经选定谱面的门控
        路径保持准备页标题，避免被加载页文字覆盖。

        协力的开演前加载还会经过“目标得分”等页面，同一 ROI 会读到与
        歌曲无关的文字。候选开启时，标题先独立于等级匹配本地曲目，再与
        本页封面交叉确认；准备页的高置信度不能阻止读取最终页自己的标题。
        """
        if (
            self.repository is None
            or not text
            or (
                self.independent_final_title_enabled and float(confidence) < 0.7
            )
            or (
                (not self.independent_final_title_enabled or self._final_title_confirmed)
                and not identity_confirmed
                and float(confidence) <= self._observed_title_confidence
            )
        ):
            return False
        normalized = str(text).strip()
        if not normalized or (
            normalized == self.observed_title
            and (not self.independent_final_title_enabled or self._final_title_confirmed)
        ):
            return False
        probe = self.repository.resolve(
            UNKNOWN_SONG_ID,
            self.difficulty,
            level=(None if self.independent_final_title_enabled else self.observed_level),
            title=normalized,
        )
        if probe.selection is None:
            return False
        self.observed_title = normalized
        self._observed_title_confidence = float(confidence)
        self._final_title_confirmed = True
        return True

    def _title_identity_status(self, song_id: str, reading: TitleReading) -> str:
        """宽框只提供读数；身份仍经过原谱面、等级、共享封面与标题门禁。"""
        assert self.repository is not None
        identify_title = getattr(self.repository, "identify_by_cover_title", None)
        catalog_identity = (
            identify_title(song_id, reading.text).identity if callable(identify_title) else None
        )
        title_only = self.repository.resolve(
            UNKNOWN_SONG_ID, self.difficulty, title=reading.text,
            level=(None if self.independent_final_title_enabled else self.observed_level),
        )
        if title_only.selection is not None and not any(
            same_song(song_id, fingerprint, max_distance=LOOSE_SAME_SONG_DISTANCE)
            for fingerprint in title_only.selection.fingerprints
        ):
            return "conflict"
        resolved = self.repository.resolve(
            song_id, self.difficulty, title=reading.text, level=self.observed_level,
        )
        if resolved.selection is None:
            return "conflict" if catalog_identity is not None else "missing"
        if catalog_identity is not None and catalog_identity.bestdori_song_id != resolved.selection.bestdori_song_id:
            # 同封面不同版本也是真实身份冲突，不能用宽框补成另一个 Special/FULL 版本。
            return "conflict"
        gate = FinalCoverGate(
            resolved.selection, difficulty=self.difficulty, observed_level=self.observed_level,
            observed_title=reading.text, allow_missing_level=self.allow_missing_level,
            require_matching_title=True,
        )
        return "confirmed" if gate.evidence_reason() is None else "missing"

    def evidence_reason(self) -> str | None:
        if not self.difficulty:
            return "preparation difficulty is missing"
        if self.observed_level is None and not self.allow_missing_level:
            return "preparation song level is missing"
        if self.gate is not None:
            return self.gate.evidence_reason()
        return None

    def _observe_cover_candidate(self, song_id: str) -> bool:
        if song_id == UNKNOWN_SONG_ID:
            self._candidate_song_id = UNKNOWN_SONG_ID
            self._candidate_frames = 0
        elif self._candidate_song_id != UNKNOWN_SONG_ID and same_song(song_id, self._candidate_song_id):
            self._candidate_frames += 1
            return True
        else:
            self._candidate_song_id = song_id
            self._candidate_frames = 1
        # 任何未知或不同封面都先隔离标题缓存，得分页也不能绕过身份生命周期。
        self._final_title_confirmed = False
        if self.independent_final_title_enabled:
            self.observed_title = None
            self._observed_title_confidence = 0.0
            self._final_title_observed_frame = None
        return song_id != UNKNOWN_SONG_ID

    def observe(
        self, image: Any, *, refresh_title: bool = False, title_reader=None,
    ) -> FinalCoverResolution | None:
        self.frames += 1
        self.last_title_diagnostic = {"status": "not-read", "cached_title_frame": self._final_title_observed_frame}
        self.last_member_loading_score = (
            member_loading_score(image) if self.reject_member_loading else None
        )
        self.last_member_loading_detected = bool(
            self.last_member_loading_score is not None
            and self.last_member_loading_score >= MEMBER_LOADING_ICON_THRESHOLD
        )
        if self.last_member_loading_detected:
            # 加载页即使稳定多帧也不能确认；出现该页会中断连续候选计数。
            self._candidate_song_id = UNKNOWN_SONG_ID
            self._candidate_frames = 0
            self._final_title_confirmed = False
            if self.independent_final_title_enabled:
                self.observed_title = None
                self._observed_title_confidence = 0.0
                self._final_title_observed_frame = None
            self.last_reason = "member loading screen"
            return None
        score_identity = None
        if is_final_score_layout(image):
            # 得分页仍展示相同封面，不能把得分标签提升为歌名或制造身份冲突。
            score_identity = identify_final_song(image)
            self._observe_cover_candidate(score_identity.song_id)
            self.last_title_diagnostic = {
                "status": "excluded-score-layout",
                "cached_title_frame": self._final_title_observed_frame,
            }
            if not (
                self._final_title_confirmed
                and self._final_title_observed_frame is not None
                and self._candidate_frames >= 2
            ):
                self.last_reason = "goal score page is not a song title"
                return None
            # 标题条已变为目标得分时，使用同一封面首帧的实读标题完成交叉确认；
            # 不重新 OCR 得分，也不能只凭缓存标志绕过谱面和标题冲突检查。
            self.last_title_diagnostic["status"] = "score-layout-cached-title"
        if (
            score_identity is None and refresh_title and self.repository is not None
            and not self.independent_final_title_enabled
            and (self.observed_title_confidence < 0.9 or self.wide_title_trial)
        ):
            # 标题 OCR 也必须在成员加载保护之后，不能把加载页文字当最终标题。
            if self.wide_title_trial:
                title_identity = identify_final_song(image)
                reading = recognize_final_cover_title(
                    image, reader=title_reader or recognize_song_title,
                    diagnostics=self.last_title_diagnostic, wide_title_trial=True,
                    identity_validator=lambda item: self._title_identity_status(title_identity.song_id, item),
                )
            else:
                reading = (title_reader or recognize_song_title)(image, roi=FINAL_COVER_TITLE_ROI)
            if reading is not None:
                self.refresh_observed_title(
                    reading.text, reading.confidence,
                    identity_confirmed=self.last_title_diagnostic.get("source") == "wide-roi",
                )
        if self.gate is not None:
            confirmation = self.gate.observe(image)
            self.last_reason = self.gate.last_reason
            if confirmation is None:
                return None
            return FinalCoverResolution(
                confirmation=confirmation,
                selection=self.gate.selection,
                observed_title=self.observed_title,
                observed_title_confidence=self._observed_title_confidence,
                final_title_confirmed=self._final_title_confirmed,
            )

        identity = score_identity or identify_final_song(image)
        if score_identity is None and not self._observe_cover_candidate(identity.song_id):
            self.last_reason = "final cover jacket is not visible"
            return None
        title_needs_reading = not self._final_title_confirmed
        if self.wide_title_trial and self._final_title_confirmed and self.repository is not None:
            # 未确认身份的高置信截断缓存不能阻止后续帧取得较低置信的完整标题。
            title_needs_reading = self._title_identity_status(
                identity.song_id, TitleReading(self.observed_title or "", self.observed_title_confidence),
            ) == "missing"
        if self.independent_final_title_enabled and title_needs_reading and score_identity is None:
            # 第一张有效封面就实读标题，并绑定本次指纹候选；连续封面门禁
            # 仍需两帧，不能等到第二帧才读而错过短暂展示的标题。
            self.last_title_diagnostic = {"first_cover_frame": self._candidate_frames == 1}
            reading = recognize_final_cover_title(
                image, reader=recognize_song_title, diagnostics=self.last_title_diagnostic,
                wide_title_trial=self.wide_title_trial,
                identity_validator=lambda reading: self._title_identity_status(identity.song_id, reading),
            )
            if str(self.last_title_diagnostic.get("status", "")).startswith("excluded-score"):
                self.last_reason = "goal score page is not a song title"
                return None
            if reading is not None and is_final_score_text(reading.text):
                self.last_title_diagnostic["status"] = "excluded-score-text"
                self.last_reason = "goal score page is not a song title"
                return None
            if reading is not None:
                if self.wide_title_trial and self._final_title_confirmed and not is_final_title_completion(
                    TitleReading(self.observed_title or "", self.observed_title_confidence), reading,
                ):
                    # 换帧也不能把上一帧真实高置信冲突替换成无关宽框标题。
                    reading = None
            if reading is not None:
                refreshed = self.refresh_observed_title(
                    reading.text, reading.confidence,
                    identity_confirmed=self.last_title_diagnostic.get("source") == "wide-roi",
                )
                if not refreshed and reading.confidence >= 0.9:
                    # 稳定封面上的高置信度实读即使不在曲库也保留，交给身份
                    # 交叉检查拒绝冲突，不能静默丢弃后继续使用准备页旧标题。
                    self.observed_title = str(reading.text).strip()
                    self._observed_title_confidence = float(reading.confidence)
                    self._final_title_confirmed = bool(self.observed_title)
                if self._final_title_confirmed:
                    self._final_title_observed_frame = self.frames
                    self.last_title_diagnostic["cached_title_frame"] = self.frames
        # 缓存标题不能降低连续两帧封面要求，加载和换封面会清空该缓存。
        if self._candidate_frames < 2:
            self.last_reason = "waiting for stable final cover jacket"
            return None
        if self.independent_final_title_enabled and not self._final_title_confirmed:
            self.last_reason = "final cover title is not confirmed"
            return None
        if self.require_observed_title and not self.observed_title:
            self.last_reason = "final cover title is not confirmed"
            return None

        assert self.repository is not None
        resolution = self.repository.resolve(
            identity.song_id,
            self.difficulty,
            level=self.observed_level,
            title=self.observed_title,
        )
        if resolution.selection is None:
            if (
                self._final_title_confirmed
                and resolution.reason == "song title conflicts with regional level drift candidate"
            ):
                # 本页封面与本页可信标题冲突属于身份失败，不能退回视觉演奏。
                raise RuntimeError(f"最终封面歌曲身份冲突：{resolution.reason}")
            if identity.song_id not in self._logged_fingerprints:
                self._logged_fingerprints.add(identity.song_id)
                print(
                    "FinalCover resolve_failed "
                    f"fingerprint={identity.song_id} "
                    f"level={self.observed_level} "
                    f"title={self.observed_title!r} "
                    f"reason={resolution.reason}",
                    flush=True,
                )
            self.last_reason = resolution.reason
            return None
        if (
            bool(getattr(resolution.selection, "level_drift_tolerated", False))
            and not self._final_title_confirmed
        ):
            self.last_reason = "regional level drift requires final cover title"
            return None
        gate = FinalCoverGate(
            resolution.selection,
            difficulty=self.difficulty,
            observed_level=self.observed_level,
            observed_title=self.observed_title,
            allow_missing_level=self.allow_missing_level,
            require_matching_title=self.independent_final_title_enabled,
        )
        confirmation = gate.observe(image)
        self.last_reason = gate.last_reason
        if confirmation is None:
            if (
                self._final_title_confirmed
                and gate.last_reason == "final cover song title conflicts with selected chart"
            ):
                raise RuntimeError(f"最终封面歌曲身份冲突：{gate.last_reason}")
            if (
                gate.last_reason == "final cover jacket does not match selected chart"
                and identity.song_id not in self._logged_fingerprints
            ):
                self._logged_fingerprints.add(identity.song_id)
                print(
                    "FinalCover gate_mismatch "
                    f"fingerprint={identity.song_id} "
                    f"selected_bestdori_id="
                    f"{resolution.selection.bestdori_song_id} "
                    f"level={self.observed_level}",
                    flush=True,
                )
            return None
        self.gate = gate
        return FinalCoverResolution(
            confirmation=confirmation,
            selection=resolution.selection,
            observed_title=self.observed_title,
            observed_title_confidence=self._observed_title_confidence,
            final_title_confirmed=self._final_title_confirmed,
        )
