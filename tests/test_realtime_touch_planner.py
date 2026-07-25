from __future__ import annotations

import json
from pathlib import Path

from agent.realtime.note_detector import NoteKind, ObservedNote
from agent.realtime.touch_planner import (
    ActionKind,
    RealtimePlanner,
    sliding_holds_enabled,
)


def _note(kind, lane, y, timestamp):
    return ObservedNote(kind, lane, 190 + lane * 150, y, 60, 100, timestamp)


def test_only_sliding_chart_difficulties_enable_cross_lane_holds():
    assert not sliding_holds_enabled("Easy")
    assert not sliding_holds_enabled("Normal")
    assert sliding_holds_enabled("Hard")
    assert sliding_holds_enabled("Expert")
    assert sliding_holds_enabled("Special")


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


def test_reclassified_crossing_cannot_retap_same_lane_within_retrigger_window():
    planner = RealtimePlanner(
        judgement_y=565,
        timing_offset_ms=0,
        retrigger_seconds=.12,
    )
    planner.update([_note(NoteKind.TAP, 2, 520, 1.00)], now=1.00)
    first = planner.update([_note(NoteKind.TAP, 2, 570, 1.05)], now=1.05)
    planner.update([_note(NoteKind.SKILL, 2, 560, 1.06)], now=1.06)
    rebuilt = planner.update([_note(NoteKind.SKILL, 2, 570, 1.10)], now=1.10)

    assert [(action.kind, action.reason) for action in first] == [
        (ActionKind.TAP, "crossing")
    ]
    assert rebuilt == []


def test_planner_keeps_a_hold_pressed_through_short_detection_gaps():
    planner = RealtimePlanner(judgement_y=620, timing_offset_ms=0, hold_grace_seconds=.35)

    planner.update([_note(NoteKind.HOLD, 2, 560, 1.00)], now=1.00)
    down = planner.update([_note(NoteKind.HOLD, 2, 580, 1.02)], now=1.02)
    gap = planner.update([], now=1.04)

    assert [action.kind for action in down] == [ActionKind.DOWN]
    assert gap == []


def test_planner_does_not_predict_hold_release_before_three_hundred_ms():
    planner = RealtimePlanner(judgement_y=620, timing_offset_ms=0, hold_grace_seconds=.35)
    planner.update([_note(NoteKind.HOLD, 2, 560, 1.00)], now=1.00)
    down = planner.update([_note(NoteKind.HOLD, 2, 580, 1.02)], now=1.02)

    assert [action.kind for action in down] == [ActionKind.DOWN]
    assert planner.update([], now=1.05) == []
    assert planner.update([], now=1.33) == []
    released = planner.update([], now=1.38)
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
    up = planner.update([ObservedNote(NoteKind.HOLD, 2, 490, 689, 60, 100, 1.5)], now=1.5)

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


def test_paired_holds_lift_together_when_one_tail_ring_reaches_the_line():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )

    downs = planner.update([
        ObservedNote(NoteKind.HOLD, 1, 340, 510, 70, 105, 1.0),
        ObservedNote(NoteKind.HOLD, 5, 940, 530, 70, 110, 1.0),
    ], now=1.0)
    planner.update([
        ObservedNote(NoteKind.HOLD, 1, 340, 540, 100, 105, 1.2),
        ObservedNote(NoteKind.HOLD, 5, 940, 575, 100, 110, 1.2),
    ], now=1.2)
    ups = planner.update([
        ObservedNote(NoteKind.HOLD, 1, 340, 570, 100, 18, 1.4),
        ObservedNote(NoteKind.HOLD, 5, 940, 572, 100, 18, 1.4),
    ], now=1.4)

    assert [(a.kind, a.lane, a.contact) for a in downs] == [
        (ActionKind.DOWN, 1, 1), (ActionKind.DOWN, 5, 5)
    ]
    # The lane-5 tail recorded last frame sits within the rescue margin, so
    # the shared connector lifts both contacts in one frame.
    assert [(a.kind, a.lane, a.contact, a.reason) for a in ups] == [
        (ActionKind.UP, 1, 1, "tail-ring"),
        (ActionKind.UP, 5, 5, "tail-ring-paired"),
    ]


