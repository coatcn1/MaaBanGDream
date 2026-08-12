from __future__ import annotations

from agent.realtime.chart_predictor import ChartPredictor
from agent.realtime.chart_timeline import ChartJudgement, ChartTimeline
from agent.realtime.note_detector import NoteKind, ObservedNote
from agent.realtime.touch_planner import ActionKind, RealtimePlanner, TouchAction


def _synthetic_chart() -> ChartTimeline:
    # 192 BPM: beat -> seconds = beat * 0.3125.
    judgements = [
        ChartJudgement(2.03125, 1, "tap", 0),
        ChartJudgement(2.1875, 2, "tap", 1),
        ChartJudgement(2.34375, 5, "tap", 2),
        ChartJudgement(2.5, 6, "tap", 3),
        ChartJudgement(2.8125, 0, "tap", 4),
        ChartJudgement(3.125, 4, "tap", 5),
        ChartJudgement(3.4375, 1, "tap", 6),
        ChartJudgement(3.75, 3, "tap", 7),
        ChartJudgement(4.0625, 5, "hold-head", 8),
        ChartJudgement(4.375, 5, "hold-tail", 8),
    ]
    return ChartTimeline(judgements, bpm=192.0)


def test_calibration_succeeds_when_chart_matches():
    chart = _synthetic_chart()
    predictor = ChartPredictor(chart, min_calibration_samples=6)
    predictor._anchor_time = 0.0
    # Engine starts 3.0 s after the song beat grid: song = engine - 3.0.
    actions = []
    for judgement in chart.judgements:
        if judgement.kind == "hold-tail":
            continue
        kind = (
            ActionKind.DOWN
            if judgement.kind == "hold-head"
            else ActionKind.TAP
        )
        actions.append(TouchAction(
            kind,
            judgement.lane,
            judgement.time_s + 3.0,
            reason="crossing",
        ))
    predictor._feed_calibration(actions)
    assert predictor.calibrated
    assert abs(predictor.song_offset_s - (-3.0)) <= 0.02


def test_calibration_fails_closed_on_unrelated_actions():
    chart = _synthetic_chart()
    predictor = ChartPredictor(chart, min_calibration_samples=4)
    predictor._anchor_time = 0.0
    actions = []
    for index in range(20):
        actions.append(TouchAction(
            ActionKind.TAP, index % 7, 1.0 + index * 0.2, reason="crossing",
        ))
    predictor._feed_calibration(actions)
    assert predictor.calibration_failed
    assert not predictor.calibrated


def _planner_with_calibrated_predictor(chart: ChartTimeline):
    planner = RealtimePlanner(
        judgement_y=565,
        timing_offset_ms=0,
        rescue_first_visible=True,
        enable_slide=True,
        chart_timeline=chart,
        chart_prediction=True,
    )
    predictor = planner._chart_predictor
    assert predictor is not None
    predictor.calibrated = True
    predictor.song_offset_s = -3.0
    return planner, predictor


