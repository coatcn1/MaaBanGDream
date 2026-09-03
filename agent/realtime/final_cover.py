"""开演前最终封面确认门控。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .chart_repository import LocalChartRepository
from .song_identity import UNKNOWN_SONG_ID, identify_final_song, same_song
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
                return "preparation song title conflicts with shared jacket"
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
        identity = identify_final_song(image)
        if identity.song_id == UNKNOWN_SONG_ID:
            self.last_reason = "final cover jacket is not visible"
            return None
        fingerprints = tuple(getattr(self.selection, "fingerprints", ()))
        if not any(same_song(identity.song_id, item) for item in fingerprints):
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
            return None
        self.gate = gate
        return FinalCoverResolution(
            confirmation=confirmation,
            selection=resolution.selection,
        )
