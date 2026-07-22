from __future__ import annotations

from agent.realtime.note_detector import NoteKind, ObservedNote
from agent.realtime.touch_planner import ActionKind, RealtimePlanner


def _note(kind, lane, y, timestamp):
    return ObservedNote(kind, lane, 190 + lane * 150, y, 60, 100, timestamp)


def test_planner_batches_a_chord_once_when_notes_cross_the_judgement_line():
    planner = RealtimePlanner(judgement_y=620, timing_offset_ms=0)

    assert planner.update([
        _note(NoteKind.TAP, 1, 590, 1.00),
        _note(NoteKind.TAP, 5, 590, 1.00),
    ], now=1.00) == []
    actions = planner.update([
        _note(NoteKind.TAP, 1, 622, 1.02),
        _note(NoteKind.TAP, 5, 622, 1.02),
    ], now=1.02)
    duplicate = planner.update([
        _note(NoteKind.TAP, 1, 628, 1.03),
        _note(NoteKind.TAP, 5, 628, 1.03),
    ], now=1.03)

    assert [(action.kind, action.lane) for action in actions] == [
        (ActionKind.TAP, 1),
        (ActionKind.TAP, 5),
    ]
    assert duplicate == []


def test_planner_keeps_a_hold_pressed_through_short_detection_gaps():
    planner = RealtimePlanner(judgement_y=620, timing_offset_ms=0, hold_grace_seconds=.35)

    planner.update([_note(NoteKind.HOLD, 2, 560, 1.00)], now=1.00)
    down = planner.update([_note(NoteKind.HOLD, 2, 580, 1.02)], now=1.02)
    gap = planner.update([], now=1.04)

    assert [action.kind for action in down] == [ActionKind.DOWN]
    assert gap == []


def test_planner_releases_a_hold_from_head_motion_when_tail_is_then_lost():
    planner = RealtimePlanner(judgement_y=620, timing_offset_ms=0, hold_grace_seconds=.35)
    planner.update([_note(NoteKind.HOLD, 2, 560, 1.00)], now=1.00)
    down = planner.update([_note(NoteKind.HOLD, 2, 580, 1.02)], now=1.02)

    assert [action.kind for action in down] == [ActionKind.DOWN]
    released = planner.update([], now=1.05)
    assert [(action.kind, action.reason) for action in released] == [
        (ActionKind.UP, "predicted-tail")
    ]


def test_planner_does_not_release_a_legitimate_long_hold_at_six_seconds():
    planner = RealtimePlanner(
        judgement_y=565, rescue_first_visible=True, hold_max_seconds=20
    )
    down = planner.update([
        ObservedNote(NoteKind.HOLD, 2, 490, 520, 100, 100, 1.0)
    ], now=1.0)

    assert [action.kind for action in down] == [ActionKind.DOWN]
    assert planner.update([], now=7.01) == []
    released = planner.update([], now=21.01)
    assert [(action.kind, action.reason) for action in released] == [
        (ActionKind.UP, "hold-failsafe")
    ]


def test_production_hold_release_line_is_before_the_judgement_line():
    planner = RealtimePlanner(judgement_y=565)
    assert planner.hold_release_y == 555


def test_planner_releases_a_hold_when_its_tail_reaches_the_judgement_line():
    planner = RealtimePlanner(judgement_y=620, timing_offset_ms=0)
    # Centroid moves down.  The lower edge is the head; upper edge is tail.
    planner.update([ObservedNote(NoteKind.HOLD, 2, 490, 540, 60, 100, 1.0)], now=1.0)
    down = planner.update([ObservedNote(NoteKind.HOLD, 2, 490, 575, 60, 100, 1.1)], now=1.1)
    up = planner.update([ObservedNote(NoteKind.HOLD, 2, 490, 689, 60, 100, 1.2)], now=1.2)

    assert [action.kind for action in down] == [ActionKind.DOWN]
    assert [action.kind for action in up] == [ActionKind.UP]


def test_planner_supports_two_simultaneous_holds_with_distinct_contacts():
    planner = RealtimePlanner(judgement_y=620, timing_offset_ms=0)
    planner.update([
        _note(NoteKind.HOLD, 1, 560, 1.0),
        _note(NoteKind.HOLD, 5, 560, 1.0),
    ], now=1.0)

    downs = planner.update([
        _note(NoteKind.HOLD, 1, 580, 1.02),
        _note(NoteKind.HOLD, 5, 580, 1.02),
    ], now=1.02)
    reset = planner.reset(1.1)

    assert [(a.kind, a.lane, a.contact) for a in downs] == [
        (ActionKind.DOWN, 1, 1), (ActionKind.DOWN, 5, 5)
    ]
    assert [(a.kind, a.lane, a.contact) for a in reset] == [
        (ActionKind.UP, 1, 1), (ActionKind.UP, 5, 5)
    ]


