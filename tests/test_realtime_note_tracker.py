from agent.realtime.note_detector import NoteKind, ObservedNote
from agent.realtime.note_tracker import MultiNoteTracker


def note(y, timestamp, *, x=340, width=80, height=18):
    return ObservedNote(NoteKind.TAP, 1, x, y, width, height, timestamp)


def test_tracker_keeps_two_same_lane_notes_as_independent_tracks():
    tracker = MultiNoteTracker(memory_seconds=.3)

    first = tracker.update([note(500, 0), note(430, 0)], 0)
    second = tracker.update([note(520, .02), note(450, .02)], .02)

    assert len(first) == 2
    assert [item.track_id for item in second] == [item.track_id for item in first]
    assert all(900 <= item.velocity_y <= 1100 for item in second)


def test_tracker_does_not_merge_dense_same_lane_heads_twelve_pixels_apart():
    tracker = MultiNoteTracker(memory_seconds=.3)

    tracked = tracker.update([
        note(500, 0, x=340, width=72, height=16),
        note(512, 0, x=342, width=76, height=18),
    ], 0)

    assert len(tracked) == 2
    assert tracked[0].track_id != tracked[1].track_id


def test_tracker_clusters_detector_fragments_before_assigning_ids():
    tracker = MultiNoteTracker(memory_seconds=.3)

    tracked = tracker.update([
        note(420, 0, x=320, width=55, height=8),
        note(428, 0, x=350, width=90, height=22),
    ], 0)

    assert len(tracked) == 1


def test_tracker_clusters_widely_split_left_and_right_feedback_halves():
    tracker = MultiNoteTracker(memory_seconds=.3)

    tracked = tracker.update([
        note(578, 0, x=878, width=90, height=4),
        note(577, 0, x=976, width=82, height=10),
    ], 0)

    assert len(tracked) == 1


def test_duplicate_frame_does_not_pollute_velocity_regression():
    tracker = MultiNoteTracker(memory_seconds=.3)
    tracker.update([note(400, 0)], 0)
    tracker.update([note(420, .02)], .02)
    duplicate = tracker.update([note(420, .03)], .03)[0]
    fresh = tracker.update([note(440, .04)], .04)[0]

    assert duplicate.sample_count == 2
    assert fresh.sample_count == 3
    assert 900 <= fresh.velocity_y <= 1100


def test_tracker_exposes_origin_motion_and_trigger_metadata():
    tracker = MultiNoteTracker(memory_seconds=.3)
    first = tracker.update([note(470, 1.0)], 1.0)[0]
    second = tracker.update([note(520, 1.05)], 1.05)[0]
    tracker.mark_fired(second.track_id, 1.05)
    fired = tracker.update([note(560, 1.10)], 1.10)[0]

    assert first.first_y == 470
    assert second.minimum_y == 470
    assert second.motion_samples == 2
    assert second.downward_motion_frames == 1
    assert fired.downward_motion_frames == 2
    assert fired.fired
    assert fired.last_fired_at == 1.05


def test_slide_chart_merges_boundary_hugging_adjacent_lane_fragments():
    tracker = MultiNoteTracker(
        memory_seconds=.3,
        keep_downward_on_jitter=True,
    )
    tracked = tracker.update([
        # One physical note split at the lane 2/3 boundary: both fragments hug
        # the shared boundary instead of sitting at their lane centres.
        ObservedNote(NoteKind.TAP, 2, 560, 500, 50, 16, 0),
        ObservedNote(NoteKind.TAP, 3, 585, 502, 55, 18, 0),
    ], 0)

    assert len(tracked) == 1
    assert tracked[0].note.lane == 3


def test_slide_chart_keeps_real_adjacent_chord_partners_separate():
    tracker = MultiNoteTracker(
        memory_seconds=.3,
        keep_downward_on_jitter=True,
    )
    tracked = tracker.update([
        ObservedNote(NoteKind.TAP, 2, 500, 500, 50, 16, 0),
        ObservedNote(NoteKind.TAP, 3, 630, 502, 55, 18, 0),
    ], 0)

    assert len(tracked) == 2


def test_plain_chart_does_not_merge_adjacent_lane_fragments():
    tracker = MultiNoteTracker(memory_seconds=.3)
    tracked = tracker.update([
        ObservedNote(NoteKind.TAP, 2, 560, 500, 50, 16, 0),
        ObservedNote(NoteKind.TAP, 3, 585, 502, 55, 18, 0),
    ], 0)

    assert len(tracked) == 2
