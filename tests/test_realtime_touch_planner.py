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


def test_trusted_parallel_fragment_cannot_retap_same_physical_note():
    planner = RealtimePlanner(
        judgement_y=565,
        timing_offset_ms=0,
        retrigger_seconds=.12,
    )
    for timestamp, upper_y, lower_y in (
        (1.00, 480, 500),
        (1.02, 510, 530),
        (1.04, 540, 570),
    ):
        actions = planner.update([
            ObservedNote(
                NoteKind.TAP, 5, 920, upper_y, 90, 14, timestamp
            ),
            ObservedNote(
                NoteKind.TAP, 5, 850, lower_y, 120, 30, timestamp
            ),
        ], now=timestamp)
    assert [(action.kind, action.lane) for action in actions] == [
        (ActionKind.TAP, 5)
    ]

    rebuilt = planner.update([
        ObservedNote(NoteKind.TAP, 5, 920, 570, 90, 14, 1.08)
    ], now=1.08)

    assert rebuilt == []


def test_tap_ring_cannot_fire_after_its_flick_was_already_dispatched():
    planner = RealtimePlanner(
        judgement_y=565,
        timing_offset_ms=0,
        retrigger_seconds=.12,
        rescue_first_visible=True,
    )
    planner.update([
        ObservedNote(NoteKind.FLICK, 1, 360, 480, 58, 22, 1.00)
    ], now=1.00)
    flick = planner.update([
        ObservedNote(NoteKind.FLICK, 1, 360, 500, 58, 22, 1.04),
        ObservedNote(NoteKind.TAP, 1, 400, 565, 82, 6, 1.04),
    ], now=1.04)
    ring = planner.update([
        ObservedNote(NoteKind.TAP, 1, 400, 575, 82, 6, 1.08)
    ], now=1.08)

    assert [(action.kind, action.lane) for action in flick] == [
        (ActionKind.FLICK, 1)
    ]
    assert ring == []


def test_late_flick_ring_residue_is_suppressed_beyond_retrigger_window():
    planner = RealtimePlanner(
        judgement_y=565,
        timing_offset_ms=0,
        retrigger_seconds=.12,
        rescue_first_visible=True,
    )
    planner.update([
        ObservedNote(NoteKind.FLICK, 1, 360, 480, 58, 22, 1.00)
    ], now=1.00)
    flick = planner.update([
        ObservedNote(NoteKind.FLICK, 1, 360, 520, 58, 22, 1.04),
        ObservedNote(NoteKind.TAP, 1, 400, 565, 82, 6, 1.04),
    ], now=1.04)
    # Real Hard traces show the playable ring surviving ~0.4 s after the
    # arrow dispatched; it is first seen at the line and would otherwise be
    # rescued as a spurious TAP.
    ring = planner.update([
        ObservedNote(NoteKind.TAP, 1, 400, 575, 82, 6, 1.42)
    ], now=1.42)

    assert [(action.kind, action.lane) for action in flick] == [
        (ActionKind.FLICK, 1)
    ]
    assert ring == []


def test_occluded_long_falling_head_fires_just_before_trigger_target():
    planner = RealtimePlanner(
        judgement_y=565,
        timing_offset_ms=0,
        rescue_first_visible=True,
    )
    now = 1.00
    actions = []
    for y in (480, 500, 520, 540, 549, 556):
        actions.extend(planner.update([
            ObservedNote(NoteKind.TAP, 3, 640, y, 40, 20, now)
        ], now=now))
        now += 0.016

    # The head is tracked from far up (minimum_y ~480), but a slide body can
    # swallow it a few pixels before the trigger target. A strongly trusted
    # long-falling head within 6 px of the target must still fire, otherwise
    # Hard dense passages turn into silent misses.
    assert [(action.kind, action.lane) for action in actions] == [
        (ActionKind.TAP, 3)
    ]


