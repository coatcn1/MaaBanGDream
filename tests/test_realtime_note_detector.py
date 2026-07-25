from __future__ import annotations

import cv2
import numpy as np

from agent.realtime.note_detector import NoteDetector, NoteKind


def _frame(background=(28, 31, 38)):
    image = np.full((720, 1280, 3), background, dtype=np.uint8)
    for center in NoteDetector.DEFAULT_LANE_CENTERS:
        cv2.line(image, (640, 100), (center, 660), (48, 52, 62), 2)
    return image


def test_detector_finds_simultaneous_tap_notes_by_lane():
    image = _frame()
    cv2.rectangle(image, (310, 490), (370, 515), (55, 185, 255), -1)
    cv2.rectangle(image, (910, 490), (970, 515), (55, 185, 255), -1)

    notes = NoteDetector(input_color_order="RGB").detect(image, timestamp=1.0)

    assert [(note.kind, note.lane) for note in notes] == [
        (NoteKind.TAP, 1),
        (NoteKind.TAP, 5),
    ]


def test_detector_splits_slow_stacked_taps_joined_by_colour_bridge():
    image = _frame()
    cv2.ellipse(image, (370, 500), (65, 10), 0, 0, 360, (55, 185, 255), -1)
    cv2.ellipse(image, (350, 555), (78, 11), 0, 0, 360, (55, 185, 255), -1)
    cv2.rectangle(image, (345, 508), (355, 547), (55, 185, 255), -1)

    notes = [
        note for note in NoteDetector(input_color_order="RGB").detect(image, 1.0)
        if note.kind == NoteKind.TAP
    ]

    assert [note.lane for note in notes] == [1, 1]
    assert notes[1].y - notes[0].y >= 40


def test_detector_does_not_split_one_large_ring_into_top_and_bottom_notes():
    image = _frame()
    cv2.ellipse(image, (640, 540), (90, 24), 0, 0, 360, (55, 185, 255), 7)

    notes = [
        note for note in NoteDetector(input_color_order="RGB").detect(image, 1.0)
        if note.kind == NoteKind.TAP
    ]

    assert len(notes) <= 1


def test_detector_classifies_skill_hold_and_flick_on_a_different_background():
    image = _frame(background=(75, 48, 92))
    cv2.rectangle(image, (460, 420), (520, 445), (255, 220, 45), -1)
    cv2.rectangle(image, (610, 390), (670, 460), (70, 240, 110), -1)
    cv2.rectangle(image, (760, 560), (820, 585), (255, 65, 185), -1)

    notes = NoteDetector(input_color_order="RGB").detect(image, timestamp=2.0)

    assert [(note.kind, note.lane) for note in notes] == [
        (NoteKind.HOLD, 3),
        (NoteKind.SKILL, 2),
        (NoteKind.FLICK, 4),
    ]


def test_detector_suppresses_stationary_judgement_feedback_after_first_frame():
    image = _frame()
    # Same cyan component in the central feedback band for multiple capture
    # frames models the stationary PERFECT/GREAT text that previously masked a
    # true falling note in the planner's lane map.
    cv2.rectangle(image, (610, 510), (670, 535), (55, 185, 255), -1)
    detector = NoteDetector(input_color_order="RGB")

    assert detector.detect(image, timestamp=0.0)
    assert detector.detect(image, timestamp=0.02) == []


def test_detector_suppresses_green_hold_shaped_judgement_feedback():
    image = _frame()
    # Real trace: a green PERFECT particle persisted near x=683, y=531 and
    # was incorrectly treated as a hold note.
    cv2.rectangle(image, (660, 526), (706, 540), (70, 240, 110), -1)

    detector = NoteDetector(input_color_order="RGB")
    assert detector.detect(image, timestamp=1.0)
    assert detector.detect(image, timestamp=1.02) == []


def test_detector_keeps_a_note_moving_through_feedback_band():
    detector = NoteDetector(input_color_order="RGB")
    first = _frame()
    second = _frame()
    cv2.rectangle(first, (610, 500), (670, 525), (55, 185, 255), -1)
    cv2.rectangle(second, (610, 516), (670, 541), (55, 185, 255), -1)

    assert detector.detect(first, timestamp=1.0)
    assert detector.detect(second, timestamp=1.02)