def test_paired_hold_with_a_distant_tail_releases_on_its_own():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )

    planner.update([
        ObservedNote(NoteKind.HOLD, 1, 340, 510, 70, 105, 1.0),
        ObservedNote(NoteKind.HOLD, 5, 940, 490, 70, 110, 1.0),
    ], now=1.0)
    first = planner.update([
        ObservedNote(NoteKind.HOLD, 1, 340, 570, 100, 18, 1.4),
        ObservedNote(NoteKind.HOLD, 5, 940, 500, 100, 110, 1.4),
    ], now=1.4)
    second = planner.update([
        ObservedNote(NoteKind.HOLD, 5, 940, 572, 100, 18, 1.8),
    ], now=1.8)

    # Lane 5's tail is still well above the release margin, so pairing must
    # not drag it up with lane 1.
    assert [(a.kind, a.lane, a.contact, a.reason) for a in first] == [
        (ActionKind.UP, 1, 1, "tail-ring"),
    ]
    assert [(a.kind, a.lane, a.contact, a.reason) for a in second] == [
        (ActionKind.UP, 5, 5, "tail-ring"),
    ]


def test_reset_clears_hold_chord_partner_state():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )
    planner.update([
        ObservedNote(NoteKind.HOLD, 1, 340, 510, 70, 105, 1.0),
        ObservedNote(NoteKind.HOLD, 5, 940, 530, 70, 110, 1.0),
    ], now=1.0)
    assert planner._hold_chord_partner

    ups = planner.reset(1.2)

    assert [(a.kind, a.lane, a.contact) for a in ups] == [
        (ActionKind.UP, 1, 1), (ActionKind.UP, 5, 5)
    ]
    assert planner._hold_chord_partner == {}


def test_tap_fragment_shadowed_by_same_lane_flick_never_judges_first():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )

    actions = planner.update([
        _note(NoteKind.TAP, 2, 565, 1.0),
        _note(NoteKind.FLICK, 2, 561, 1.0),
    ], now=1.0)

    assert [(a.kind, a.lane, a.reason) for a in actions] == [
        (ActionKind.FLICK, 2, "rescue")
    ]


def test_first_visible_notes_are_rescued_once_at_the_line():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )

    tap = planner.update([_note(NoteKind.TAP, 2, 561, 1.0)], now=1.0)
    planner.update([], now=1.03)
    flick = planner.update([_note(NoteKind.FLICK, 5, 562, 1.06)], now=1.06)
    hold = planner.update([_note(NoteKind.HOLD, 3, 560, 1.09)], now=1.09)

    assert [(action.kind, action.lane, action.reason) for action in tap] == [
        (ActionKind.TAP, 2, "rescue")
    ]
    assert [(action.kind, action.lane, action.reason) for action in flick] == [
        (ActionKind.FLICK, 5, "rescue")
    ]
    assert [(action.kind, action.lane) for action in hold] == [(ActionKind.DOWN, 3)]


def test_first_visible_residue_below_the_line_never_fires():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )

    # Tap-effect ripples park at ~judgement + 9 with a flat geometry.
    first = planner.update([_note(NoteKind.TAP, 2, 574.4, 1.0)], now=1.0)
    planner.update([], now=1.2)
    reappeared = planner.update([_note(NoteKind.TAP, 2, 574.4, 1.5)], now=1.5)

    assert first == []
    assert reappeared == []


def test_tracked_note_crossing_below_the_line_still_fires():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )

    planner.update([_note(NoteKind.TAP, 2, 555, 1.0)], now=1.0)
    crossed = planner.update([_note(NoteKind.TAP, 2, 575, 1.05)], now=1.05)

    assert [(action.kind, action.reason) for action in crossed] == [
        (ActionKind.TAP, "rescue")
    ]


def test_first_visible_bright_object_is_rescued_once():
    planner = RealtimePlanner(judgement_y=565, rescue_first_visible=True)
    note = _note(NoteKind.TAP, 2, 561, 1.0)

    first = planner.update([note], now=1.0)
    repeated = planner.update([_note(NoteKind.TAP, 2, 561, 1.1)], now=1.1)

    assert [(action.kind, action.reason) for action in first] == [
        (ActionKind.TAP, "rescue")
    ]
    assert repeated == []