def test_occlusion_rescue_consumes_low_confidence_fragment_on_slide_chart():
    planner = RealtimePlanner(
        judgement_y=565,
        timing_offset_ms=0,
        rescue_first_visible=True,
        enable_slide=True,
    )
    now = 1.00
    for y in (300, 340, 380, 420, 460, 500, 520):
        planner.update([
            ObservedNote(NoteKind.TAP, 3, 640, y, 40, 20, now)
        ], now=now)
        now += 0.016
    # The head is occluded; only a late low-confidence fragment is visible.
    # It is not compatible with the head track (12 px above the last head),
    # so it becomes a separate track. The stale head has not yet reached the
    # near-target window and must rescue through the occlusion, consuming the
    # fragment so it cannot double-fire.
    actions = planner.update([
        ObservedNote(NoteKind.TAP, 3, 630, 508, 30, 10, now)
    ], now=now)
    actions.extend(planner.update([], now=now + 0.016))

    assert sum(
        1 for action in actions
        if action.kind == ActionKind.TAP and action.lane == 3
    ) == 1


def test_stale_flick_fragment_cannot_shadow_a_later_tap_crossing():
    planner = RealtimePlanner(
        judgement_y=565,
        timing_offset_ms=0,
        rescue_first_visible=True,
    )
    planner.update([
        ObservedNote(NoteKind.TAP, 2, 490, 480, 78, 16, 1.00)
    ], now=1.00)
    planner.update([
        ObservedNote(NoteKind.TAP, 2, 490, 530, 78, 16, 1.03),
        ObservedNote(NoteKind.FLICK, 2, 490, 490, 24, 10, 1.03),
    ], now=1.03)
    planner.update([
        ObservedNote(NoteKind.TAP, 2, 490, 545, 78, 16, 1.06)
    ], now=1.06)

    crossing = planner.update([
        ObservedNote(NoteKind.TAP, 2, 490, 566, 78, 16, 1.09)
    ], now=1.09)

    assert [(action.kind, action.lane) for action in crossing] == [
        (ActionKind.TAP, 2)
    ]


def test_concentric_late_fragment_cannot_retap_a_tracked_skill_head():
    planner = RealtimePlanner(
        judgement_y=565,
        timing_offset_ms=0,
        retrigger_seconds=.12,
    )
    for timestamp, outer_y in (
        (1.00, 480),
        (1.04, 530),
        (1.08, 565),
    ):
        first = planner.update([
            ObservedNote(
                NoteKind.SKILL, 5, 930, outer_y, 92, 36, timestamp
            ),
        ], now=timestamp)
    assert [(action.kind, action.lane) for action in first] == [
        (ActionKind.TAP, 5)
    ]

    late_fragment = planner.update([
        ObservedNote(NoteKind.SKILL, 5, 929, 575, 86, 8, 1.12)
    ], now=1.12)

    assert late_fragment == []


def test_tracker_swap_at_line_cannot_preempt_the_replacement_head():
    planner = RealtimePlanner(
        judgement_y=565,
        timing_offset_ms=0,
    )
    planner.update([
        ObservedNote(NoteKind.TAP, 2, 494, 220, 64, 14, 1.00)
    ], now=1.00)
    planner.update([
        ObservedNote(NoteKind.TAP, 2, 494, 350, 64, 14, 1.04)
    ], now=1.04)
    planner.update([
        ObservedNote(NoteKind.TAP, 2, 494, 480, 64, 14, 1.08),
        ObservedNote(NoteKind.TAP, 2, 507, 465, 82, 18, 1.08),
    ], now=1.08)

    # Segmentation assigns the old track to a line-glow component 94 px
    # lower, while the replacement physical head continues near its previous
    # position. The jumped track must not create an early TAP.
    protected = planner.update([
        ObservedNote(NoteKind.TAP, 2, 494, 574, 64, 14, 1.12),
        ObservedNote(NoteKind.TAP, 2, 507, 479, 82, 18, 1.12),
    ], now=1.12)
    continued = planner.update([
        ObservedNote(NoteKind.TAP, 2, 500, 530, 82, 18, 1.20)
    ], now=1.20)
    real = planner.update([
        ObservedNote(NoteKind.TAP, 2, 483, 562, 82, 18, 1.28)
    ], now=1.28)

    assert protected == []
    assert [
        (action.kind, action.lane) for action in continued + real
    ] == [
        (ActionKind.TAP, 2)
    ]


