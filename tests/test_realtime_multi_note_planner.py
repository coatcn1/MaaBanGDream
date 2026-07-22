from agent.realtime.note_detector import NoteKind, ObservedNote
from agent.realtime.touch_planner import ActionKind, RealtimePlanner


def note(y, timestamp):
    return ObservedNote(NoteKind.TAP, 1, 340, y, 80, 18, timestamp)


def test_two_same_lane_notes_each_trigger_once():
    planner = RealtimePlanner(judgement_y=565, timing_offset_ms=0)

    planner.update([note(520, 0), note(450, 0)], 0)
    first = planner.update([note(570, .05), note(500, .05)], .05)
    second = planner.update([note(610, .09), note(570, .09)], .09)
    repeated = planner.update([note(620, .10), note(580, .10)], .10)

    assert [a.kind for a in first] == [ActionKind.TAP]
    assert [a.kind for a in second] == [ActionKind.TAP]
    assert repeated == []