def test_note_rebuilt_after_retrigger_window_is_judged_as_new():
    planner = RealtimePlanner(
        judgement_y=565,
        rescue_first_visible=True,
        track_memory_seconds=.15,
    )

    first = planner.update([_note(NoteKind.TAP, 2, 561, 1.0)], now=1.0)
    planner.update([], now=1.20)
    rebuilt = planner.update([_note(NoteKind.TAP, 2, 562, 1.48)], now=1.48)

    assert [(action.kind, action.reason) for action in first] == [
        (ActionKind.TAP, "rescue")
    ]
    # The retrigger window is short on purpose: a note reappearing well
    # after it (and after tracker memory expired) is a new judgement.
    assert [(action.kind, action.reason) for action in rebuilt] == [
        (ActionKind.TAP, "rescue")
    ]


def test_rebuilt_near_line_skill_is_suppressed_within_retrigger_window():
    planner = RealtimePlanner(
        judgement_y=565, rescue_first_visible=True, track_memory_seconds=.05
    )

    first = planner.update([_note(NoteKind.SKILL, 4, 561, 1.0)], now=1.0)
    planner.update([], now=1.06)
    rebuilt = planner.update([_note(NoteKind.SKILL, 4, 563, 1.10)], now=1.10)

    assert [(action.kind, action.reason) for action in first] == [
        (ActionKind.TAP, "rescue")
    ]
    assert rebuilt == []


def test_simultaneous_adjacent_chord_notes_fire_together():
    planner = RealtimePlanner(judgement_y=565, rescue_first_visible=True)

    planner.update([
        _note(NoteKind.TAP, 3, 520, .95),
        _note(NoteKind.SKILL, 4, 520, .95),
    ], now=.95)
    actions = planner.update([
        _note(NoteKind.TAP, 3, 570, 1.0),
        _note(NoteKind.SKILL, 4, 570, 1.0),
    ], now=1.0)

    assert [(action.kind, action.lane) for action in actions] == [
        (ActionKind.TAP, 3),
        (ActionKind.TAP, 4),
    ]


def test_note_must_cross_judgement_line_before_triggering():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )

    assert planner.update([_note(NoteKind.TAP, 2, 545, 1.0)], now=1.0) == []
    tap = planner.update([_note(NoteKind.TAP, 2, 570, 1.05)], now=1.05)

    assert [(action.kind, action.lane, action.reason) for action in tap] == [
        (ActionKind.TAP, 2, "rescue")
    ]


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
        ObservedNote(NoteKind.HOLD, 5, 940, 571, 100, 18, 1.5),
        ObservedNote(NoteKind.TAP, 1, 340, 535, 80, 25, 1.5),
    ], now=1.5)

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


def test_released_hold_tail_cannot_be_rescued_as_a_tap_on_the_same_lane():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )
    planner.update([
        ObservedNote(NoteKind.HOLD, 4, 730, 405, 100, 320, 1.0)
    ], now=1.0)
    released = planner.update([
        ObservedNote(NoteKind.HOLD, 4, 790, 572, 100, 18, 2.0)
    ], now=2.0)
    lingering_tail = planner.update([
        ObservedNote(NoteKind.TAP, 4, 790, 562, 100, 18, 2.30)
    ], now=2.30)

    assert [action.kind for action in released] == [ActionKind.UP]
    assert lingering_tail == []


def test_real_crossing_note_after_hold_release_is_not_suppressed():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )
    planner.update([
        ObservedNote(NoteKind.HOLD, 4, 730, 405, 100, 320, 1.0)
    ], now=1.0)
    planner.update([
        ObservedNote(NoteKind.HOLD, 4, 790, 572, 100, 18, 2.0),
        ObservedNote(NoteKind.TAP, 4, 790, 520, 80, 18, 2.0),
    ], now=2.0)
    actions = planner.update([
        ObservedNote(NoteKind.TAP, 4, 790, 570, 80, 18, 2.10)
    ], now=2.10)

    assert [(action.kind, action.reason) for action in actions] == [
        (ActionKind.TAP, "crossing")
    ]


def test_latest_hold_replay_filters_all_late_born_adjacent_taps():
    fixture = Path(__file__).parent / "fixtures" / "latest_hold_adjacent_replay.json"
    frames = json.loads(fixture.read_text(encoding="utf-8"))
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )
    adjacent = []

    for frame in frames:
        timestamp = float(frame["timestamp"])
        notes = [
            ObservedNote(
                NoteKind(item["kind"]),
                item["lane"],
                item["x"],
                item["y"],
                item["width"],
                item["height"],
                timestamp,
            )
            for item in frame["notes"]
        ]
        adjacent.extend(
            action
            for action in planner.update(notes, timestamp)
            if action.kind in (ActionKind.TAP, ActionKind.FLICK)
            and action.lane == 1
        )

    assert adjacent == []
    assert planner.filtered_adjacent_artifacts == 7