def test_kind_change_at_line_rescues_a_tracked_crossing():
    planner = RealtimePlanner(
        judgement_y=565,
        timing_offset_ms=0,
        rescue_first_visible=True,
    )
    planner.update([
        ObservedNote(NoteKind.TAP, 4, 790, 520, 70, 16, 1.00)
    ], now=1.00)
    planner.update([
        ObservedNote(NoteKind.TAP, 4, 790, 540, 72, 16, 1.03)
    ], now=1.03)

    rescued = planner.update([
        ObservedNote(NoteKind.FLICK, 4, 790, 578, 58, 4, 1.06)
    ], now=1.06)

    assert [(action.kind, action.lane, action.reason) for action in rescued] == [
        (ActionKind.FLICK, 4, "reclassified-crossing")
    ]


def test_planner_keeps_a_hold_pressed_through_short_detection_gaps():
    planner = RealtimePlanner(judgement_y=620, timing_offset_ms=0, hold_grace_seconds=.35)

    planner.update([_note(NoteKind.HOLD, 2, 560, 1.00)], now=1.00)
    down = planner.update([_note(NoteKind.HOLD, 2, 580, 1.02)], now=1.02)
    gap = planner.update([], now=1.04)

    assert [action.kind for action in down] == [ActionKind.DOWN]
    assert gap == []


def test_detached_slide_head_uses_connected_body_as_hold_evidence():
    planner = RealtimePlanner(
        judgement_y=565,
        timing_offset_ms=0,
        rescue_first_visible=True,
        enable_slide=True,
    )

    actions = planner.update([
        # Wide diagonal body: its centroid has already moved into lane 1.
        ObservedNote(
            NoteKind.HOLD, 1, 371, 453, 558, 176, 1.00,
            hold_body_confidence=1.0,
        ),
        # Detached playable head at the line is still in lane 0.
        ObservedNote(
            NoteKind.HOLD, 0, 242, 562, 124, 18, 1.00,
            hold_body_confidence=0.0,
        ),
    ], now=1.00)

    assert [
        (action.kind, action.lane, action.reason) for action in actions
    ] == [(ActionKind.DOWN, 0, "rescue")]

    continuation = planner.update([
        ObservedNote(
            NoteKind.HOLD, 1, 390, 475, 520, 170, 1.04,
            hold_body_confidence=1.0,
        ),
        ObservedNote(
            NoteKind.HOLD, 0, 245, 570, 120, 18, 1.04,
            hold_body_confidence=0.0,
        ),
    ], now=1.04)

    assert not [
        action for action in continuation if action.kind == ActionKind.DOWN
    ]


def test_negative_profile_offset_cannot_delay_a_hold_below_the_line():
    planner = RealtimePlanner(
        judgement_y=565,
        timing_offset_ms=-11,
        rescue_first_visible=True,
    )
    planner.update([
        ObservedNote(
            NoteKind.HOLD, 1, 390, 414, 212, 178, 1.00,
            hold_body_confidence=1.0,
        ),
    ], now=1.00)

    actions = planner.update([
        ObservedNote(
            NoteKind.HOLD, 1, 383, 470, 220, 186, 1.04,
            hold_body_confidence=1.0,
        ),
    ], now=1.04)

    assert [(action.kind, action.lane) for action in actions] == [
        (ActionKind.DOWN, 1)
    ]


def test_new_confirmed_head_restarts_a_stale_cross_lane_contact():
    planner = RealtimePlanner(
        judgement_y=565,
        timing_offset_ms=0,
        rescue_first_visible=True,
        enable_slide=True,
    )
    planner.update([
        ObservedNote(
            NoteKind.HOLD, 6, 1030, 390, 220, 220, 1.00,
            hold_body_confidence=1.0,
        )
    ], now=1.00)
    started = planner.update([
        ObservedNote(
            NoteKind.HOLD, 6, 1050, 470, 220, 190, 1.04,
            hold_body_confidence=1.0,
        )
    ], now=1.04)
    assert [action.kind for action in started] == [ActionKind.DOWN]

    planner.update([
        ObservedNote(
            NoteKind.HOLD, 4, 730, 520, 80, 60, 1.30,
            hold_body_confidence=1.0,
        )
    ], now=1.30)
    restarted = planner.update([
        ObservedNote(
            NoteKind.HOLD, 6, 1060, 470, 240, 190, 1.70,
            hold_body_confidence=1.0,
        )
    ], now=1.70)

    assert [action.kind for action in restarted] == [
        ActionKind.UP,
        ActionKind.DOWN,
    ]