def test_chart_tail_releases_hold_whose_body_vanished():
    chart = _synthetic_chart()
    planner, predictor = _planner_with_calibrated_predictor(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    # Hold head at song 4.0625 -> engine anchor + 7.0625; tail at song
    # 4.375 -> engine anchor + 7.375.
    head_engine = anchor + 7.0625
    started = planner.update([
        ObservedNote(
            NoteKind.HOLD, 5, 940, 470, 100, 200, head_engine - 0.05,
        )
    ], head_engine)
    assert [action.kind for action in started] == [ActionKind.DOWN]

    # Body vanishes.  Before the chart tail time the hold must stay down.
    before = planner.update([], anchor + 7.35)
    assert not [a for a in before if a.kind == ActionKind.UP]
    released = planner.update([], anchor + 7.42)
    assert [(a.kind, a.reason) for a in released] == [
        (ActionKind.UP, "chart-tail")
    ]


def test_chart_tail_releases_visible_body_at_chart_time():
    chart = _synthetic_chart()
    planner, predictor = _planner_with_calibrated_predictor(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    head_engine = anchor + 7.0625
    started = planner.update([
        ObservedNote(
            NoteKind.HOLD, 5, 940, 470, 100, 200, head_engine - 0.05,
        )
    ], head_engine)
    assert [action.kind for action in started] == [ActionKind.DOWN]

    # At the chart tail time the finger is on the tail lane: release exactly
    # on time even though the body is still visible.
    before = planner.update([
        ObservedNote(
            NoteKind.HOLD, 5, 940, 490, 100, 190, anchor + 7.36,
        )
    ], anchor + 7.36)
    assert not [a for a in before if a.kind == ActionKind.UP]
    at_tail = planner.update([], anchor + 7.42)
    assert [(a.kind, a.reason) for a in at_tail] == [
        (ActionKind.UP, "chart-tail")
    ]


def test_chart_tail_does_not_release_slide_before_finger_reaches_tail_lane():
    chart = _synthetic_chart()
    planner, predictor = _planner_with_calibrated_predictor(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    # Start a slide on lane 5 (head at song 4.0625).  The chart tail is at
    # song 4.375 on lane 5, but keep the finger on lane 5 (straight hold in
    # the synthetic chart); simulate a slide by moving the active lane.
    head_engine = anchor + 7.0625
    started = planner.update([
        ObservedNote(
            NoteKind.HOLD, 5, 940, 470, 100, 200, head_engine - 0.05,
        )
    ], head_engine)
    assert [action.kind for action in started] == [ActionKind.DOWN]
    # Force the active hold onto lane 3 (finger has not reached lane 5).
    planner._state._active_hold_lane[5] = 3
    # Before the move lead the chart must not touch the contact.
    before_lead = planner.update([], anchor + 7.10)
    assert not [a for a in before_lead if a.reason == "chart-tail"]
    assert not [a for a in before_lead if a.reason == "chart-tail-move"]
    # Near the tail the chart moves the finger to the tail lane and releases
    # in the same frame even though the slide body is not visible.
    at_tail = planner.update([], anchor + 7.42)
    moves = [a for a in at_tail if a.reason == "chart-tail-move"]
    releases = [a for a in at_tail if a.reason == "chart-tail"]
    assert len(moves) == 1 and moves[0].lane == 5
    assert len(releases) == 1 and releases[0].lane == 5


def _planner_with_press_rescue(chart: ChartTimeline):
    planner = RealtimePlanner(
        judgement_y=565,
        timing_offset_ms=0,
        rescue_first_visible=True,
        enable_slide=True,
        chart_timeline=chart,
        chart_prediction=True,
        chart_predict_presses=True,
    )
    predictor = planner._chart_predictor
    assert predictor is not None
    predictor.calibrated = True
    predictor.song_offset_s = -3.0
    return planner, predictor


def test_chart_press_rescues_tap_only_after_due_time():
    chart = _synthetic_chart()
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    # Lane 1 tap at song 2.03125 -> engine anchor + 5.03125.  Before the
    # due time the chart must not preempt the detector.
    early = planner.update([], anchor + 4.99)
    assert not [a for a in early if a.reason == "chart-predicted"]

    # Right after the due time, with no detector action, the rescue fires.
    at_due = planner.update([], anchor + 5.07)
    rescued = [a for a in at_due if a.reason == "chart-predicted"]
    assert len(rescued) == 1
    assert rescued[0].lane == 1
    assert rescued[0].kind == ActionKind.TAP

    # A second frame in the same lane window must not double-press.
    again = planner.update([], anchor + 5.09)
    assert not [a for a in again if a.reason == "chart-predicted"]


def test_chart_press_does_not_rescue_when_crossing_covered_the_note():
    chart = _synthetic_chart()
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    # A real detector crossing at the chart time (song 2.03125 -> engine
    # anchor + 5.03125) already dispatched a tap on lane 1; the chart must
    # stay quiet instead of adding a second press.
    planner._state._last_trigger[1] = anchor + 5.03
    planner._state._last_trigger_action_kind[1] = ActionKind.TAP
    after = planner.update([], anchor + 5.08)
    assert not [a for a in after if a.reason == "chart-predicted"]


def test_chart_press_suppresses_duplicate_crossing_on_same_lane():
    chart = _synthetic_chart()
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    # The chart rescues the lane-1 tap after due (song 2.03125 -> engine
    # anchor + 5.03125).  A detector crossing fragment arriving ~80 ms later
    # is the same note and must be filtered, not dispatched twice.
    at_due = planner.update([], anchor + 5.07)
    assert [a.reason for a in at_due if a.kind == ActionKind.TAP] == [
        "chart-predicted",
    ]
    # The delayed fragment keeps descending; its crossing is the same note
    # and must be filtered by the chart-press suppression.
    now = anchor + 5.10
    for y in (420, 440, 460, 480, 500, 520, 540, 555, 563, 568):
        now += 0.016
        late = planner.update([
            ObservedNote(NoteKind.TAP, 1, 430, y, 20, 10, now),
        ], now=now)
    assert not [a for a in late if a.reason == "crossing"]


def test_chart_presses_occluded_straight_hold_head_without_body():
    chart = _synthetic_chart()
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    # Hold head at song 4.0625 -> engine anchor + 7.0625; straight tail at
    # song 4.375 on the same lane.  With no visible body the chart presses
    # the head after due and releases it on the same lane at the tail time.
    pressed = planner.update([], anchor + 7.10)
    downs = [a for a in pressed if a.kind == ActionKind.DOWN]
    assert len(downs) == 1
    assert downs[0].lane == 5
    assert downs[0].reason == "chart-predicted"

    before_tail = planner.update([], anchor + 7.35)
    assert not [a for a in before_tail if a.kind == ActionKind.UP]
    at_tail = planner.update([], anchor + 7.42)
    assert [(a.kind, a.reason) for a in at_tail] == [
        (ActionKind.UP, "chart-tail"),
    ]


def test_chart_does_not_blind_press_slide_hold():
    # Slide: head on lane 5, tail on lane 3.  Without a visible body the
    # finger cannot follow the slide, so the chart must not blind-press.
    chart = ChartTimeline([
        ChartJudgement(2.0, 5, "hold-head", 0),
        ChartJudgement(2.5, 3, "hold-tail", 0),
    ], bpm=192.0)
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    pressed = planner.update([], anchor + 2.08)
    assert not [a for a in pressed if a.kind == ActionKind.DOWN]


def test_chart_blind_press_handles_recent_tap_on_lane():
    chart = _synthetic_chart()
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    # A tap within the same instant is the same note: no double press.
    planner._state._last_trigger[5] = anchor + 7.07
    planner._state._last_trigger_action_kind[5] = ActionKind.TAP
    pressed = planner.update([], anchor + 7.10)
    assert not [a for a in pressed if a.kind == ActionKind.DOWN]

    # A tap ~80 ms earlier is the same head misdetected as a tap (the chart
    # has no tap on this lane near the head), so the blind press must still
    # fire instead of letting the hold head slip.
    planner._state._last_trigger[5] = anchor + 7.02
    pressed = planner.update([], anchor + 7.10)
    assert [a.kind for a in pressed if a.reason == "chart-predicted"] == [
        ActionKind.DOWN,
    ]


def test_chart_tail_does_not_lift_partner_before_its_own_slide_tail():
    # Two simultaneous slide holds: lane0->lane2 and lane4->lane6, with the
    # second tail 33 ms later.  Releasing the first must not lift the second
    # as a chord partner before its own chart tail.
    chart = ChartTimeline([
        ChartJudgement(2.0, 0, "hold-head", 0),
        ChartJudgement(2.4, 2, "hold-tail", 0),
        ChartJudgement(2.0, 4, "hold-head", 1),
        ChartJudgement(2.5, 6, "hold-tail", 1),
    ], bpm=192.0)
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor
    state = planner._state

    state._active_hold_tail[0] = 500.0
    state._active_hold_tail[4] = 500.0
    state._active_hold_lane[0] = 2
    state._active_hold_lane[4] = 5
    state._hold_started[0] = anchor + 5.0
    state._hold_started[4] = anchor + 5.0
    state._hold_confirmed.update((0, 4))
    predictor.expected_hold_tail[0] = (2.4, 2)
    predictor.expected_hold_tail[4] = (2.5, 6)
    state._hold_chord_partner[0] = 4
    state._hold_chord_partner[4] = 0

    # engine anchor + 5.43 -> song 2.43: contact 0 tail due, contact 4 not.
    first = planner.update([], anchor + 5.43)
    ups0 = [a for a in first if a.kind == ActionKind.UP and a.contact == 0]
    ups4 = [a for a in first if a.kind == ActionKind.UP and a.contact == 4]
    assert [a.reason for a in ups0] == ["chart-tail"]
    assert not ups4
    assert 4 in state._active_hold_tail
    assert 0 not in state._hold_chord_partner
    assert 4 not in state._hold_chord_partner

    # engine anchor + 5.53 -> song 2.53: contact 4's own slide tail is due;
    # the chart moves the finger to lane 6 and releases it there.
    second = planner.update([], anchor + 5.53)
    moves = [a for a in second if a.reason == "chart-tail-move"]
    ups4 = [a for a in second if a.kind == ActionKind.UP and a.contact == 4]
    assert len(moves) == 1 and moves[0].lane == 6
    assert [a.reason for a in ups4] == ["chart-tail"]
    assert ups4[0].lane == 6


def test_hold_tail_ring_on_wrong_lane_waits_for_chart_tail():
    # Slide lane0 -> lane2.  A tail ring appears on lane 1 (wrong lane): the
    # hold pipeline must not release there; the chart moves the finger to
    # lane 2 and releases at the chart tail time.
    chart = ChartTimeline([
        ChartJudgement(2.0, 0, "hold-head", 0),
        ChartJudgement(2.4, 2, "hold-tail", 0),
    ], bpm=192.0)
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor
    state = planner._state

    state._active_hold_tail[0] = 500.0
    state._active_hold_lane[0] = 1
    state._hold_started[0] = anchor + 5.0
    state._hold_confirmed.add(0)
    predictor.expected_hold_tail[0] = (2.4, 2)
    state._chart_tail_lane[0] = 2

    # engine anchor + 5.20 -> song 2.20: tail ring on lane 1, not due yet.
    ring = planner.update([
        ObservedNote(NoteKind.HOLD, 1, 480, 570, 80, 20, anchor + 5.20),
    ], now=anchor + 5.20)
    assert not [a for a in ring if a.kind == ActionKind.UP]

    # engine anchor + 5.42 -> song 2.42: chart tail due; move to lane 2 and
    # release there in the same frame.
    at_tail = planner.update([], anchor + 5.42)
    moves = [a for a in at_tail if a.reason == "chart-tail-move"]
    releases = [a for a in at_tail if a.reason == "chart-tail"]
    assert len(moves) == 1 and moves[0].lane == 2
    assert len(releases) == 1 and releases[0].lane == 2


def test_phantom_hold_lane_is_freed_before_a_due_chart_head():
    chart = _synthetic_chart()
    planner, predictor = _planner_with_calibrated_predictor(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    # A phantom hold starts on lane 3 (no chart head near its start) at song
    # 1.0 (engine anchor + 4.0 with offset -3.0), then drifts onto lane 5
    # where the chart has a head at 4.0625 (engine anchor + 7.06).
    started = planner.update([
        ObservedNote(NoteKind.HOLD, 3, 640, 470, 100, 200, anchor + 4.0)
    ], now=anchor + 4.0)
    assert [action.kind for action in started] == [ActionKind.DOWN]
    planner._state._active_hold_lane[3] = 5

    # Before the due head the phantom stays; at the head time it is released
    # and the restart cooldown is cleared so the real body can start.
    before = planner.update([], anchor + 6.8)
    assert not [a for a in before if a.reason == "chart-lane-free"]
    at_head = planner.update([], anchor + 7.08)
    assert [a.reason for a in at_head if a.kind == ActionKind.UP] == [
        "chart-lane-free"
    ]
    assert 5 not in planner._state._hold_released_at

    # The real body on lane 5 now starts immediately.
    real = planner.update([
        ObservedNote(NoteKind.HOLD, 5, 940, 470, 100, 200, anchor + 7.1)
    ], now=anchor + 7.1)
    assert [action.kind for action in real] == [ActionKind.DOWN]
