from __future__ import annotations

from dataclasses import dataclass, replace
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
    hold_body_confidence: float = 1.0
    # A pink chevron marker riding at the tail end of a hold body means the
    # hold must be released with an upward swipe, not a plain lift.
    hold_tail_flick: bool = False


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
        # TYPE4 heads are solid cyan diamonds (H~100..108); TYPE1 uses pale
        # cyan spindles in the same band.  Keep the original hue range and
        # widen the geometry below instead: TYPE4 heads are much smaller and
        # squarer than TYPE1's outline-laden spindles.
        (NoteKind.TAP, (82, 70, 155), (108, 255, 255)),
        # At the game's 100% long-note opacity the translucent body measures
        # roughly H=48..68, S=34..171, V=145..240. Include the body, not only
        # the saturated white/green head rings.
        (NoteKind.HOLD, (38, 25, 100), (81, 255, 255)),
        (NoteKind.SKILL, (15, 95, 160), (37, 255, 255)),
    )
    FLICK_RANGE = ((135, 80, 155), (179, 255, 255))

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
        separated: list[ObservedNote] = []
        for note in candidates:
            if not separated:
                separated.append(note)
                continue
            previous = separated[-1]
            minimum_gap = max(
                8.0,
                min(24.0, (previous.height + note.height) * .35),
            )
            if note.y - previous.y >= minimum_gap:
                separated.append(note)
        if len(separated) == 2:
            first, second = separated
            width_ratio = min(first.width, second.width) / max(
                first.width, second.width
            )
            # The upper and lower arcs of one hollow ring have nearly the same
            # width and centre. Two perspective-scaled note heads do not.
            if (
                width_ratio >= .9
                and abs(first.x - second.x) <= 12
            ):
                return []
        return separated if len(separated) >= 2 else []

    @staticmethod
    def _is_hold_body_component(
        component: np.ndarray,
        *,
        original_width: int,
        original_height: int,
    ) -> bool:
        """Reject round green effects while retaining bodies and tail rings."""
        fill_ratio = float(np.count_nonzero(component)) / max(1, component.size)
        aspect = original_width / max(1, original_height)
        # A real body is a filled ribbon. A disconnected tail/head ring is
        # horizontally elongated. Round skill and judgement ripples are
        # neither, and were the source of false holds on no-hold charts.
        return (
            fill_ratio >= .32
            or aspect >= 2.2
            or original_height >= 140
        )

    def _detect_flick_chevrons(
        self,
        hsv: np.ndarray,
        ordinary_notes: list[ObservedNote],
        timestamp: float,
    ) -> list[ObservedNote]:
        """Recognise a flick from its chevron stack, not its purple head ring.

        Skin 1 gives every ordinary note a magenta outline in the same HSV
        range as a flick. A real flick carries one or more upward chevrons
        (bottom rows wider than top rows); an ordinary ring keeps nearly
        equal row extents and also sits on top of its cyan/yellow head.
        Misclassifying a tap as a flick is harmless - an upward swipe clears
        a tap note too - so recall wins ties against precision here.
        """
        mask = cv2.inRange(hsv, *self.FLICK_RANGE)
        mask = cv2.morphologyEx(
            mask,
            cv2.MORPH_OPEN,
            np.ones((2, 2), np.uint8),
        )
        count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask)
        candidates: list[tuple[ObservedNote, bool]] = []
        raw_components: list[dict[str, object]] = []
        for label in range(1, count):
            left, top, width, height, area = stats[label]
            original_width = int(width * 2)
            original_height = int(height * 2)
            if (
                area * 4 < 45
                or width < 4
                or height < 2
                or width / max(height, 1) < 1.15
                or original_height > 55
            ):
                continue
            component = labels[
                top:top + height,
                left:left + width,
            ] == label
            # Row extents vectorised: first/last lit pixel per row, same
            # integer values as the per-row flatnonzero loop it replaces.
            has_pixel = component.any(axis=1)
            first_lit = component.argmax(axis=1)
            last_lit = component.shape[1] - 1 - component[:, ::-1].argmax(axis=1)
            row_extents = (last_lit - first_lit + 1)[has_pixel].astype(float).tolist()
            third = max(1, len(row_extents) // 3)
            top_extent = float(np.mean(row_extents[:third]))
            bottom_extent = float(np.mean(row_extents[-third:]))
            is_chevron = (
                bottom_extent >= top_extent * 1.35
                and bottom_extent - top_extent >= 2
            )
            x, local_y = centroids[label]
            x *= 2
            y = local_y * 2 + self.PLAYFIELD_TOP
            centers = self.centers_at(y)
            lane = min(range(len(centers)), key=lambda i: abs(x - centers[i]))
            lane_spacing = max(24.0, abs(centers[1] - centers[0]))
            # Chevron stacks span slightly more than one perspective lane
            # width far up the road; rings and fragments stay at 1.15.
            width_cap = lane_spacing * (1.6 if is_chevron else 1.15)
            if (
                abs(x - centers[lane]) > lane_spacing * .46
                or original_width > width_cap
            ):
                continue
            raw_components.append({
                "lane": lane,
                "x": float(x),
                "y": float(y),
                "left": float(left * 2),
                "right": float((left + width) * 2),
                "top": float(top * 2 + self.PLAYFIELD_TOP),
                "bottom": float((top + height) * 2 + self.PLAYFIELD_TOP),
                "original_width": original_width,
                "original_height": original_height,
                "note": ObservedNote(
                    NoteKind.FLICK,
                    lane,
                    float(x),
                    float(y),
                    original_width,
                    original_height,
                    timestamp,
                ),
            })
            candidates.append((
                raw_components[-1]["note"],  # type: ignore[arg-type]
                is_chevron,
            ))

        # Horizontal flick arrows render as two wide flat magenta wing bars
        # (with chevron halves above/below) that the row-extent test cannot
        # recognise on its own. Left as fragments, the wings become "tap
        # siblings" and shadow the chevron half, so the whole arrow is tapped
        # instead of flicked - which the game judges as a miss. Reassemble the
        # wings into one FLICK and consume every fragment inside its bbox.
        flat_bars = [
            item for item in raw_components
            if int(item["original_width"]) >= 56
            and int(item["original_height"]) <= 18
            and int(item["original_width"])
            / max(1, int(item["original_height"])) >= 4.0
        ]
        consumed_ids: set[int] = set()
        horizontal_flicks: list[ObservedNote] = []
        if flat_bars:
            bars = sorted(flat_bars, key=lambda item: float(item["x"]))
            groups: list[list[dict[str, object]]] = []
            current = [bars[0]]
            for bar in bars[1:]:
                previous = current[-1]
                if (
                    abs(float(bar["y"]) - float(previous["y"])) <= 18
                    and float(bar["left"]) - float(previous["right"]) <= 90
                ):
                    current.append(bar)
                else:
                    groups.append(current)
                    current = [bar]
            groups.append(current)
            for group in groups:
                if len(group) < 2:
                    continue
                left = min(float(item["left"]) for item in group)
                right = max(float(item["right"]) for item in group)
                top = min(float(item["top"]) for item in group)
                bottom = max(float(item["bottom"]) for item in group)
                if right - left < 100:
                    continue
                center_x = (left + right) / 2
                center_y = (top + bottom) / 2
                centers = self.centers_at(center_y)
                lane = min(
                    range(len(centers)),
                    key=lambda i: abs(center_x - centers[i]),
                )
                assembly = ObservedNote(
                    NoteKind.FLICK,
                    lane,
                    float(center_x),
                    float(center_y),
                    int(right - left),
                    int(bottom - top),
                    timestamp,
                )
                horizontal_flicks.append(assembly)
                half_width = (right - left) / 2 + 20
                half_height = (bottom - top) / 2 + 25
                for item in raw_components:
                    if (
                        abs(float(item["x"]) - center_x) <= half_width
                        and abs(float(item["y"]) - center_y) <= half_height
                    ):
                        consumed_ids.add(id(item["note"]))
        if consumed_ids:
            candidates = [
                (note, is_chevron)
                for note, is_chevron in candidates
                if id(note) not in consumed_ids
            ]
            candidates.extend((note, True) for note in horizontal_flicks)

        # The magenta/white outline of an ordinary head sits on top of its
        # cyan or yellow component. Remove it before looking for stacks.
        candidates = [
            (candidate, is_chevron)
            for candidate, is_chevron in candidates
            if not any(
                note.kind in (NoteKind.TAP, NoteKind.SKILL)
                and note.lane == candidate.lane
                and abs(note.y - candidate.y) <= 32
                and abs(note.x - candidate.x)
                <= max(45.0, (note.width + candidate.width) * .55)
                for note in ordinary_notes
            )
        ]

        tap_candidates = [
            candidate for candidate, is_chevron in candidates if not is_chevron
        ]

        def _isolated(candidate: ObservedNote) -> bool:
            # A lone chevron only counts as a flick arrow when it is isolated:
            # a ring fragment always has sibling fragments of the same
            # ordinary note within a few pixels, while a real arrow sits
            # ~40 px above its spindle body. Without this gate, arc fragments
            # create phantom flick tracks that shadow the note's real tap
            # track - and a shadowed tap is marked fired and never acts.
            return not any(
                other.lane == candidate.lane
                and abs(other.y - candidate.y) <= 25
                and abs(other.x - candidate.x) <= 40
                for other in tap_candidates
            )

        classified: list[ObservedNote] = [
            ObservedNote(
                NoteKind.TAP,
                candidate.lane,
                candidate.x,
                candidate.y,
                candidate.width,
                candidate.height,
                candidate.timestamp,
            )
            for candidate in tap_candidates
        ]
        for lane in range(len(self.lane_centers)):
            lane_candidates = sorted(
                (
                    note
                    for note, is_chevron in candidates
                    if is_chevron and note.lane == lane
                ),
                key=lambda note: note.y,
            )
            stacked: set[int] = set()
            index = 0
            while index < len(lane_candidates):
                stack = [lane_candidates[index]]
                cursor = index + 1
                while cursor < len(lane_candidates):
                    previous = stack[-1]
                    candidate = lane_candidates[cursor]
                    gap = candidate.y - previous.y
                    if (
                        5 <= gap <= 34
                        and abs(candidate.x - previous.x)
                        <= max(32.0, (candidate.width + previous.width) * .45)
                    ):
                        stack.append(candidate)
                        cursor += 1
                        continue
                    break
                if len(stack) >= 2 and stack[-1].y - stack[0].y >= 12:
                    head = stack[-1]
                    # One ordinary ring can shatter into several chevron-shaped
                    # arcs that stack exactly like real arrows. Only accept the
                    # stack when its head has no tap-shaped siblings.
                    if _isolated(head):
                        classified.append(ObservedNote(
                            NoteKind.FLICK,
                            lane,
                            head.x,
                            head.y,
                            max(item.width for item in stack),
                            max(item.height for item in stack),
                            timestamp,
                        ))
                    for member in stack:
                        stacked.add(id(member))
                    index = cursor
                else:
                    index += 1
            # A lone surviving chevron is still a flick arrow, subject to the
            # same isolation gate as stack heads.
            for candidate in lane_candidates:
                if id(candidate) in stacked:
                    continue
                if _isolated(candidate):
                    classified.append(candidate)
        return classified

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
                if (
                    kind != NoteKind.HOLD
                    and width / max(height, 1) < 0.85
                ):
                    continue
                component = labels[top:top + height, left:left + width] == label
                original_width = width * 2
                original_height = height * 2
                if (
                    kind == NoteKind.HOLD
                    and not self._is_hold_body_component(
                        component,
                        original_width=original_width,
                        original_height=original_height,
                    )
                ):
                    continue
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
                    minimum_width = 6 + progress * 18
                elif kind == NoteKind.FLICK:
                    minimum_width = 10 + progress * 35
                else:
                    minimum_width = 8 + progress * 22
                if original_width < minimum_width:
                    continue
                notes.append(ObservedNote(
                    kind,
                    lane,
                    float(x),
                    float(y),
                    int(width * 2),
                    int(height * 2),
                    timestamp,
                    (
                        1.0
                        if kind != NoteKind.HOLD or original_height >= 80
                        else 0.0
                    ),
                ))
        notes = self._merge_skill_hold_heads(notes)
        notes.extend(self._detect_flick_chevrons(hsv, notes, timestamp))
        notes = self._annotate_hold_tail_flicks(notes)
        return self._remove_stationary_feedback(notes)

    @staticmethod
    def _annotate_hold_tail_flicks(
        notes: list[ObservedNote],
    ) -> list[ObservedNote]:
        """Fold a pink tail arrow into the hold body it rides on.

        A hold that ends in a flick carries a magenta chevron marker at the
        tail end of its green body. Emitted as a standalone flick it dies
        unfired while the hold lifts with a plain UP and the tail misses.
        Mark the hold instead; the planner swipes the release. The marker is
        consumed so it never spawns a phantom flick track.
        """
        holds = [note for note in notes if note.kind == NoteKind.HOLD]
        if not holds:
            return notes
        consumed: set[int] = set()
        annotated: dict[int, ObservedNote] = {}
        for hold in holds:
            top = hold.y - hold.height / 2
            for note in notes:
                if (
                    note.kind == NoteKind.FLICK
                    and id(note) not in consumed
                    and note.lane == hold.lane
                    and abs(note.x - hold.x) <= max(50.0, hold.width * .4)
                    and -30 <= top - note.y <= 40
                ):
                    consumed.add(id(note))
                    annotated[id(hold)] = replace(hold, hold_tail_flick=True)
        if not consumed:
            return notes
        return [
            annotated.get(id(note), note)
            for note in notes
            if id(note) not in consumed
        ]

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
                hold.hold_body_confidence,
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