def test_hold_start_right_after_a_hit_on_the_same_lane_is_an_effect():
    planner = RealtimePlanner(
        judgement_y=565, rescue_first_visible=True, timing_offset_ms=0
    )
    planner.update([_note(NoteKind.TAP, 2, 520, 1.00)], now=1.00)
    fired = planner.update([_note(NoteKind.TAP, 2, 570, 1.05)], now=1.05)
    assert [action.kind for action in fired] == [ActionKind.TAP]

    # The perfect-hit flash: a tall green beam first seen at the line,
    # right after the tap. It must not start a hold, on either frame.
    for timestamp in (1.10, 1.25):
        effect = planner.update([
            ObservedNote(NoteKind.HOLD, 2, 490, 560, 60, 120, timestamp)
        ], now=timestamp)
        assert not [action for action in effect if action.kind == ActionKind.DOWN]

    # A genuinely tracked falling hold still starts once the window ends.
    planner.update([ObservedNote(NoteKind.HOLD, 2, 490, 480, 60, 100, 1.45)], now=1.45)
    down = planner.update([ObservedNote(NoteKind.HOLD, 2, 490, 530, 60, 100, 1.55)], now=1.55)
    assert [action.kind for action in down] == [ActionKind.DOWN]


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


def test_planner_swipes_the_release_when_the_hold_tail_is_a_flick():
    planner = RealtimePlanner(judgement_y=620, timing_offset_ms=0)
    planner.update([ObservedNote(NoteKind.HOLD, 2, 490, 540, 60, 100, 1.0, 1.0, True)], now=1.0)
    down = planner.update([ObservedNote(NoteKind.HOLD, 2, 490, 575, 60, 100, 1.1, 1.0, True)], now=1.1)
    up = planner.update([ObservedNote(NoteKind.HOLD, 2, 490, 689, 60, 100, 1.5, 1.0, True)], now=1.5)

    assert [action.kind for action in down] == [ActionKind.DOWN]
    assert [(action.kind, action.reason, action.contact) for action in up] == [
        (ActionKind.FLICK, "tail-crossing", 2)
    ]


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


def test_centered_note_next_to_a_hold_is_not_an_artifact():
    planner = RealtimePlanner(
        judgement_y=565, rescue_first_visible=True, timing_offset_ms=0
    )
    planner.update([ObservedNote(NoteKind.HOLD, 2, 490, 480, 60, 100, 1.0)], now=1.0)
    down = planner.update([ObservedNote(NoteKind.HOLD, 2, 490, 530, 60, 100, 1.1)], now=1.1)
    assert [action.kind for action in down] == [ActionKind.DOWN]

    # A real note first seen at the line on the adjacent lane, near its own
    # centre: late, but real - it must be rescued, not killed as an artifact.
    centered = planner.update([
        ObservedNote(NoteKind.TAP, 1, 340, 560, 60, 30, 1.2)
    ], now=1.2)
    assert [(action.kind, action.lane) for action in centered] == [
        (ActionKind.TAP, 1)
    ]

    # A fragment hugging the edge toward the hold stays suppressed.
    edge = planner.update([
        ObservedNote(NoteKind.TAP, 3, 597, 560, 60, 30, 1.3)
    ], now=1.3)
    assert edge == []


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


def _start_sliding_hold(planner):
    """Start a hold on lane 5, slide it to lane 6, release at the tail."""
    planner.update([ObservedNote(NoteKind.HOLD, 5, 940, 480, 60, 100, 1.0)], now=1.0)
    planner.update([ObservedNote(NoteKind.HOLD, 5, 940, 530, 60, 100, 1.1)], now=1.1)
    planner.update([ObservedNote(NoteKind.HOLD, 6, 1090, 540, 60, 100, 1.2)], now=1.2)
    planner.update([ObservedNote(NoteKind.HOLD, 6, 1090, 560, 60, 100, 1.3)], now=1.3)
    return planner.update([ObservedNote(NoteKind.HOLD, 6, 1090, 640, 60, 100, 1.6)], now=1.6)