def test_planner_rescues_both_sides_of_slightly_asymmetric_double_hold():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )

    actions = planner.update([
        ObservedNote(NoteKind.HOLD, 1, 340, 510, 70, 105, 1.0),
        # Segmentation makes lane 6's head 20px higher in the same frame.
        ObservedNote(NoteKind.HOLD, 5, 940, 490, 70, 110, 1.0),
    ], now=1.0)

    assert [(a.kind, a.lane, a.contact) for a in actions] == [
        (ActionKind.DOWN, 1, 1), (ActionKind.DOWN, 5, 5)
    ]
    assert actions[1].reason == "paired-rescue"


def test_rescue_mode_hits_intermittently_detected_notes_near_the_line():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )

    tap = planner.update([_note(NoteKind.TAP, 2, 561, 1.0)], now=1.0)
    planner.update([], now=1.03)
    flick = planner.update([_note(NoteKind.FLICK, 5, 562, 1.06)], now=1.06)
    hold = planner.update([_note(NoteKind.HOLD, 3, 560, 1.09)], now=1.09)

    assert [(action.kind, action.lane) for action in tap] == [(ActionKind.TAP, 2)]
    assert [(action.kind, action.lane) for action in flick] == [(ActionKind.FLICK, 5)]
    assert [(action.kind, action.lane) for action in hold] == [(ActionKind.DOWN, 3)]


def test_rescue_mode_does_not_repeatedly_tap_a_persistent_bright_object():
    planner = RealtimePlanner(judgement_y=565, rescue_first_visible=True)
    note = _note(NoteKind.TAP, 2, 561, 1.0)

    first = planner.update([note], now=1.0)
    repeated = planner.update([_note(NoteKind.TAP, 2, 561, 1.1)], now=1.1)

    assert len(first) == 1
    assert repeated == []


def test_rescue_does_not_retap_skill_when_track_is_rebuilt_near_line():
    planner = RealtimePlanner(
        judgement_y=565, rescue_first_visible=True, track_memory_seconds=.05
    )

    first = planner.update([_note(NoteKind.SKILL, 4, 561, 1.0)], now=1.0)
    planner.update([], now=1.06)
    rebuilt = planner.update([_note(NoteKind.SKILL, 4, 563, 1.10)], now=1.10)

    assert [(action.kind, action.lane) for action in first] == [(ActionKind.TAP, 4)]
    assert rebuilt == []


def test_one_tap_suppresses_same_window_notes_on_adjacent_lanes():
    planner = RealtimePlanner(judgement_y=565, rescue_first_visible=True)

    actions = planner.update([
        _note(NoteKind.TAP, 3, 561, 1.0),
        _note(NoteKind.SKILL, 4, 561, 1.0),
    ], now=1.0)

    assert [(action.kind, action.lane) for action in actions] == [
        (ActionKind.TAP, 3)
    ]


def test_rescue_does_not_trigger_twenty_five_pixels_before_judgement_line():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )

    assert planner.update([_note(NoteKind.TAP, 2, 545, 1.0)], now=1.0) == []
    tap = planner.update([_note(NoteKind.TAP, 2, 561, 1.05)], now=1.05)

    assert [(action.kind, action.lane) for action in tap] == [(ActionKind.TAP, 2)]


def test_planner_tracks_a_falling_note_across_a_short_detection_gap():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=False
    )
    planner.update([_note(NoteKind.TAP, 2, 530, 1.0)], now=1.0)
    planner.update([], now=1.02)

    actions = planner.update([_note(NoteKind.TAP, 2, 570, 1.04)], now=1.04)

    assert [(action.kind, action.lane, action.reason) for action in actions] == [
        (ActionKind.TAP, 2, "crossing")
    ]


def test_active_hold_uses_the_long_bar_not_a_small_green_feedback_ring():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )
    down = planner.update([
        ObservedNote(NoteKind.HOLD, 4, 790, 515, 70, 100, 1.0)
    ], now=1.0)

    actions = planner.update([
        ObservedNote(NoteKind.HOLD, 4, 730, 390, 100, 300, 1.02),
        ObservedNote(NoteKind.HOLD, 4, 790, 572, 60, 14, 1.02),
    ], now=1.02)

    assert [action.kind for action in down] == [ActionKind.DOWN]
    assert actions == []


def test_small_green_feedback_fragment_cannot_start_a_hold():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )

    actions = planner.update([
        ObservedNote(NoteKind.HOLD, 4, 790, 572, 60, 14, 1.0)
    ], now=1.0)

    assert actions == []


def test_moving_short_hold_head_can_start_a_hold():
    """A white connector/alpha split can leave only a short moving head."""
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=False
    )
    planner.update([
        ObservedNote(NoteKind.HOLD, 2, 490, 530, 62, 34, 1.0)
    ], now=1.0)

    actions = planner.update([
        ObservedNote(NoteKind.HOLD, 2, 490, 552, 66, 38, 1.02)
    ], now=1.02)

    assert [(action.kind, action.lane, action.reason) for action in actions] == [
        (ActionKind.DOWN, 2, "crossing")
    ]


