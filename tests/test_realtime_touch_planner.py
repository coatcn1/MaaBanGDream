from __future__ import annotations

import json
from pathlib import Path

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


def test_released_hold_cannot_restart_from_clear_lingering_body_after_window():
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
        ObservedNote(NoteKind.HOLD, 4, 790, 520, 100, 100, 2.6)
    ], now=2.6)

    assert lingering == []


def test_latest_thin_hold_replay_starts_after_upper_origin_survives_gap():
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
    ] == [(ActionKind.DOWN, 6, "upper-origin-rescue")]


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


def test_upper_origin_hold_tracks_perspective_body_until_real_tail():
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

    assert [(action.kind, action.reason) for action in down] == [
        (ActionKind.DOWN, "upper-origin-rescue")
    ]
    assert planner.update([
        ObservedNote(NoteKind.HOLD, 5, 655, 51, 22, 24, .40),
        # The bright playable head persists for another frame. It is not the
        # far tail and must never produce the 32 ms release seen on device.
        ObservedNote(NoteKind.HOLD, 5, 954, 570, 80, 12, .40),
    ], .40) == []
    for timestamp, y in ((.45, 55), (.50, 60)):
        assert planner.update([
            ObservedNote(
                NoteKind.HOLD, 5, 655, y, 22, 24, timestamp
            )
        ], timestamp) == []

    assert planner.update([], .70) == []
    released = planner.update([
        ObservedNote(NoteKind.HOLD, 5, 954, 570, 80, 12, 1.0)
    ], 1.0)
    assert [(action.kind, action.reason) for action in released] == [
        (ActionKind.UP, "tail-ring")
    ]


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