def test_slide_release_does_not_poison_the_start_lane():
    planner = RealtimePlanner(
        judgement_y=565, rescue_first_visible=True, timing_offset_ms=0
    )
    released = _start_sliding_hold(planner)
    assert [(action.kind, action.lane) for action in released] == [
        (ActionKind.UP, 6)
    ]

    # A real tap crossing on the hold's START lane within 0.4 s of the
    # release must still fire: the finger lifted on lane 6, not lane 5.
    planner.update([_note(NoteKind.TAP, 5, 520, 1.66)], now=1.66)
    planner.update([_note(NoteKind.TAP, 5, 545, 1.70)], now=1.70)
    fired = planner.update([_note(NoteKind.TAP, 5, 568, 1.74)], now=1.74)
    assert [(action.kind, action.lane) for action in fired] == [
        (ActionKind.TAP, 5)
    ]


def test_post_release_window_real_crossing_uses_track_history():
    planner = RealtimePlanner(
        judgement_y=565, rescue_first_visible=True, timing_offset_ms=0
    )
    released = _start_sliding_hold(planner)
    assert [(action.kind, action.lane) for action in released] == [
        (ActionKind.UP, 6)
    ]

    # On the actual release lane a densely sampled real note still fires:
    # its previous sample is only 23 px above the line, but the track
    # history proves the fall.
    planner.update([_note(NoteKind.TAP, 6, 520, 1.66)], now=1.66)
    planner.update([_note(NoteKind.TAP, 6, 545, 1.70)], now=1.70)
    fired = planner.update([_note(NoteKind.TAP, 6, 568, 1.74)], now=1.74)
    assert [(action.kind, action.lane) for action in fired] == [
        (ActionKind.TAP, 6)
    ]

    # Tail residue appearing fresh below the line stays suppressed.
    planner.update([_note(NoteKind.TAP, 6, 558, 1.80)], now=1.80)
    residue = planner.update([_note(NoteKind.TAP, 6, 566, 1.84)], now=1.84)
    assert residue == []


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


def test_tail_flick_releases_survive_a_same_frame_validated_rescue():
    # Live crash realtime-20260727-235942 frame 3516: two chord holds ended
    # as tail-flick swipes in the same frame as a validated rescue TAP on the
    # adjacent lane. The suppression pass kept only the rescue, both release
    # FLICKs vanished, and the leaked finger later crashed the dispatcher
    # with "touch contact N is already active".
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )

    planner.update([
        ObservedNote(NoteKind.HOLD, 2, 490, 510, 70, 105, 1.0),
        ObservedNote(NoteKind.HOLD, 3, 640, 530, 70, 110, 1.0),
    ], now=1.0)
    planner.update([
        ObservedNote(NoteKind.HOLD, 2, 490, 540, 100, 105, 1.2),
        ObservedNote(
            NoteKind.HOLD, 3, 640, 575, 100, 110, 1.2, hold_tail_flick=True
        ),
    ], now=1.2)
    fired = planner.update([
        ObservedNote(
            NoteKind.HOLD, 2, 490, 570, 100, 18, 1.4, hold_tail_flick=True
        ),
        ObservedNote(NoteKind.TAP, 4, 790, 568, 60, 100, 1.4),
    ], now=1.4)

    assert [(a.kind, a.lane, a.contact, a.reason) for a in fired] == [
        (ActionKind.FLICK, 2, 2, "tail-ring"),
        (ActionKind.FLICK, 3, 3, "tail-ring-paired"),
        (ActionKind.TAP, 4, None, "rescue"),
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


def test_falling_flick_promotes_its_separated_ring_fragment():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )

    planner.update([
        ObservedNote(NoteKind.FLICK, 1, 365, 481, 54, 22, 1.00),
    ], now=1.00)
    planner.update([
        ObservedNote(NoteKind.FLICK, 1, 365, 488, 54, 22, 1.02),
    ], now=1.02)
    actions = planner.update([
        # Live trace frame 3530: the chevron is about 75 px above the
        # playable ring. The old fixed 60 px gate let the ring fire as TAP.
        ObservedNote(NoteKind.FLICK, 1, 365, 494.7, 56, 22, 1.04),
        ObservedNote(NoteKind.TAP, 1, 357.7, 569.9, 92, 20, 1.04),
    ], now=1.04)

    assert [(action.kind, action.lane, action.reason) for action in actions] == [
        (ActionKind.FLICK, 1, "rescue")
    ]