def test_detector_keeps_the_translucent_body_of_an_opaque_hold_bar():
    image = _frame()
    # Measured from the 100%-opacity rehearsal frame: the center of the green
    # body is darker than the bright head/tail rings (HSV V around 145).
    polygon = np.array([[680, 230], [720, 230], [830, 550], [730, 550]], np.int32)
    cv2.fillConvexPoly(image, polygon, (48, 145, 74))

    holds = [n for n in NoteDetector(input_color_order="RGB").detect(image, 1.0) if n.kind == NoteKind.HOLD]

    assert holds
    assert max(n.height for n in holds) >= 250


def test_diagonal_hold_uses_playable_lower_end_for_lane():
    image = _frame()
    # The body centroid sits near lane 4, but the lower/playable end is lane 5.
    # Tracking the centroid made real green trails change lane while falling.
    polygon = np.array([[650, 180], [680, 180], [920, 560], [860, 560]], np.int32)
    cv2.fillConvexPoly(image, polygon, (48, 145, 74))

    holds = [n for n in NoteDetector(input_color_order="RGB").detect(image, 1.0) if n.kind == NoteKind.HOLD]

    assert holds
    assert max(holds, key=lambda note: note.height).lane == 5


def test_hold_geometry_uses_wide_head_ring_instead_of_body_bottom():
    image = _frame()
    body = np.array([[670, 100], [700, 100], [845, 490], [760, 490]], np.int32)
    cv2.fillConvexPoly(image, body, (48, 145, 74))
    # A narrow green artefact below the ring used to make the component's
    # bottom edge trigger the hold far too early.
    cv2.rectangle(image, (790, 490), (810, 550), (48, 145, 74), -1)
    cv2.ellipse(image, (800, 490), (85, 10), 0, 0, 360, (70, 240, 110), -1)

    hold = max(
        (n for n in NoteDetector(input_color_order="RGB").detect(image, 1.0) if n.kind == NoteKind.HOLD),
        key=lambda note: note.height,
    )

    assert 480 <= hold.y + hold.height / 2 <= 500


def test_detector_keeps_wide_perspective_hold_near_judgement_line():
    image = _frame()
    polygon = np.array([[650, 70], [690, 70], [990, 555], [650, 555]], np.int32)
    cv2.fillConvexPoly(image, polygon, (48, 145, 74))
    cv2.ellipse(image, (820, 550), (180, 12), 0, 0, 360, (70, 240, 110), -1)

    holds = [n for n in NoteDetector(input_color_order="RGB").detect(image, 1.0) if n.kind == NoteKind.HOLD]

    assert holds
    assert max(note.width for note in holds) > 300


def test_detector_rejects_wide_multilane_skill_effects():
    image = _frame()
    # This is much wider than a single lane at the same depth. It represents a
    # stage/skill effect, not a tappable skill head.
    cv2.rectangle(image, (520, 390), (760, 425), (255, 220, 45), -1)

    assert NoteDetector(input_color_order="RGB").detect(image, timestamp=1.0) == []


def test_detector_defaults_to_maa_bgr_frames():
    image = _frame()
    cv2.rectangle(image, (310, 490), (370, 515), (255, 185, 55), -1)

    notes = NoteDetector().detect(image, timestamp=1.0)

    assert [(note.kind, note.lane) for note in notes] == [(NoteKind.TAP, 1)]


def test_yellow_skill_head_connected_to_green_body_is_one_hold():
    image = _frame()
    # A skill-headed long note is rendered as a yellow head at the playable
    # lower end of a separate green translucent body.  Treating both colour
    # components independently produces TAP + DOWN and breaks the whole hold.
    cv2.rectangle(image, (765, 250), (815, 500), (48, 145, 74), -1)
    cv2.ellipse(image, (790, 500), (52, 13), 0, 0, 360, (70, 240, 110), -1)
    cv2.ellipse(image, (790, 500), (43, 9), 0, 0, 360, (255, 205, 35), -1)

    notes = NoteDetector(input_color_order="RGB").detect(image, timestamp=1.0)
    lane_notes = [note for note in notes if note.lane == 4]

    assert [note.kind for note in lane_notes] == [NoteKind.HOLD]
    assert 488 <= lane_notes[0].y + lane_notes[0].height / 2 <= 512
