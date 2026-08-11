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