def test_large_hold_head_is_rescued_when_geometry_jumps_across_line():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )
    planner.update([
        ObservedNote(NoteKind.HOLD, 2, 520, 220, 180, 380, 1.0)
    ], now=1.0)  # head=410

    actions = planner.update([
        ObservedNote(NoteKind.HOLD, 2, 490, 325, 220, 500, 1.02)
    ], now=1.02)  # head=575; centroid jump is intentionally >80 px

    assert [(action.kind, action.lane, action.reason) for action in actions] == [
        (ActionKind.DOWN, 2, "rescue")
    ]


def test_active_hold_follows_continuous_tail_not_next_bar_above():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )
    planner.update([
        ObservedNote(NoteKind.HOLD, 4, 730, 405, 100, 320, 1.0)
    ], now=1.0)
    # Same bar tail advances from y=245 to y=270; another future bar appears
    # above it and must not steal the active contact.
    middle = planner.update([
        ObservedNote(NoteKind.HOLD, 4, 735, 420, 100, 300, 1.02),
        ObservedNote(NoteKind.HOLD, 4, 650, 100, 90, 400, 1.02),
    ], now=1.02)
    tracked_tail = planner._active_hold_tail[4]
    end = planner.update([
        ObservedNote(NoteKind.HOLD, 4, 790, 660, 80, 100, 1.37)
    ], now=1.37)

    assert middle == []
    assert tracked_tail == 270
    assert [action.kind for action in end] == [ActionKind.UP]


def test_hold_releases_at_predicted_tail_crossing_when_body_disappears():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )
    planner.update([
        ObservedNote(NoteKind.HOLD, 4, 730, 405, 100, 320, 1.0)
    ], now=1.0)
    planner.update([
        ObservedNote(NoteKind.HOLD, 4, 745, 450, 100, 300, 1.2)
    ], now=1.2)

    assert planner.update([], now=2.12) == []
    released = planner.update([], now=2.14)

    assert [(action.kind, action.reason) for action in released] == [
        (ActionKind.UP, "predicted-tail")
    ]


def test_visible_tail_ring_at_line_overrides_stale_prediction():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )
    planner.update([
        ObservedNote(NoteKind.HOLD, 4, 730, 405, 100, 320, 1.0)
    ], now=1.0)
    planner.update([
        ObservedNote(NoteKind.HOLD, 4, 745, 450, 100, 300, 1.2)
    ], now=1.2)

    released = planner.update([
        # The body remains visible in the same frame; the thin ring must win.
        ObservedNote(NoteKind.HOLD, 4, 760, 546, 234, 60, 2.0),
        ObservedNote(NoteKind.HOLD, 4, 784, 572.5, 108, 14, 2.0),
    ], now=2.0)

    assert [(action.kind, action.reason) for action in released] == [
        (ActionKind.UP, "tail-ring")
    ]


def test_lane_sweep_covers_free_lanes_without_releasing_active_hold():
    planner = RealtimePlanner(
        judgement_y=565,
        rescue_first_visible=True,
        lane_sweep_interval=.08,
    )
    planner.update([
        ObservedNote(NoteKind.HOLD, 3, 640, 405, 100, 320, 1.0)
    ], now=1.0)

    actions = planner.update([], now=1.08)

    sweep_lanes = [a.lane for a in actions if a.reason == "lane-sweep"]
    assert sweep_lanes == [0, 1, 2, 4, 5, 6]
    assert all(a.kind == ActionKind.FLICK for a in actions if a.reason == "lane-sweep")
    assert not any(a.kind == ActionKind.UP for a in actions)
    assert planner.update([], now=1.12) == []
def test_hold_tail_release_does_not_tap_a_nearby_note_again():
    planner = RealtimePlanner(judgement_y=565, timing_offset_ms=0)
    planner.update([
        ObservedNote(NoteKind.HOLD, 5, 940, 500, 80, 100, 1.0),
        ObservedNote(NoteKind.TAP, 1, 340, 490, 70, 25, 1.0),
    ], now=1.0)
    planner.update([
        ObservedNote(NoteKind.HOLD, 5, 940, 530, 80, 100, 1.1),
        ObservedNote(NoteKind.TAP, 1, 340, 510, 70, 25, 1.1),
    ], now=1.1)

    actions = planner.update([
        ObservedNote(NoteKind.HOLD, 5, 940, 571, 100, 18, 1.2),
        ObservedNote(NoteKind.TAP, 1, 340, 535, 80, 25, 1.2),
    ], now=1.2)

    assert [(action.kind, action.lane) for action in actions] == [
        (ActionKind.UP, 5),
    ]


def test_released_hold_cannot_restart_from_its_lingering_tail_geometry():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )
    planner.update([
        ObservedNote(NoteKind.HOLD, 4, 730, 405, 100, 320, 1.0)
    ], now=1.0)
    planner.update([
        ObservedNote(NoteKind.HOLD, 4, 745, 450, 100, 300, 1.2)
    ], now=1.2)
    released = planner.update([
        ObservedNote(NoteKind.HOLD, 4, 790, 572, 100, 18, 2.0)
    ], now=2.0)
    lingering = planner.update([
        ObservedNote(NoteKind.HOLD, 4, 790, 520, 100, 100, 2.04)
    ], now=2.04)

    assert [action.kind for action in released] == [ActionKind.UP]
    assert lingering == []