def test_close_same_lane_tap_is_not_absorbed_by_a_flick():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )

    planner.update([
        ObservedNote(NoteKind.FLICK, 2, 490, 500, 54, 22, 1.00),
        ObservedNote(NoteKind.TAP, 2, 490, 520, 60, 18, 1.00),
    ], now=1.00)
    planner.update([
        ObservedNote(NoteKind.FLICK, 2, 490, 525, 60, 22, 1.02),
        ObservedNote(NoteKind.TAP, 2, 490, 545, 70, 18, 1.02),
    ], now=1.02)
    tap = planner.update([
        ObservedNote(NoteKind.FLICK, 2, 490, 545, 70, 22, 1.04),
        ObservedNote(NoteKind.TAP, 2, 490, 565, 80, 20, 1.04),
    ], now=1.04)

    assert [(action.kind, action.lane) for action in tap] == [
        (ActionKind.TAP, 2)
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
        (ActionKind.TAP, "crossing")
    ]


def test_tracked_note_keeps_dispatch_lead_when_profile_offset_is_negative():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=-20, rescue_first_visible=True
    )

    planner.update([_note(NoteKind.TAP, 2, 540, 1.00)], now=1.00)
    before = planner.update(
        [_note(NoteKind.TAP, 2, 561, 1.02)], now=1.02
    )
    due = planner.update(
        [_note(NoteKind.TAP, 2, 562, 1.03)], now=1.03
    )

    assert [(action.kind, action.reason) for action in before] == [
        (ActionKind.TAP, "crossing")
    ]
    assert due == []


def test_low_capture_fps_predicts_a_crossing_before_the_next_frame():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=-11, rescue_first_visible=True
    )
    for timestamp in (1.00, 1.03, 1.06, 1.09):
        planner.update([], now=timestamp)
    planner.update([
        ObservedNote(NoteKind.TAP, 2, 490, 530, 80, 18, 1.12)
    ], now=1.12)

    predicted = planner.update([
        ObservedNote(NoteKind.TAP, 2, 490, 556, 80, 18, 1.15)
    ], now=1.15)

    assert [(action.kind, action.reason) for action in predicted] == [
        (ActionKind.TAP, "crossing")
    ]


def test_moving_predictive_line_cannot_lose_a_physical_crossing():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=-11, rescue_first_visible=True
    )
    planner._frame_interval_seconds = .006
    planner.update([
        ObservedNote(NoteKind.TAP, 2, 490, 547, 80, 18, 1.00)
    ], now=1.00)
    before_line = planner.update([
        ObservedNote(NoteKind.TAP, 2, 490, 557, 80, 18, 1.016)
    ], now=1.016)
    assert before_line == []

    # A capture stall moves the predictive target above previous_y. The note
    # still visibly crosses the immutable game judgement line and must fire.
    planner._frame_interval_seconds = .025
    crossing = planner.update([
        ObservedNote(NoteKind.TAP, 2, 490, 567, 80, 18, 1.048)
    ], now=1.048)

    assert [(action.kind, action.reason) for action in crossing] == [
        (ActionKind.TAP, "crossing")
    ]


def test_dropout_prediction_uses_last_seen_after_duplicate_frames():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )
    for timestamp, y in (
        (1.00, 430),
        (1.04, 470),
        (1.08, 510),
        (1.12, 540),
    ):
        planner.update([
            ObservedNote(NoteKind.TAP, 4, 790, y, 78, 16, timestamp)
        ], now=timestamp)
    # Identical frames refresh the real last-seen time without adding motion
    # samples or changing the sample's older timestamp.
    planner.update([
        ObservedNote(NoteKind.TAP, 4, 790, 540, 78, 16, 1.15)
    ], now=1.15)
    planner.update([
        ObservedNote(NoteKind.TAP, 4, 790, 540, 78, 16, 1.18)
    ], now=1.18)

    assert planner.update([], now=1.19) == []
    rescued = planner.update([], now=1.21)
    repeated = planner.update([], now=1.23)

    assert [(action.kind, action.lane, action.reason) for action in rescued] == [
        (ActionKind.TAP, 4, "predicted-dropout-rescue")
    ]
    assert repeated == []


