from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import cv2
import numpy as np


class NoteKind(str, Enum):
    TAP = "tap"
    SKILL = "skill"
    HOLD = "hold"
    FLICK = "flick"


@dataclass(frozen=True)
class ObservedNote:
    kind: NoteKind
    lane: int
    x: float
    y: float
    width: int
    height: int
    timestamp: float


class NoteDetector:
    """Detect saturated rhythm-note heads inside the seven-lane playfield."""

    DEFAULT_LANE_CENTERS = (190, 340, 490, 640, 790, 940, 1090)
    VANISHING_Y = 20
    JUDGEMENT_Y = 590
    PLAYFIELD_TOP = 45
    # Stop above the glowing judgement line. Its seven fixed cyan nodes would
    # otherwise look exactly like stationary tap notes.
    PLAYFIELD_BOTTOM = 580

    def __init__(
        self,
        lane_centers: tuple[int, ...] | None = None,
        *,
        input_color_order: str = "BGR",
    ):
        self.lane_centers = lane_centers or self.DEFAULT_LANE_CENTERS
        if input_color_order not in {"BGR", "RGB"}:
            raise ValueError("input_color_order must be BGR or RGB")
        self._hsv_conversion = (
            cv2.COLOR_BGR2HSV if input_color_order == "BGR" else cv2.COLOR_RGB2HSV
        )
        # The game draws PERFECT/GREAT feedback over the middle of the
        # playfield.  Its coloured glyph fragments can pass the same HSV tests
        # as notes, but unlike a note they do not move between video frames.
        # Keep this state in the detector (one instance per realtime engine),
        # rather than using a fixed screen mask which would hide valid notes.
        self._stationary: dict[tuple[NoteKind, int], tuple[float, float, int]] = {}

    COLOR_RANGES = (
        (NoteKind.TAP, (82, 70, 155), (108, 255, 255)),
        # At the game's 100% long-note opacity the translucent body measures
        # roughly H=48..68, S=34..171, V=145..240. Include the body, not only
        # the saturated white/green head rings.
        (NoteKind.HOLD, (38, 25, 100), (81, 255, 255)),
        (NoteKind.SKILL, (15, 95, 160), (37, 255, 255)),
        (NoteKind.FLICK, (135, 80, 155), (179, 255, 255)),
    )

    def centers_at(self, y: float) -> tuple[float, ...]:
        progress = min(1.08, max(0.0, (y - self.VANISHING_Y) / (
            self.JUDGEMENT_Y - self.VANISHING_Y
        )))
        return tuple(640 + (center - 640) * progress for center in self.lane_centers)

    def _split_stacked_ordinary_component(
        self,
        component: np.ndarray,
        *,
        left: int,
        top: int,
        kind: NoteKind,
        timestamp: float,
    ) -> list[ObservedNote]:
        """Recover vertically stacked heads joined by a thin colour bridge."""
        width = component.shape[1]
        horizontal = cv2.morphologyEx(
            component.astype(np.uint8) * 255,
            cv2.MORPH_OPEN,
            np.ones((1, max(7, round(width * .2))), np.uint8),
        )
        count, _, stats, centroids = cv2.connectedComponentsWithStats(horizontal)
        candidates: list[ObservedNote] = []
        for label in range(1, count):
            _, _, local_width, local_height, area = stats[label]
            original_width = int(local_width * 2)
            original_height = int(local_height * 2)
            if (
                area * 4 < 180
                or original_height > 32
                or original_width / max(original_height, 1) < 2.5
            ):
                continue
            local_x, local_y = centroids[label]
            x = (left + local_x) * 2
            y = (top + local_y) * 2 + self.PLAYFIELD_TOP
            centers = self.centers_at(y)
            lane = min(range(len(centers)), key=lambda i: abs(x - centers[i]))
            lane_spacing = max(24.0, abs(centers[1] - centers[0]))
            if abs(x - centers[lane]) > lane_spacing * .46:
                continue
            progress = min(1.0, max(0.0, (y - self.VANISHING_Y) / (
                self.JUDGEMENT_Y - self.VANISHING_Y
            )))
            minimum_width = (
                10 + progress * 35
                if kind == NoteKind.FLICK
                else 12 + progress * 45
            )
            if original_width < minimum_width:
                continue
            candidates.append(ObservedNote(
                kind,
                lane,
                float(x),
                float(y),
                original_width,
                max(4, original_height),
                timestamp,
            ))
        candidates.sort(key=lambda note: note.y)
        if len(candidates) < 2:
            return []
        separated = [
            note for index, note in enumerate(candidates)
            if index == 0 or note.y - candidates[index - 1].y >= 50
        ]
        return separated if len(separated) >= 2 else []

    def detect(self, image: np.ndarray, timestamp: float) -> list[ObservedNote]:
        # Colour segmentation does not need full-resolution pixels.  Processing
        # the playfield at half size cuts HSV/connected-component work to 25%,
        # while all reported geometry remains in the canonical 1280x720 space.
        roi = cv2.resize(
            image[self.PLAYFIELD_TOP:self.PLAYFIELD_BOTTOM],
            None, fx=.5, fy=.5, interpolation=cv2.INTER_AREA,
        )
        hsv = cv2.cvtColor(roi, self._hsv_conversion)
        notes: list[ObservedNote] = []
        kernel = np.ones((2, 3), np.uint8)
        for kind, lower, upper in self.COLOR_RANGES:
            mask = cv2.inRange(hsv, lower, upper)
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
            count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask)
            for label in range(1, count):
                left, top, width, height, area = stats[label]
                original_area = area * 4
                maximum_area = 120000 if kind == NoteKind.HOLD else 9000
                if not 45 <= original_area <= maximum_area or width < 4 or height < 2:
                    continue
                if kind != NoteKind.HOLD and width / max(height, 1) < 1.15:
                    continue
                component = labels[top:top + height, left:left + width] == label
                if (
                    kind != NoteKind.HOLD
                    and width * 2 >= 80
                    and height * 2 >= 45
                ):
                    split = self._split_stacked_ordinary_component(
                        component,
                        left=left,
                        top=top,
                        kind=kind,
                        timestamp=timestamp,
                    )
                    if split:
                        notes.extend(split)
                        continue
                maximum_height = 270 if kind == NoteKind.HOLD else 55
                maximum_width = 320 if kind == NoteKind.HOLD else 150
                if width > maximum_width or height > maximum_height:
                    continue
                x, local_y = centroids[label]
                head_y = None
                # The component bottom is not the playable head: translucent
                # body/glow pixels can extend ~100 px below the visible ring.
                # The head ring is the widest horizontal run in the lower half
                # of the falling component. Use that run for both lane and
                # crossing time while retaining full body height for its tail.
                if kind == NoteKind.HOLD:
                    lower_start = min(height - 1, max(0, int(height * .45)))
                    row_counts = component[lower_start:].sum(axis=1)
                    head_row = lower_start + int(np.argmax(row_counts))
                    head_x = np.flatnonzero(component[head_row])
                    if head_x.size:
                        x = left + float(np.median(head_x))
                        head_y = (top + head_row) * 2 + self.PLAYFIELD_TOP
                x *= 2
                y = local_y * 2 + self.PLAYFIELD_TOP
                original_height = height * 2
                if head_y is not None:
                    # RealtimePlanner defines head as y + height/2 and tail as
                    # y - height/2. Anchor that geometry on the observed ring.
                    y = head_y - original_height / 2
                lane_y = (
                    head_y if head_y is not None else (top + height) * 2 + self.PLAYFIELD_TOP
                    if kind == NoteKind.HOLD else y
                )
                centers = self.centers_at(lane_y)
                lane = min(range(len(centers)), key=lambda i: abs(x - centers[i]))
                lane_spacing = max(24.0, abs(centers[1] - centers[0]))
                if abs(x - centers[lane]) > lane_spacing * .42:
                    continue
                original_width = width * 2
                # A note grows with the perspective lane width. A fixed pixel
                # cap rejected valid heads near the judgement line on some
                # songs. Reject only components that span substantially more
                # than one lane at their current depth; holds may contain a
                # wider connected trail and are handled by head/tail tracking.
                if kind != NoteKind.HOLD and original_width > lane_spacing * 1.15:
                    continue
                progress = min(1.0, max(0.0, (y - self.VANISHING_Y) / (
                    self.JUDGEMENT_Y - self.VANISHING_Y
                )))
                # Character outlines and skill particles produce many tiny
                # saturated blobs. Real note heads grow predictably as they
                # approach the player, so reject components that are too narrow
                # for their depth in the perspective playfield.
                if kind in (NoteKind.TAP, NoteKind.SKILL):
                    minimum_width = 12 + progress * 45
                elif kind == NoteKind.FLICK:
                    minimum_width = 10 + progress * 35
                else:
                    minimum_width = 8 + progress * 22
                if original_width < minimum_width:
                    continue
                notes.append(ObservedNote(
                    kind, lane, float(x), float(y), int(width * 2), int(height * 2), timestamp
                ))
        return self._remove_stationary_feedback(self._merge_skill_hold_heads(notes))

    @staticmethod
    def _merge_skill_hold_heads(notes: list[ObservedNote]) -> list[ObservedNote]:
        """Fold a yellow skill head into the green long note it starts.

        The renderer uses two colour components for this one gameplay object.
        Leaving both observations in the stream lets the ordinary-note tracker
        emit a TAP immediately before the hold tracker emits DOWN.
        """
        consumed_skills: set[int] = set()
        replacements: dict[int, ObservedNote] = {}
        for skill_index, skill in enumerate(notes):
            if skill.kind != NoteKind.SKILL:
                continue
            candidates = []
            for hold_index, hold in enumerate(notes):
                if hold.kind != NoteKind.HOLD or hold.lane != skill.lane:
                    continue
                hold_head = hold.y + hold.height / 2
                if abs(skill.x - hold.x) <= max(55, skill.width) and abs(skill.y - hold_head) <= 38:
                    candidates.append((abs(skill.y - hold_head), hold_index, hold))
            if not candidates:
                continue
            _, hold_index, hold = min(candidates)
            tail = hold.y - hold.height / 2
            # The yellow ellipse is the playable head centre. The green mask
            # often includes glow pixels below it, so keeping the green mask's
            # lower edge would start the hold late.
            head = skill.y
            replacements[hold_index] = ObservedNote(
                NoteKind.HOLD,
                hold.lane,
                skill.x,
                (tail + head) / 2,
                max(hold.width, skill.width),
                max(1, int(round(head - tail))),
                hold.timestamp,
            )
            consumed_skills.add(skill_index)
        return [
            replacements.get(index, note)
            for index, note in enumerate(notes)
            if index not in consumed_skills
        ]

    def _remove_stationary_feedback(self, notes: list[ObservedNote]) -> list[ObservedNote]:
        """Suppress persistent judgement glyphs, while retaining moving notes.

        LDOpenGL normally supplies a fresh game frame every 16.7 ms, even when
        capture runs faster.  Four unchanged observations therefore identify a
        static feedback glyph without materially delaying a falling note.
        Restricting this to the central judgement-feedback band keeps the upper
        playfield and the actual judgement line unaffected.
        """
        next_stationary: dict[tuple[NoteKind, int], tuple[float, float, int]] = {}
        filtered: list[ObservedNote] = []
        for note in notes:
            in_feedback_band = (
                500 <= note.x <= 780 and 490 <= note.y <= 560
            )
            key = (note.kind, note.lane)
            previous = self._stationary.get(key)
            count = 1
            if (
                in_feedback_band
                and previous is not None
                and abs(note.x - previous[0]) <= 8
                and abs(note.y - previous[1]) <= 2
            ):
                count = previous[2] + 1
            if in_feedback_band:
                next_stationary[key] = (note.x, note.y, count)
            # A real note moves through this band; judgement glyph fragments
            # remain at the same position. Keep the first observation (it is
            # still well above the planner's rescue line) and suppress only a
            # repeated stationary component. This avoids creating a blind
            # strip that made genuine notes reappear too late and get rescued.
            if not in_feedback_band or count == 1:
                filtered.append(note)
        self._stationary = next_stationary
        return sorted(filtered, key=lambda note: (note.y, note.lane))