def test_adjacent_note_tracked_from_above_is_kept_during_hold():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )
    planner.update([
        ObservedNote(NoteKind.HOLD, 2, 500, 310, 220, 510, 1.0),
        ObservedNote(NoteKind.TAP, 1, 400, 470, 60, 30, 1.0),
    ], now=1.0)
    planner.update([
        ObservedNote(NoteKind.HOLD, 2, 500, 330, 220, 500, 1.05),
        ObservedNote(NoteKind.TAP, 1, 405, 525, 65, 35, 1.05),
    ], now=1.05)
    actions = planner.update([
        ObservedNote(NoteKind.HOLD, 2, 500, 350, 220, 490, 1.10),
        ObservedNote(NoteKind.TAP, 1, 410, 570, 70, 40, 1.10),
    ], now=1.10)

    assert [(action.kind, action.lane) for action in actions] == [
        (ActionKind.TAP, 1)
    ]


def test_adjacent_track_with_reversed_motion_is_filtered_during_hold():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )
    frames = [
        (1.00, 500),
        (1.04, 540),
        (1.08, 530),
        (1.12, 570),
    ]
    actions = []
    for timestamp, tap_y in frames:
        actions.extend(planner.update([
            ObservedNote(NoteKind.HOLD, 2, 500, 310, 220, 510, timestamp),
            ObservedNote(NoteKind.TAP, 1, 400, tap_y, 60, 30, timestamp),
        ], now=timestamp))

    assert not [
        action for action in actions
        if action.kind in (ActionKind.TAP, ActionKind.FLICK) and action.lane == 1
    ]
    assert planner.filtered_adjacent_artifacts == 1


def test_predicted_hold_release_waits_at_least_three_hundred_ms():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )
    down = planner.update([
        ObservedNote(NoteKind.HOLD, 3, 640, 500, 100, 140, 1.0)
    ], now=1.0)
    planner.update([
        ObservedNote(NoteKind.HOLD, 3, 640, 550, 100, 120, 1.05)
    ], now=1.05)

    assert [action.kind for action in down] == [ActionKind.DOWN]
    assert planner.update([], now=1.20) == []
    assert planner.update([], now=1.31) == []
    release = planner.update([], now=1.41)
    assert [(action.kind, action.reason) for action in release] == [
        (ActionKind.UP, "predicted-tail")
    ]


def test_released_hold_cannot_restart_from_lingering_body_during_cooldown():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )
    planner.update([
        ObservedNote(NoteKind.HOLD, 4, 730, 405, 100, 320, 1.0)
    ], now=1.0)
    planner.update([
        ObservedNote(NoteKind.HOLD, 4, 790, 572, 100, 18, 2.0)
    ], now=2.0)

    lingering = planner.update([
        ObservedNote(NoteKind.HOLD, 4, 790, 520, 100, 100, 2.1)
    ], now=2.1)

    assert lingering == []


def test_upper_green_body_does_not_turn_thin_judgement_fragment_into_hold():
    fixture = Path(__file__).parent / "fixtures" / "latest_thin_hold_replay.json"
    frames = json.loads(fixture.read_text(encoding="utf-8"))
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )
    actions = []

    for frame in frames:
        timestamp = float(frame["timestamp"])
        notes = [
            ObservedNote(
                NoteKind(item["kind"]),
                item["lane"],
                item["x"],
                item["y"],
                item["width"],
                item["height"],
                timestamp,
            )
            for item in frame["notes"]
        ]
        actions.extend(planner.update(notes, timestamp))

    assert [
        (action.kind, action.lane, action.reason) for action in actions
        if action.kind is ActionKind.DOWN
    ] == []


def test_latest_slanted_hold_replay_moves_one_existing_contact():
    fixture = Path(__file__).parent / "fixtures" / "latest_slanted_hold_replay.json"
    frames = json.loads(fixture.read_text(encoding="utf-8"))
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )
    actions = []

    for frame in frames:
        timestamp = float(frame["timestamp"])
        notes = [
            ObservedNote(
                NoteKind(item["kind"]),
                item["lane"],
                item["x"],
                item["y"],
                item["width"],
                item["height"],
                timestamp,
            )
            for item in frame["notes"]
        ]
        actions.extend(planner.update(notes, timestamp))

    structural = [
        action for action in actions
        if action.kind in (ActionKind.DOWN, ActionKind.MOVE, ActionKind.UP)
    ]
    assert [action.kind for action in structural] == [
        ActionKind.DOWN,
        ActionKind.MOVE,
    ]
    assert structural[0].contact == structural[1].contact == 5
    assert structural[1].lane == 6
    assert structural[1].target_x == 1013