def test_two_sample_effect_jump_cannot_inherit_a_falling_note_track():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )

    planner.update([
        ObservedNote(NoteKind.TAP, 2, 558, 508, 52, 38, 1.00)
    ], now=1.00)
    effect = planner.update([
        ObservedNote(NoteKind.TAP, 2, 494, 574.4, 58, 14, 1.08)
    ], now=1.08)

    assert effect == []


def test_line_glow_cannot_preempt_an_upstream_same_lane_head():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )
    planner.update([
        ObservedNote(NoteKind.TAP, 6, 1015, 470, 104, 28, 1.00)
    ], now=1.00)
    planner.update([
        ObservedNote(NoteKind.TAP, 6, 1020, 490, 90, 22, 1.02)
    ], now=1.02)

    protected = planner.update([
        ObservedNote(NoteKind.TAP, 6, 1027, 520, 64, 14, 1.04),
        ObservedNote(NoteKind.TAP, 6, 1082, 572, 58, 14, 1.04),
    ], now=1.04)
    crossed = planner.update([], now=1.08)

    assert protected == []
    assert [
        (action.kind, action.lane, action.reason) for action in crossed
    ] == [
        (ActionKind.TAP, 6, "predicted-crossing-rescue")
    ]


def test_tracked_line_effect_cannot_preempt_an_established_flick():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )
    planner.update([
        ObservedNote(NoteKind.FLICK, 1, 367, 227, 56, 22, 1.00)
    ], now=1.00)
    planner.update([
        ObservedNote(NoteKind.FLICK, 1, 367, 350, 56, 22, 1.04),
        ObservedNote(NoteKind.TAP, 1, 360, 524, 74, 38, 1.04),
    ], now=1.04)
    planner.update([
        ObservedNote(NoteKind.FLICK, 1, 367, 420, 56, 22, 1.08),
        ObservedNote(NoteKind.TAP, 1, 360, 545, 74, 38, 1.08),
    ], now=1.08)

    protected = planner.update([
        ObservedNote(NoteKind.FLICK, 1, 367, 481, 56, 22, 1.12),
        ObservedNote(NoteKind.TAP, 1, 360, 569, 74, 38, 1.12),
    ], now=1.12)
    flick = planner.update([
        ObservedNote(NoteKind.FLICK, 1, 367, 570, 56, 22, 1.16)
    ], now=1.16)

    assert protected == []
    assert [(action.kind, action.lane) for action in flick] == [
        (ActionKind.FLICK, 1)
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
        (ActionKind.TAP, 2, "crossing")
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
    # TAP EFFECT 1 residue in the failing SAVIOR OF SONG trace first appeared
    # about 0.50 s after release, so the guard must cover more than 0.4 s.
    lingering_tail = planner.update([
        ObservedNote(NoteKind.TAP, 4, 790, 562, 100, 18, 2.50)
    ], now=2.50)

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


def test_confirmed_hold_can_restart_after_crossing_from_y_553():
    planner = RealtimePlanner(
        judgement_y=565, timing_offset_ms=0, rescue_first_visible=True
    )
    planner.update([
        ObservedNote(NoteKind.HOLD, 0, 190, 405, 100, 320, 1.0)
    ], now=1.0)
    released = planner.update([
        ObservedNote(NoteKind.HOLD, 0, 190, 572, 100, 18, 2.0)
    ], now=2.0)
    assert [action.kind for action in released] == [ActionKind.UP]

    # A later, independently tracked body crosses from head y=553 to y=567
    # in one 31 ms frame. This is continuous high-confidence hold evidence,
    # even though it did not pass through the obsolete y<=540 restart gate.
    planner.update([
        ObservedNote(NoteKind.TAP, 0, 190, 562, 70, 18, 4.800)
    ], now=4.800)
    planner.update([
        ObservedNote(NoteKind.HOLD, 0, 190, 455, 196, 196, 5.000)
    ], now=5.000)
    restarted = planner.update([
        ObservedNote(NoteKind.HOLD, 0, 190, 473, 188, 188, 5.031)
    ], now=5.031)

    assert [(action.kind, action.lane, action.reason) for action in restarted] == [
        (ActionKind.DOWN, 0, "crossing")
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
