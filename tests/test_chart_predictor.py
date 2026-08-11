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
    at_tail = planner.update([], anchor + 7.42)
    assert not [a for a in at_tail if a.reason == "chart-tail"]


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
