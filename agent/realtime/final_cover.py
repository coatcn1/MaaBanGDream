"""开演前最终封面确认门控。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import unicodedata

from .chart_repository import LocalChartRepository
from .song_identity import (
    LOOSE_SAME_SONG_DISTANCE,
    UNKNOWN_SONG_ID,
    detect_full_badge,
    identify_final_song,
    same_song,
)
from .song_title_ocr import title_similarity


@dataclass(frozen=True, slots=True)
class FinalCoverConfirmation:
    song_id: str
    song_id_method: str
    bestdori_song_id: int


@dataclass(frozen=True, slots=True)
class FinalCoverResolution:
    confirmation: FinalCoverConfirmation
    selection: Any


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
    ) -> None:
        self.selection = selection
        self.difficulty = str(difficulty).strip().lower()
        self.observed_level = (
            None if observed_level is None else int(observed_level)
        )
        self.observed_title = (
            None if observed_title is None else str(observed_title).strip()
        )
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
            return "preparation song level is missing"
        if expected_level is None or int(expected_level) != self.observed_level:
            return "preparation song level conflicts with selected chart"
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
        # 走到这里说明 evidence_reason 已确认等级硬约束（难度、等级与
        # 准备页读数一致）。最终封面裁切/缩放会让个别谱面稳定多翻转几
        # bit（Little Busters! 实测 10 bit、FIRE BIRD 实测 12 bit），
        # 必须与 LocalChartRepository.resolve 的宽阈值语义一致，否则
        # 仓库刚按 14 bit + 等级解析出的谱面会被这里 8 bit 复核直接拒绝，
        # 整局降级成视觉 Legacy。宽阈值只在等级匹配时启用，不能单独放宽。
        if not any(
            same_song(
                identity.song_id,
                item,
                max_distance=LOOSE_SAME_SONG_DISTANCE,
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
    ) -> None:
        if selection is None and repository is None:
            raise ValueError("缺少最终封面谱面解析器")
        self.difficulty = str(difficulty).strip().lower()
        self.observed_level = (
            None if observed_level is None else int(observed_level)
        )
        self.observed_title = (
            None if observed_title is None else str(observed_title).strip()
        )
        self._observed_title_confidence = float(observed_title_confidence or 0.0)
        self.repository = repository
        self.gate = (
            FinalCoverGate(
                selection,
                difficulty=self.difficulty,
                observed_level=self.observed_level,
                observed_title=self.observed_title,
            )
            if selection is not None else None
        )
        self.frames = 0
        self.last_reason = "final cover has not been observed"
        self._candidate_song_id = UNKNOWN_SONG_ID
        self._candidate_frames = 0
        # 退化诊断：每个新指纹只打一条日志，避免逐帧刷屏。
        self._logged_fingerprints: set[str] = set()

    @property
    def observed_title_confidence(self) -> float:
        return self._observed_title_confidence

    def refresh_observed_title(self, text: str, confidence: float) -> bool:
        """用最终封面页自身的标题 OCR 刷新准备页标题。

        协力房间准备页的标题行字体小且常被读乱；最终歌曲信息页封面下方
        的标题字体更清晰。该刷新只用于“准备页没有可信谱面、开演前按封面
        解析”的延迟路径（此时才有 repository）；准备页已经选定谱面的门控
        路径保持准备页标题，避免被加载页文字覆盖。

        协力的开演前加载还会经过“目标得分”等页面，同一 ROI 会读到与
        歌曲无关的文字；只有该读数能在当前难度等级下唯一匹配本地曲目时
        才替换，垃圾读数一律忽略，也不会覆盖准备页已经可靠的标题。
        """
        if (
            self.repository is None
            or not text
            or float(confidence) <= self._observed_title_confidence
        ):
            return False
        normalized = str(text).strip()
        if not normalized or normalized == self.observed_title:
            return False
        probe = self.repository.resolve(
            UNKNOWN_SONG_ID,
            self.difficulty,
            level=self.observed_level,
            title=normalized,
        )
        if probe.selection is None:
            return False
        self.observed_title = normalized
        self._observed_title_confidence = float(confidence)
        return True

    def evidence_reason(self) -> str | None:
        if not self.difficulty:
            return "preparation difficulty is missing"
        if self.observed_level is None:
            return "preparation song level is missing"
        if self.gate is not None:
            return self.gate.evidence_reason()
        return None

    def observe(self, image: Any) -> FinalCoverResolution | None:
        self.frames += 1
        if self.gate is not None:
            confirmation = self.gate.observe(image)
            self.last_reason = self.gate.last_reason
            if confirmation is None:
                return None
            return FinalCoverResolution(
                confirmation=confirmation,
                selection=self.gate.selection,
            )

        identity = identify_final_song(image)
        if identity.song_id == UNKNOWN_SONG_ID:
            self._candidate_song_id = UNKNOWN_SONG_ID
            self._candidate_frames = 0
            self.last_reason = "final cover jacket is not visible"
            return None
        if (
            self._candidate_song_id != UNKNOWN_SONG_ID
            and same_song(identity.song_id, self._candidate_song_id)
        ):
            self._candidate_frames += 1
        else:
            self._candidate_song_id = identity.song_id
            self._candidate_frames = 1
        # 协力加载画面会短暂经过多张高纹理图片，连续两帧稳定后才查谱面。
        if self._candidate_frames < 2:
            self.last_reason = "waiting for stable final cover jacket"
            return None

        assert self.repository is not None
        resolution = self.repository.resolve(
            identity.song_id,
            self.difficulty,
            level=self.observed_level,
            title=self.observed_title,
        )
        if resolution.selection is None:
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
        gate = FinalCoverGate(
            resolution.selection,
            difficulty=self.difficulty,
            observed_level=self.observed_level,
            observed_title=self.observed_title,
        )
        confirmation = gate.observe(image)
        self.last_reason = gate.last_reason
        if confirmation is None:
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
        )