def test_disconnected_short_fragments_cannot_start_or_release_hold():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )
    for timestamp, y in ((0.0, 40), (0.05, 43), (0.10, 46)):
        planner.update([
            ObservedNote(
                NoteKind.HOLD, 5, 653, y, 18, 20, timestamp
            )
        ], timestamp)
    down = planner.update([
        ObservedNote(NoteKind.HOLD, 5, 655, 48, 20, 22, .35),
        ObservedNote(NoteKind.HOLD, 5, 954, 570, 32, 10, .35),
    ], .35)

    assert down == []
    assert planner.update([
        ObservedNote(NoteKind.HOLD, 5, 655, 51, 22, 24, .40),
        ObservedNote(NoteKind.HOLD, 5, 954, 570, 80, 12, .40),
    ], .40) == []
    for timestamp, y in ((.45, 55), (.50, 60)):
        assert planner.update([
            ObservedNote(
                NoteKind.HOLD, 5, 655, y, 22, 24, timestamp
            )
        ], timestamp) == []

    assert planner.update([], .70) == []
    assert planner.update([
        ObservedNote(NoteKind.HOLD, 5, 954, 570, 80, 12, 1.0)
    ], 1.0) == []


def test_upper_body_x_shift_from_perspective_does_not_move_contact():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )
    down = planner.update([
        ObservedNote(NoteKind.HOLD, 5, 940, 300, 180, 540, 1.0)
    ], 1.0)
    continued = planner.update([
        ObservedNote(NoteKind.HOLD, 5, 655, 100, 80, 80, 1.05)
    ], 1.05)

    assert [action.kind for action in down] == [ActionKind.DOWN]
    assert continued == []
    assert planner._active_hold_tail[5] == 60


def test_visible_hold_body_blocks_stale_predicted_release():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )
    planner.update([
        ObservedNote(NoteKind.HOLD, 4, 730, 405, 100, 320, 1.0)
    ], 1.0)
    planner.update([
        ObservedNote(NoteKind.HOLD, 4, 745, 450, 100, 300, 1.2)
    ], 1.2)

    assert planner.update([
        ObservedNote(NoteKind.HOLD, 4, 745, 450, 100, 300, 2.14)
    ], 2.14) == []


def test_real_body_can_restart_hold_on_released_lane():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )

    first = planner.update([
        ObservedNote(NoteKind.HOLD, 5, 940, 500, 220, 140, .35)
    ], .35)
    released = planner.update([
        ObservedNote(NoteKind.HOLD, 5, 954, 570, 80, 12, 1.0)
    ], 1.0)

    second = planner.update([
        ObservedNote(NoteKind.HOLD, 5, 940, 500, 220, 140, 2.35)
    ], 2.35)

    assert [(action.kind, action.reason) for action in first] == [
        (ActionKind.DOWN, "rescue")
    ]
    assert [(action.kind, action.reason) for action in released] == [
        (ActionKind.UP, "tail-ring")
    ]
    assert [(action.kind, action.reason) for action in second] == [
        (ActionKind.DOWN, "rescue")
    ]


def test_non_sliding_chart_does_not_steal_adjacent_hold_component():
    planner = RealtimePlanner(
        judgement_y=565,
        timing_offset_ms=0,
        rescue_first_visible=True,
        enable_slide=False,
    )
    first = planner.update([
        ObservedNote(NoteKind.HOLD, 1, 340, 500, 100, 140, 1.0)
    ], 1.0)
    second = planner.update([
        ObservedNote(NoteKind.HOLD, 2, 490, 510, 100, 140, 1.05)
    ], 1.05)

    assert [(action.kind, action.lane, action.contact) for action in first] == [
        (ActionKind.DOWN, 1, 1)
    ]
    assert [(action.kind, action.lane, action.contact) for action in second] == [
        (ActionKind.DOWN, 2, 2)
    ]
    assert planner._active_hold_lane == {1: 1, 2: 2}
