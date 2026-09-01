from __future__ import annotations

from agent.realtime.chart_predictor import ChartPredictor
from agent.realtime.chart_timeline import (
    ChartHoldPath,
    ChartJudgement,
    ChartPathPoint,
    ChartTimeline,
)
from agent.realtime.note_detector import NoteKind, ObservedNote
from agent.realtime.note_tracker import TrackedNote
from agent.realtime.touch_planner import ActionKind, RealtimePlanner, TouchAction
from agent.realtime.touch_planner.geometry import lane_center_x


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


def test_post_lock_phase_refinement_total_is_bounded():
    # 密集段落视觉残差带系统性偏差；无界小步精修曾在整局漂移 40ms+。
    # 锁定后总修正必须封顶 ±6ms。
    chart = _synthetic_chart()
    predictor = ChartPredictor(chart)
    predictor.calibrated = True
    predictor._calibrated_at_relative_s = 0.0
    for _ in range(40):
        for lane in (0, 1, 2, 3):
            predictor._record_phase_residual(
                0.040,
                lane,
                relative_time_s=0.0,
            )
    assert abs(predictor.song_offset_s) <= 0.006 + 1e-9


def test_post_lock_phase_refinement_stops_after_window():
    chart = _synthetic_chart()
    predictor = ChartPredictor(chart)
    predictor.calibrated = True
    predictor._calibrated_at_relative_s = 0.0
    for lane in (0, 1, 2, 3):
        predictor._record_phase_residual(
            0.040,
            lane,
            relative_time_s=60.0,
        )
    assert predictor.song_offset_s == 0.0


def _phase_track(track_id, lane, now, *, crossing_in=0.5):
    note = ObservedNote(
        NoteKind.TAP, lane, 300 + lane * 100,
        565 - crossing_in * 200, 20, 10, now,
    )
    return TrackedNote(
        track_id, note, note.y - 10, note.x, 200.0, 4, False,
        note.y - 30, note.y - 30, 4, 3, None, now,
    )


def test_upstream_tracks_lock_phase_with_six_samples_two_lanes_and_low_mad():
    judgements = [
        ChartJudgement(time_s, lane, "tap", index)
        for index, (time_s, lane) in enumerate([
            (2.0, 0), (2.37, 3), (2.91, 1),
            (3.58, 5), (4.04, 2), (4.77, 6),
        ])
    ]
    predictor = ChartPredictor(
        ChartTimeline(judgements, bpm=120.0), min_calibration_samples=6,
    )
    predictor._anchor_time = 100.0
    for index, judgement in enumerate(judgements, 1):
        # song = engine-relative - 3.0; track predicts crossing in 0.5 s.
        now = 100.0 + judgement.time_s + 3.0 - 0.5
        predictor.observe_tracks([
            _phase_track(index, judgement.lane, now),
        ], now)

    assert predictor.calibrated
    assert abs(predictor.song_offset_s + 3.0) <= 0.020


def test_upstream_tracks_on_only_one_lane_do_not_take_chart_control():
    chart = ChartTimeline([
        ChartJudgement(2.0 + index * 0.5, 0, "tap", index)
        for index in range(6)
    ], bpm=120.0)
    predictor = ChartPredictor(chart, min_calibration_samples=6)
    predictor._anchor_time = 100.0
    for index, judgement in enumerate(chart.judgements, 1):
        now = 100.0 + judgement.time_s + 3.0 - 0.5
        predictor.observe_tracks([_phase_track(index, 0, now)], now)

    assert not predictor.calibrated


def test_prelock_phase_evidence_rescues_matching_below_line_fragment():
    """Exact-chart evidence may confirm a fragment without taking control.

    The 最高到达点 Expert trace had collected six projected trajectories,
    but their early velocity noise was still too wide for the strict 20 ms
    chart-lock gate.  Its lane-4 note then first appeared at y=576 and was
    discarded as residue, producing the run's only Miss.  Four ordered,
    distinct chart notes around one provisional phase are enough to confirm
    that already-visible fragment, but are not enough to enable chart-owned
    presses.
    """
    judgements = [
        ChartJudgement(5.455, 0, "tap", 0),
        ChartJudgement(5.682, 2, "tap", 1),
        ChartJudgement(5.909, 4, "tap", 2),
        ChartJudgement(6.136, 6, "tap", 3),
    ]
    planner = RealtimePlanner(
        judgement_y=565,
        timing_offset_ms=29,
        rescue_first_visible=True,
        chart_timeline=ChartTimeline(judgements, bpm=132.0),
        chart_prediction=True,
        chart_predict_presses=True,
    )
    predictor = planner._chart_predictor
    assert predictor is not None
    predictor._anchor_time = 100.0
    evidence = [
        (1, 0, 8.429),
        (2, 2, 8.540),
        (3, 2, 8.553),
        (4, 2, 8.708),
        (5, 4, 8.827),
        (6, 6, 8.962),
    ]
    for track_id, lane, crossing_relative in evidence:
        now = 100.0 + crossing_relative - 0.5
        predictor.observe_tracks([
            _phase_track(track_id, lane, now, crossing_in=0.5),
        ], now)

    assert not predictor.calibrated
    unrelated = planner.update([
        ObservedNote(
            NoteKind.TAP, 3, 640, 576.4, 64, 8, 108.75,
        ),
    ], now=108.75)
    rescued = planner.update([
        # Detector classified the real normal note's bottom fragment as FLICK;
        # the exact chart should restore TAP semantics for the dispatched input.
        ObservedNote(
            NoteKind.FLICK, 4, 790, 576.46, 64, 8, 108.797,
        ),
    ], now=108.797)

    assert unrelated == []
    assert [(action.kind, action.lane, action.reason) for action in rescued] == [
        (ActionKind.TAP, 4, "chart-provisional-rescue")
    ]
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
    early = [a for a in before if a.kind == ActionKind.UP]
    assert len(early) == 1
    assert early[0].reason == "chart-tail"
    # 释放提前发出但携带精确到期时间（引擎 anchor + 7.375）。
    assert abs(early[0].timestamp - (anchor + 7.375)) < 1e-6
    released = planner.update([], anchor + 7.42)
    assert released == []


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
    early = [a for a in before if a.kind == ActionKind.UP]
    assert len(early) == 1
    assert early[0].reason == "chart-tail"
    assert abs(early[0].timestamp - (anchor + 7.375)) < 1e-6
    at_tail = planner.update([], anchor + 7.42)
    assert at_tail == []


def test_chart_slide_tail_release_emits_flick_without_visual_marker():
    chart = ChartTimeline([
        ChartJudgement(2.0, 0, "hold-head", 0, tail_flick=True),
        ChartJudgement(2.5, 3, "hold-tail", 0, tail_flick=True),
    ], bpm=192.0)
    planner, predictor = _planner_with_calibrated_predictor(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    # Engine anchor + 5.0 -> song 2.0: visible slide head on lane 0.
    head_engine = anchor + 5.0
    started = planner.update([
        ObservedNote(
            NoteKind.HOLD, 0, 190, 470, 100, 200, head_engine - 0.05,
        )
    ], head_engine)
    assert [a.kind for a in started] == [ActionKind.DOWN]

    # Finger follows the slide body onto the tail lane before the tail time.
    planner.update([
        ObservedNote(
            NoteKind.HOLD, 3, 640, 480, 100, 190, anchor + 5.20,
        )
    ], anchor + 5.20)

    # Even without any pink tail-ring marker, the fixed chart knows the tail
    # is a slide flick and must emit FLICK instead of a plain UP.
    at_tail = planner.update([], anchor + 5.53)
    releases = [a for a in at_tail if a.reason == "chart-tail"]
    assert len(releases) == 1
    assert releases[0].kind == ActionKind.FLICK
    assert releases[0].lane == 3


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


def test_chart_press_uses_positive_timing_offset_as_earlier_input():
    chart = ChartTimeline([
        ChartJudgement(2.0, 1, "tap", 0),
    ], bpm=120.0)
    planner = RealtimePlanner(
        judgement_y=565,
        timing_offset_ms=20,
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
    predictor._anchor_time = 100.0

    # Positive visual timing offsets move the trigger above the judgement line,
    # i.e. earlier.  Chart ownership must use the same sign convention.
    actions = planner.update([], 104.975)

    assert [action.reason for action in actions] == ["chart-predicted"]


def test_live_timing_adjustment_updates_chart_press_bias():
    chart = ChartTimeline([
        ChartJudgement(2.0, 1, "tap", 0),
    ], bpm=120.0)
    planner, predictor = _planner_with_press_rescue(chart)
    predictor._anchor_time = 100.0

    planner.set_timing_offset_ms(30)
    actions = planner.update([], 104.975)

    assert [action.reason for action in actions] == ["chart-predicted"]


def test_positive_timing_offset_releases_chart_hold_earlier():
    chart = ChartTimeline([
        ChartJudgement(2.0, 1, "hold-head", 0),
        ChartJudgement(2.5, 1, "hold-tail", 0),
    ], bpm=120.0)
    planner = RealtimePlanner(
        judgement_y=565,
        timing_offset_ms=30,
        chart_timeline=chart,
        chart_prediction=True,
        chart_predict_presses=True,
    )
    predictor = planner._chart_predictor
    assert predictor is not None
    predictor.calibrated = True
    predictor.song_offset_s = -3.0
    predictor._anchor_time = 100.0
    planner._state._active_hold_tail[1] = 400
    planner._state._active_hold_lane[1] = 1
    planner._state._hold_started[1] = 105.0
    predictor.expected_hold_tail[1] = (2.5, 1)

    actions = planner.update([], 105.48)

    assert [(action.kind, action.reason) for action in actions] == [
        (ActionKind.UP, "chart-tail"),
    ]


def test_suppressed_visual_rescue_does_not_consume_chart_press():
    """A visual action removed by chart ownership was never dispatched."""
    chart = ChartTimeline([
        ChartJudgement(2.0, 1, "tap", 0),
    ], bpm=120.0)
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    # A first-visible fragment at the line creates a visual rescue in the
    # ordinary pipeline.  The chart owner must remove that rescue *before*
    # deduplication commits it as a dispatched input, then send the real
    # chart-timed press.  The old ordering removed the rescue afterwards and
    # incorrectly consumed the chart judgement without returning any action.
    now = anchor + 5.03
    actions = planner.update([
        ObservedNote(NoteKind.TAP, 1, 340, 560, 20, 10, now),
    ], now)

    assert [
        (action.kind, action.lane, action.reason)
        for action in actions
    ] == [(ActionKind.TAP, 1, "chart-predicted")]


def test_chart_press_preserves_directional_flick():
    chart = ChartTimeline([
        ChartJudgement(
            2.0, 3, "tap", 0, flick=True, direction="Right"
        ),
    ], bpm=120.0)
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    actions = planner.update([], anchor + 5.05)
    predicted = [a for a in actions if a.reason == "chart-predicted"]

    assert len(predicted) == 1
    assert predicted[0].kind == ActionKind.FLICK
    assert predicted[0].flick_direction == "Right"


def test_matching_chart_upgrades_visual_tap_to_directional_flick():
    chart = ChartTimeline([
        ChartJudgement(
            2.0, 3, "tap", 0, flick=True, direction="Left"
        ),
    ], bpm=120.0)
    planner, predictor = _planner_with_press_rescue(chart)
    predictor._anchor_time = 0.0
    predictor.song_offset_s = 0.0
    visual = TouchAction(ActionKind.TAP, 3, 2.0, reason="crossing")

    actions = predictor.apply_chart_flick_semantics([visual], planner._state)

    assert actions[0].kind == ActionKind.FLICK
    assert actions[0].flick_direction == "Left"
    assert planner._state._last_trigger_action_kind[3] == ActionKind.FLICK


def test_matching_chart_downgrades_false_visual_flick_on_ordinary_tap():
    chart = ChartTimeline([
        ChartJudgement(2.0, 3, "tap", 0),
    ], bpm=120.0)
    planner, predictor = _planner_with_press_rescue(chart)
    predictor._anchor_time = 0.0
    predictor.song_offset_s = 0.0
    visual = TouchAction(ActionKind.FLICK, 3, 2.0, reason="crossing")

    actions = predictor.apply_chart_flick_semantics([visual], planner._state)

    assert actions[0].kind == ActionKind.TAP
    assert actions[0].flick_direction is None


def test_prelock_exact_opening_flick_chord_upgrades_visible_taps():
    chart = ChartTimeline([
        ChartJudgement(2.4, 1, "tap", 0, flick=True, direction="Left"),
        ChartJudgement(2.4, 5, "tap", 1, flick=True, direction="Right"),
        ChartJudgement(2.8, 0, "tap", 2),
        ChartJudgement(3.1, 6, "tap", 3),
        ChartJudgement(3.5, 2, "tap", 4),
        ChartJudgement(3.9, 4, "tap", 5),
    ], bpm=120.0)
    planner, predictor = _planner_with_press_rescue(chart)
    predictor._anchor_time = 0.0
    predictor.calibrated = False
    predictor.song_offset_s = 0.0
    predictor._track_phase_candidates = {
        1: [(-2.834, 1, 0, 2.4, 5.234)],
        2: [(-2.834, 5, 1, 2.4, 5.234)],
        3: [(-2.834, 0, 2, 2.8, 5.634)],
        4: [(-2.834, 6, 3, 3.1, 5.934)],
        5: [(-2.834, 2, 4, 3.5, 6.334)],
        6: [(-2.834, 4, 5, 3.9, 6.734)],
    }
    visible = [
        TouchAction(ActionKind.TAP, 1, 5.234, reason="crossing"),
        TouchAction(ActionKind.TAP, 5, 5.234, reason="crossing"),
    ]

    actions = predictor.apply_chart_flick_semantics(visible, planner._state)

    assert not predictor.calibrated
    assert [(action.kind, action.flick_direction) for action in actions] == [
        (ActionKind.FLICK, "Left"),
        (ActionKind.FLICK, "Right"),
    ]


def test_prelock_ordinary_opening_chord_keeps_visible_taps():
    chart = ChartTimeline([
        ChartJudgement(2.4, 1, "tap", 0),
        ChartJudgement(2.4, 5, "tap", 1),
        ChartJudgement(2.8, 0, "tap", 2),
        ChartJudgement(3.1, 6, "tap", 3),
        ChartJudgement(3.5, 2, "tap", 4),
        ChartJudgement(3.9, 4, "tap", 5),
    ], bpm=120.0)
    planner, predictor = _planner_with_press_rescue(chart)
    predictor._anchor_time = 0.0
    predictor.calibrated = False
    predictor.song_offset_s = 0.0
    predictor._track_phase_candidates = {
        index + 1: [(-2.834, judgement.lane, judgement.note_index,
                     judgement.time_s, judgement.time_s + 2.834)]
        for index, judgement in enumerate(chart.judgements)
    }
    visible = [
        TouchAction(ActionKind.TAP, 1, 5.234, reason="crossing"),
        TouchAction(ActionKind.TAP, 5, 5.234, reason="crossing"),
    ]

    actions = predictor.apply_chart_flick_semantics(visible, planner._state)

    assert [action.kind for action in actions] == [ActionKind.TAP, ActionKind.TAP]


def test_prelock_opening_phase_upgrades_split_second_flick_chord():
    """The second opening chord can arrive one lane per detector frame.

    Hyadain Expert exposes two all-FLICK chords before strict phase lock.  The
    first chord establishes a safe semantic-only phase, then lane 1 of the
    second chord arrived as TAP one frame before lane 5 and cost a MISS.
    """
    chart = ChartTimeline([
        ChartJudgement(2.4, 1, "tap", 0, flick=True),
        ChartJudgement(2.4, 5, "tap", 1, flick=True),
        ChartJudgement(2.7, 1, "tap", 2, flick=True),
        ChartJudgement(2.7, 5, "tap", 3, flick=True),
        ChartJudgement(3.0, 2, "tap", 4),
        ChartJudgement(3.3, 6, "tap", 5),
    ], bpm=200.0)
    planner, predictor = _planner_with_press_rescue(chart)
    predictor._anchor_time = 0.0
    predictor.calibrated = False
    predictor.song_offset_s = 0.0
    predictor._track_phase_candidates = {
        1: [(-2.834, 1, 0, 2.4, 5.234)],
        2: [(-2.834, 5, 1, 2.4, 5.234)],
        3: [(-2.834, 1, 2, 2.7, 5.534)],
        4: [(-2.834, 5, 3, 2.7, 5.534)],
        5: [(-2.834, 2, 4, 3.0, 5.834)],
        6: [(-2.834, 6, 5, 3.3, 6.134)],
    }
    opening = predictor.apply_chart_flick_semantics([
        TouchAction(ActionKind.TAP, 1, 5.234, reason="crossing"),
        TouchAction(ActionKind.TAP, 5, 5.234, reason="crossing"),
    ], planner._state)

    split_second = predictor.apply_chart_flick_semantics([
        TouchAction(ActionKind.TAP, 1, 5.534, reason="crossing"),
    ], planner._state)
    ordinary = predictor.apply_chart_flick_semantics([
        TouchAction(ActionKind.TAP, 2, 5.834, reason="crossing"),
    ], planner._state)

    assert [action.kind for action in opening] == [
        ActionKind.FLICK, ActionKind.FLICK,
    ]
    assert split_second[0].kind is ActionKind.FLICK
    assert ordinary[0].kind is ActionKind.TAP


def test_two_confirmed_opening_flick_chords_promote_chart_clock():
    chart = ChartTimeline([
        ChartJudgement(2.4, 1, "tap", 0, flick=True),
        ChartJudgement(2.4, 5, "tap", 1, flick=True),
        ChartJudgement(2.7, 1, "tap", 2, flick=True),
        ChartJudgement(2.7, 5, "tap", 3, flick=True),
        ChartJudgement(3.0, 2, "tap", 4),
        ChartJudgement(3.3, 6, "tap", 5),
    ], bpm=200.0)
    planner, predictor = _planner_with_press_rescue(chart)
    predictor._anchor_time = 0.0
    predictor.calibrated = False
    predictor.song_offset_s = 0.0
    predictor._track_phase_candidates = {
        1: [(-2.834, 1, 0, 2.4, 5.234)],
        2: [(-2.834, 5, 1, 2.4, 5.234)],
        3: [(-2.834, 1, 2, 2.7, 5.534)],
        4: [(-2.834, 5, 3, 2.7, 5.534)],
        5: [(-2.834, 2, 4, 3.0, 5.834)],
        6: [(-2.834, 6, 5, 3.3, 6.134)],
    }

    predictor.apply_chart_flick_semantics([
        TouchAction(ActionKind.FLICK, 1, 5.234, reason="crossing"),
        TouchAction(ActionKind.FLICK, 5, 5.234, reason="crossing"),
    ], planner._state)
    predictor.apply_chart_flick_semantics([
        TouchAction(ActionKind.FLICK, 1, 5.534, reason="crossing"),
    ], planner._state)
    assert not predictor.calibrated

    predictor.apply_chart_flick_semantics([
        TouchAction(ActionKind.FLICK, 5, 5.534, reason="crossing"),
    ], planner._state)

    assert predictor.calibrated
    assert predictor.song_offset_s == -2.834
    assert {
        (0, "tap"), (1, "tap"), (2, "tap"), (3, "tap"),
    }.issubset(predictor._consumed_judgements)


def test_touch_recovery_clears_contacts_without_losing_chart_phase():
    chart = _synthetic_chart()
    planner, predictor = _planner_with_press_rescue(chart)
    predictor._anchor_time = 100.0
    predictor.calibrated = True
    predictor.song_offset_s = -3.0
    predictor._consumed_judgements.add((0, "tap"))
    predictor.expected_hold_tail[3] = (105.0, 5)
    planner._state._active_hold_tail[3] = 105.0
    planner._state._active_hold_lane[3] = 5

    planner.recover_touch_state(104.0)

    assert not planner.has_active_holds
    assert predictor.expected_hold_tail == {}
    assert predictor.calibrated
    assert predictor.song_offset_s == -3.0
    assert predictor._consumed_judgements == {(0, "tap")}


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


def test_chart_press_dispatches_same_lane_notes_118ms_apart():
    """A chart-owned press may cover only its own judgement identity.

    Happy Synthesizer Expert contains repeated same-lane notes 0.118 s apart.
    The old +/-120 ms generic coverage window consumed every affected second
    judgement without dispatching it, producing exactly seven MISS results.
    """
    chart = ChartTimeline([
        ChartJudgement(2.000, 4, "tap", 0),
        ChartJudgement(2.118, 4, "tap", 1),
    ], bpm=120.0)
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    first = planner.update([], anchor + 5.000)
    second = planner.update([], anchor + 5.118)

    assert [action.reason for action in first] == ["chart-predicted"]
    assert [action.reason for action in second] == ["chart-predicted"]
    assert predictor.predicted_presses == 2
    assert predictor._consumed_judgements == {
        (0, "tap"),
        (1, "tap"),
    }


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
    early = [a for a in before_tail if a.kind == ActionKind.UP]
    assert len(early) == 1
    assert early[0].reason == "chart-tail"
    assert abs(early[0].timestamp - (anchor + 7.375)) < 1e-6
    at_tail = planner.update([], anchor + 7.42)
    assert at_tail == []


def test_chart_blind_presses_slide_and_follows_chart_path():
    # Slide: head on lane 5, tail on lane 3.  Without a visible body the
    # chart presses the head, drives a linear lane path, and releases at the
    # tail time on the tail lane.
    chart = ChartTimeline([
        ChartJudgement(2.0, 5, "hold-head", 0),
        ChartJudgement(2.5, 3, "hold-tail", 0),
    ], bpm=192.0)
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    pressed = planner.update([], anchor + 5.07)
    downs = [a for a in pressed if a.kind == ActionKind.DOWN]
    assert len(downs) == 1
    assert downs[0].lane == 5
    assert downs[0].reason == "chart-predicted"

    # Halfway through the slide the finger should be around lane 4.
    mid = planner.update([], anchor + 5.25)
    moves = [a for a in mid if a.reason == "chart-slide-move"]
    assert len(moves) == 1 and moves[0].lane == 4

    # At the tail time the contact is released on the tail lane.
    end = planner.update([], anchor + 5.53)
    releases = [a for a in end if a.reason == "chart-tail"]
    assert len(releases) == 1 and releases[0].lane == 3


def test_chart_blind_slide_follows_each_connection_segment():
    judgements = [
        ChartJudgement(2.0, 0, "hold-head", 0),
        ChartJudgement(2.6, 4, "hold-tail", 0),
    ]
    path = ChartHoldPath(
        note_index=0,
        note_type="Slide",
        points=(
            ChartPathPoint(4.0, 2.0, 0),
            ChartPathPoint(4.4, 2.2, 6),
            ChartPathPoint(4.8, 2.4, 1),
            ChartPathPoint(5.2, 2.6, 4),
        ),
    )
    chart = ChartTimeline(judgements, bpm=120.0, hold_paths=[path])
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    pressed = planner.update([], anchor + 5.05)
    assert [a.kind for a in pressed if a.reason == "chart-predicted"] == [
        ActionKind.DOWN,
    ]

    first_curve = planner.update([], anchor + 5.25)
    moves = [a for a in first_curve if a.reason == "chart-slide-move"]
    assert len(moves) == 1 and moves[0].lane == 5

    second_curve = planner.update([], anchor + 5.45)
    moves = [a for a in second_curve if a.reason == "chart-slide-move"]
    assert len(moves) == 1 and moves[0].lane == 2


def test_adjacent_lane_zigzag_slide_uses_one_stable_midpoint():
    """Adjacent-lane judgement makes exact sawtooth motion unnecessary."""
    lanes = (1, 2, 1, 2, 1, 2, 1, 2, 1)
    path = ChartHoldPath(
        note_index=0,
        note_type="Slide",
        points=tuple(
            ChartPathPoint(4.0 + index * 0.25, 2.0 + index * 0.125, lane)
            for index, lane in enumerate(lanes)
        ),
    )
    chart = ChartTimeline(
        [
            ChartJudgement(2.0, 1, "hold-head", 0),
            ChartJudgement(3.0, 1, "hold-tail", 0),
        ],
        bpm=120.0,
        hold_paths=[path],
    )
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    initial = planner.update([], anchor + 5.05)
    expected_x = round(
        (lane_center_x(1, 565) + lane_center_x(2, 565)) / 2.0
    )
    moves = [
        action for action in initial
        if action.reason == "chart-slide-move"
    ]
    for song_time in (2.12, 2.26, 2.39, 2.51, 2.64, 2.76, 2.89):
        moves.extend(
            action
            for action in planner.update([], anchor + song_time + 3.0)
            if action.reason == "chart-slide-move"
        )

    assert moves
    assert {action.target_x for action in moves} == {expected_x}
    assert len(moves) == 1

    tail_actions = planner.update([], anchor + 6.02)
    assert not [
        action for action in tail_actions
        if action.reason == "chart-tail-move"
    ]
    assert [
        action.kind for action in tail_actions
        if action.reason == "chart-tail"
    ] == [ActionKind.UP]


def test_chart_protects_visual_slide_and_corrects_each_connection_lane():
    """A matched visual slide must not release or wander between chart nodes.

    The Expert representative trace repeatedly lost 18-24 of its 60 counted
    connection points.  Its worst mirrored slides released early or followed
    a segmented green body onto the wrong lane even though the exact chart
    head had already been matched.
    """
    chart = ChartTimeline(
        [
            ChartJudgement(2.0, 6, "hold-head", 0),
            ChartJudgement(3.2, 6, "hold-tail", 0),
        ],
        bpm=192.0,
        hold_paths=[ChartHoldPath(
            note_index=0,
            note_type="Slide",
            points=(
                ChartPathPoint(4.0, 2.0, 6),
                ChartPathPoint(4.8, 2.4, 3),
                ChartPathPoint(5.6, 2.8, 5),
                ChartPathPoint(6.4, 3.2, 6),
            ),
        )],
    )
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    started = planner.update([
        ObservedNote(NoteKind.HOLD, 6, 1090, 500, 180, 150, anchor + 5.0),
    ], now=anchor + 5.0)
    assert [action.kind for action in started] == [ActionKind.DOWN]

    # A false tail ring on the head/tail lane appears a full segment early.
    # The chart identity must keep the contact alive.
    early_ring = planner.update([
        ObservedNote(NoteKind.HOLD, 6, 1090, 570, 100, 20, anchor + 5.30),
    ], now=anchor + 5.30)
    assert not [action for action in early_ring if action.kind == ActionKind.UP]

    # Near the first connection, the fragmented visual body still says lane
    # 6.  The chart correction is appended after visual following and wins.
    first_connection = planner.update([
        ObservedNote(NoteKind.HOLD, 6, 1090, 520, 180, 150, anchor + 5.37),
    ], now=anchor + 5.37)
    corrections = [
        action for action in first_connection
        if action.reason == "chart-slide-move"
    ]
    assert corrections and corrections[-1].lane == 3

    # The same correction is restored on the next frame if vision wanders
    # away again inside the connection judgement window.
    planner._state._active_hold_lane[6] = 6
    planner._state._active_hold_x[6] = 1090
    repeated = planner.update([], now=anchor + 5.41)
    corrections = [
        action for action in repeated if action.reason == "chart-slide-move"
    ]
    assert corrections and corrections[-1].lane == 3

    before_tail = planner.update([
        ObservedNote(NoteKind.HOLD, 6, 1090, 570, 100, 20, anchor + 5.90),
    ], now=anchor + 5.90)
    assert not [action for action in before_tail if action.kind == ActionKind.UP]

    at_tail = planner.update([], now=anchor + 6.19)
    assert [action.lane for action in at_tail if action.reason == "chart-tail"] == [6]


def test_chart_starts_overlapping_slide_on_an_original_or_crossed_lane():
    """A second slide may start on another active slide's lane.

    Expert paths 383/384 and 399/400 overlap this way.  The first finger has
    already moved from its origin; two other short holds begin on a lane that
    a previous slide is physically crossing.  Both cases need another contact
    instead of dropping the new head and its counted connection points.
    """
    chart = ChartTimeline(
        [
            ChartJudgement(2.0, 3, "hold-head", 0),
            ChartJudgement(2.4, 3, "hold-head", 1),
            ChartJudgement(2.6, 1, "hold-tail", 0),
            ChartJudgement(3.0, 5, "hold-tail", 1),
        ],
        bpm=192.0,
        hold_paths=[
            ChartHoldPath(
                0,
                "Slide",
                (
                    ChartPathPoint(4.0, 2.0, 3),
                    ChartPathPoint(4.8, 2.2, 2),
                    ChartPathPoint(5.6, 2.6, 1),
                ),
            ),
            ChartHoldPath(
                1,
                "Slide",
                (
                    ChartPathPoint(4.8, 2.4, 3),
                    ChartPathPoint(5.6, 2.6, 4),
                    ChartPathPoint(6.4, 3.0, 5),
                ),
            ),
        ],
    )
    for occupied_lane in (2, 3):
        planner, predictor = _planner_with_press_rescue(chart)
        anchor = 100.0
        predictor._anchor_time = anchor

        first = planner.update([
            ObservedNote(
                NoteKind.HOLD, 3, 640, 500, 180, 150, anchor + 5.0,
            ),
        ], now=anchor + 5.0)
        assert [
            (action.lane, action.contact)
            for action in first if action.kind == ActionKind.DOWN
        ] == [(3, 3)]

        planner._state._active_hold_lane[3] = occupied_lane
        planner._state._active_hold_x[3] = 500 if occupied_lane == 2 else 640
        second = planner.update([], now=anchor + 5.47)
        second_downs = [
            action for action in second if action.kind == ActionKind.DOWN
        ]

        assert len(second_downs) == 1
        assert second_downs[0].lane == 3
        assert second_downs[0].contact != 3
        assert len(planner._state._active_hold_tail) == 2
        assert not [
            action for action in planner.update([], now=anchor + 5.50)
            if action.kind == ActionKind.DOWN
        ]


def test_simultaneous_cross_lane_flick_tail_does_not_free_new_head_lane():
    """A slide tail on another lane must finish before a same-lane new head.

    ヒバナ-Reloaded- Expert has an old slide that starts on lane 3, flicks on
    lane 6 at 120.3 s, and a new slide head on lane 3 at the same judgement.
    Treating the old contact as a lane-3 obstruction released it in place,
    dropping the lane-6 flick tail instead of allocating another contact.
    """
    chart = ChartTimeline(
        [
            ChartJudgement(2.0, 3, "hold-head", 0, tail_flick=True),
            ChartJudgement(2.3, 3, "hold-head", 1),
            ChartJudgement(2.3, 6, "hold-tail", 0, tail_flick=True),
            ChartJudgement(2.8, 3, "hold-tail", 1),
        ],
        bpm=200.0,
        hold_paths=[
            ChartHoldPath(
                0,
                "Slide",
                (
                    ChartPathPoint(4.0, 2.0, 3),
                    ChartPathPoint(4.6, 2.3, 6, flick=True),
                ),
            ),
            ChartHoldPath(
                1,
                "Long",
                (
                    ChartPathPoint(4.6, 2.3, 3),
                    ChartPathPoint(5.6, 2.8, 3),
                ),
            ),
        ],
    )
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    started = planner.update([
        ObservedNote(NoteKind.HOLD, 3, 640, 500, 180, 150, anchor + 5.0),
    ], now=anchor + 5.0)
    assert [
        (action.lane, action.contact)
        for action in started if action.kind == ActionKind.DOWN
    ] == [(3, 3)]

    overlapping = planner.update([], now=anchor + 5.29)
    assert not [
        action for action in overlapping if action.reason == "chart-lane-free"
    ]
    assert [
        (action.lane, action.contact)
        for action in overlapping if action.kind == ActionKind.DOWN
    ] == [(3, 7)]
    assert [
        (action.kind, action.lane, action.contact)
        for action in overlapping if action.reason == "chart-tail"
    ] == [(ActionKind.FLICK, 6, 3)]


def test_visual_tail_release_then_same_frame_chart_head_reuses_contact_safely():
    """A visual tail may release a contact just before a new chart head.

    The failed song-534 trace released an old slide through ``tail-ring`` and
    reused contact 3 for a new chart slide in the same planner frame.  The
    chart registry still contained the old due tail, so it immediately lifted
    the new contact with a zero-millisecond hold.  The visual UP must retire
    that stale tail before chart prediction allocates the contact again.
    """
    chart = ChartTimeline([
        ChartJudgement(1.0, 3, "hold-head", 0),
        ChartJudgement(2.3, 6, "hold-tail", 0),
        ChartJudgement(2.3, 3, "hold-head", 1),
        ChartJudgement(2.8, 0, "hold-tail", 1),
    ], bpm=200.0)
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor
    predictor.expected_hold_tail[3] = (2.3, 6)

    actions = [TouchAction(
        ActionKind.UP,
        6,
        anchor + 5.3,
        contact=3,
        reason="tail-ring",
    )]
    predictor.update(
        [],
        [],
        anchor + 5.3,
        actions,
        planner._state,
        planner._holds,
    )

    assert [
        (action.kind, action.contact, action.reason) for action in actions
    ] == [
        (ActionKind.UP, 3, "tail-ring"),
        (ActionKind.DOWN, 3, "chart-predicted"),
    ]
    assert predictor.expected_hold_tail[3] == (2.8, 0)
    assert 3 in planner._state._active_hold_tail


def test_chart_suppresses_early_visual_hold_and_presses_exact_head():
    """An outlier visual DOWN must not occupy the exact chart head."""
    chart = ChartTimeline([
        ChartJudgement(2.0, 1, "hold-head", 0),
        ChartJudgement(2.5, 1, "hold-tail", 0),
    ], bpm=192.0)
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    early = planner.update([
        ObservedNote(NoteKind.HOLD, 1, 340, 500, 180, 150, anchor + 4.65),
    ], now=anchor + 4.65)
    assert not [action for action in early if action.kind == ActionKind.DOWN]
    assert not planner.has_active_holds

    # The chart rescue window allows up to 120 ms lateness; querying only
    # 80 ms into the past used to forget short Expert heads before dispatch.
    due = planner.update([], now=anchor + 5.10)
    predicted = [
        action for action in due
        if action.kind == ActionKind.DOWN and action.reason == "chart-predicted"
    ]
    assert len(predicted) == 1
    assert predicted[0].lane == 1
    assert predicted[0].contact == 1


def test_short_chart_hold_is_not_freed_as_its_own_due_head():
    """The lane-free guard must not shorten the currently owned chart hold."""
    chart = ChartTimeline([
        ChartJudgement(2.0, 4, "hold-head", 0),
        ChartJudgement(2.075, 4, "hold-tail", 0),
    ], bpm=200.0)
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    started = planner.update([], now=anchor + 5.0)
    assert [
        (action.kind, action.reason) for action in started
    ] == [(ActionKind.DOWN, "chart-predicted")]

    before_tail = planner.update([], now=anchor + 5.047)
    early = [
        action for action in before_tail if action.kind == ActionKind.UP
    ]
    assert len(early) == 1
    assert early[0].reason == "chart-tail"
    assert abs(early[0].timestamp - (anchor + 5.075)) < 1e-6
    at_tail = planner.update([], now=anchor + 5.08)
    assert at_tail == []


def test_chart_suppresses_visual_hold_182ms_early_and_represses_on_time():
    """A visible body cannot consume a future short hold head too early."""
    chart = ChartTimeline([
        ChartJudgement(2.0, 5, "hold-head", 0),
        ChartJudgement(2.15, 3, "hold-tail", 0),
    ], bpm=200.0)
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    early_now = anchor + 4.818
    early = planner.update([
        ObservedNote(NoteKind.HOLD, 5, 940, 500, 180, 150, early_now),
    ], now=early_now)

    assert not [action for action in early if action.kind == ActionKind.DOWN]
    assert not planner.has_active_holds
    assert any(
        diagnostic["event"] == "chart_early_hold_suppressed"
        for diagnostic in planner.drain_diagnostics()
    )

    due = planner.update([], now=anchor + 5.0)
    assert [
        (action.kind, action.lane, action.reason)
        for action in due if action.kind == ActionKind.DOWN
    ] == [(ActionKind.DOWN, 5, "chart-predicted")]


def test_exact_chart_head_overrides_recent_visual_hold_release():
    """Residue cooldown cannot suppress an unclaimed exact chart head."""
    chart = ChartTimeline([
        ChartJudgement(2.0, 1, "hold-head", 0),
        ChartJudgement(2.2, 1, "hold-tail", 0),
    ], bpm=192.0)
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor
    planner._state._hold_released_at[1] = anchor + 5.04

    actions = planner.update([], now=anchor + 5.07)

    assert [
        (action.kind, action.lane, action.reason)
        for action in actions if action.kind == ActionKind.DOWN
    ] == [(ActionKind.DOWN, 1, "chart-predicted")]


def test_chart_slide_release_suppresses_head_lane_residue_repress():
    """The released chart path owns residue on both its head and tail lanes.

    The failed 最高到達点 Expert trace released simultaneous slides from
    lanes 1/5 onto lanes 0/6, then 94 ms later re-opened visual holds on the
    original head lanes.  Those phantom contacts overlapped the next chart
    taps on the same lanes and preceded the fatal life drop.
    """
    chart = ChartTimeline(
        [
            ChartJudgement(2.0, 1, "hold-head", 0),
            ChartJudgement(2.228, 0, "hold-tail", 0),
            ChartJudgement(2.5, 1, "tap", 1),
        ],
        bpm=132.0,
        hold_paths=[
            ChartHoldPath(
                0,
                "Slide",
                (
                    ChartPathPoint(4.4, 2.0, 1),
                    ChartPathPoint(4.9, 2.228, 0),
                ),
            ),
        ],
    )
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    head = planner.update([], now=anchor + 5.02)
    assert [
        (action.kind, action.lane, action.reason)
        for action in head
    ] == [(ActionKind.DOWN, 1, "chart-predicted")]

    tail = planner.update([], now=anchor + 5.24)
    assert [
        (action.kind, action.lane, action.reason)
        for action in tail
    ] == [
        (ActionKind.MOVE, 0, "chart-slide-move"),
        (ActionKind.UP, 0, "chart-tail"),
    ]

    residue_lead_time = anchor + 5.30
    assert planner.update(
        [
            ObservedNote(
                NoteKind.HOLD,
                1,
                340,
                500,
                180,
                80,
                residue_lead_time,
            ),
        ],
        now=residue_lead_time,
    ) == []

    residue_time = anchor + 5.334
    residue = planner.update(
        [
            ObservedNote(
                NoteKind.HOLD,
                1,
                340,
                530,
                180,
                80,
                residue_time,
            ),
        ],
        now=residue_time,
    )

    assert not [action for action in residue if action.kind == ActionKind.DOWN]


def test_chart_clock_discards_visual_hold_without_matching_chart_head():
    """A tail-ring fragment cannot become a new hold after cooldown expires."""
    chart = ChartTimeline([
        ChartJudgement(2.0, 1, "tap", 0),
        ChartJudgement(2.25, 6, "tap", 1),
    ], bpm=132.0)
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    lead_time = anchor + 5.15
    planner.update([
        ObservedNote(
            NoteKind.HOLD, 0, 190, 500, 180, 80, lead_time,
        ),
    ], now=lead_time)
    actions = planner.update([
        ObservedNote(
            NoteKind.HOLD, 0, 190, 530, 180, 80, anchor + 5.20,
        ),
    ], now=anchor + 5.20)

    assert not [action for action in actions if action.kind == ActionKind.DOWN]
    assert not planner.has_active_holds
    diagnostics = planner.drain_diagnostics()
    assert any(
        diagnostic["event"] == "chart_unmatched_hold_suppressed"
        for diagnostic in diagnostics
    )
    assert not [
        diagnostic for diagnostic in diagnostics
        if diagnostic["event"] == "hold_release"
        and diagnostic.get("release_method") == "chart-unmatched-visual"
    ]


def test_chart_clock_discards_short_hold_tail_residue_near_old_head():
    """A just-finished short hold cannot restart from its tail residue.

    六兆年 Expert ends with several 81 ms holds followed by five lane-3
    taps.  The detector reclassified one finished tail as a new DOWN 200 ms
    after the head; matching only against hold heads let that phantom contact
    occupy lane 3 and silence the first three final chart presses.
    """
    chart = ChartTimeline([
        ChartJudgement(2.0, 2, "hold-head", 0),
        ChartJudgement(2.08, 2, "hold-tail", 0),
        ChartJudgement(2.24, 3, "tap", 1),
    ], bpm=120.0)
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    first = anchor + 5.15
    planner.update([
        ObservedNote(NoteKind.HOLD, 2, 505, 500, 180, 80, first),
    ], now=first)
    residue = anchor + 5.20
    actions = planner.update([
        ObservedNote(NoteKind.HOLD, 2, 505, 530, 180, 80, residue),
    ], now=residue)

    assert not [action for action in actions if action.kind == ActionKind.DOWN]
    assert not planner.has_active_holds
    assert any(
        diagnostic["event"] == "chart_unmatched_hold_suppressed"
        for diagnostic in planner.drain_diagnostics()
    )


def test_visual_matches_correct_small_phase_drift():
    chart = ChartTimeline([
        ChartJudgement(float(index), 0, "tap", index)
        for index in range(1, 6)
    ], bpm=120.0)
    predictor = ChartPredictor(chart)
    predictor.calibrated = True
    predictor._anchor_time = 0.0

    for index in range(1, 5):
        now = float(index) - 0.54
        predictor.observe_tracks([
            _phase_track(index, 0, now),
        ], now)

    assert predictor.song_offset_s == 0.002


def test_result_transition_residue_after_chart_end_is_ignored():
    chart = ChartTimeline([
        ChartJudgement(2.0, 0, "tap", 0),
    ], bpm=120.0)
    predictor = ChartPredictor(chart)
    predictor.calibrated = True
    predictor._anchor_time = 0.0

    predictor.observe_visual_actions([
        TouchAction(ActionKind.TAP, 6, 10.0 + index, reason="crossing")
        for index in range(20)
    ])

    assert not predictor.disabled_for_run
    assert predictor._mismatch_streak == 0


def test_post_chart_visual_hold_artifact_cannot_touch_result_screen():
    """Once the exact chart is over, result UI pixels must never create DOWN."""
    chart = ChartTimeline([
        ChartJudgement(2.0, 1, "tap", 0),
    ], bpm=120.0)
    planner, predictor = _planner_with_press_rescue(chart)
    anchor = 100.0
    predictor._anchor_time = anchor

    # The failed formal run produced DOWN/MOVE/FLICK 4.8 seconds after the
    # chart's final judgement, while the result UI was replacing the field.
    # A green/pink result fragment happened to look like a hold body.
    now = anchor + 3.0 + chart.end_time_s + 4.8
    actions = planner.update([
        ObservedNote(NoteKind.HOLD, 5, 960, 500, 180, 150, now),
    ], now)

    assert actions == []
    assert not planner.has_active_holds


def test_dense_visual_artifact_burst_does_not_disable_matching_chart():
    chart = ChartTimeline([
        ChartJudgement(20.0, 0, "tap", 0),
        ChartJudgement(100.0, 0, "tap", 1),
    ], bpm=120.0)
    predictor = ChartPredictor(chart)
    predictor.calibrated = True
    predictor._anchor_time = 0.0

    predictor.observe_visual_actions([
        TouchAction(ActionKind.TAP, 6, 10.0 + index * .2, reason="crossing")
        for index in range(9)
    ])

    assert not predictor.disabled_for_run
    predictor.observe_visual_actions([
        TouchAction(ActionKind.TAP, 0, 20.0, reason="crossing")
    ])
    assert predictor._mismatch_streak == 0


def test_repeated_mismatch_disables_chart_and_releases_blind_hold():
    chart = ChartTimeline([
        *[
            ChartJudgement(10.0 + index, 6, "tap", index)
            for index in range(8)
        ],
        ChartJudgement(100.0, 0, "hold-tail", 20),
    ], bpm=120.0)
    planner, predictor = _planner_with_press_rescue(chart)
    predictor._anchor_time = 0.0

    predictor.observe_visual_actions([
        # Action timestamps are not phase samples after chart lock.
    ])
    for index in range(8):
        now = 13.1 + index - 0.5
        predictor.observe_tracks([
            _phase_track(index + 1, 6, now),
        ], now)
    assert predictor.disabled_for_run

    state = planner._state
    state._active_hold_tail[0] = 400.0
    state._active_hold_lane[0] = 0
    state._hold_started[0] = 10.0
    state._blind_hold_contacts.add(0)
    predictor.expected_hold_tail[0] = (30.0, 0)
    visual = TouchAction(ActionKind.TAP, 2, 20.0, reason="crossing")

    actions = predictor.update(
        [], [], 20.0, [visual], state, planner._holds, visual_observed=True
    )

    assert actions[0] is visual
    assert [(a.kind, a.reason) for a in actions[1:]] == [
        (ActionKind.UP, "chart-disabled"),
    ]
    assert predictor.expected_hold_tail == {}
    assert state._blind_hold_contacts == set()
    assert state.drain_diagnostics()[-1]["event"] == "chart_disabled_for_run"


def test_early_coherent_multilane_phase_drift_relocks_once():
    """A low-MAD early offset correction is stronger than fail-closed fallback.

    The real エゴロック Expert trace locked at -2747 ms, then eight unique
    chart judgements on five lanes consistently projected 80-142 ms later.
    Falling back to visual-only input killed the run two seconds later.  This
    pattern should move the phase once while retaining chart control.
    """
    predictor = ChartPredictor(ChartTimeline([
        ChartJudgement(10.0 + index, lane, "tap", index)
        for index, lane in enumerate((5, 1, 4, 5, 1, 4, 0, 5))
    ], bpm=120.0))
    predictor.calibrated = True
    predictor.song_offset_s = -2.737671
    predictor._calibrated_at_relative_s = 6.312

    residuals = (
        -.091379, -.110446, -.140265, -.142435,
        -.136443, -.142436, -.104176, -.080575,
    )
    lanes = (5, 1, 4, 5, 1, 4, 0, 5)
    for index, (residual, lane) in enumerate(zip(residuals, lanes)):
        predictor._record_phase_residual(
            residual,
            lane,
            relative_time_s=9.0 + index * .2,
        )

    assert not predictor.disabled_for_run
    assert predictor._phase_relock_count == 1
    assert abs(predictor.song_offset_s - (-2.8611155)) < .001
    assert predictor._mismatch_streak == 0


def test_second_coherent_phase_mismatch_still_disables_chart():
    predictor = ChartPredictor(ChartTimeline([
        ChartJudgement(10.0 + index, lane, "tap", index)
        for index, lane in enumerate((0, 1, 2, 3, 4, 5, 6, 0))
    ], bpm=120.0))
    predictor.calibrated = True
    predictor._calibrated_at_relative_s = 1.0
    predictor._phase_relock_count = 1

    for index, lane in enumerate((0, 1, 2, 3, 4, 5, 6, 0)):
        predictor._record_phase_residual(
            -.1,
            lane,
            relative_time_s=2.0 + index * .1,
        )

    assert predictor.disabled_for_run


def test_mixed_direction_phase_outliers_do_not_disable_matching_chart():
    """Projection noise is not a coherent song-clock drift.

    The fatal ヒバナ Expert trace produced eight >80 ms residuals in the
    directions ``+----+--``.  Counting only their absolute values disabled a
    correctly matched chart five seconds before the life loss began.
    """
    predictor = ChartPredictor(ChartTimeline([
        ChartJudgement(10.0, 0, "tap", 0),
    ], bpm=120.0))
    predictor.calibrated = True

    for residual in (.306, -.135, -.090, -.092, -.137, .142, -.095, -.127):
        predictor._record_phase_residual(residual, lane=0)

    assert not predictor.disabled_for_run
    assert predictor._mismatch_streak == 2


def test_duplicate_visual_fragments_count_once_per_chart_judgement():
    chart = ChartTimeline([
        ChartJudgement(10.0 + index, lane, "tap", index)
        for index, lane in enumerate((6, 0, 2, 4))
    ], bpm=120.0)
    predictor = ChartPredictor(chart)
    predictor.calibrated = True
    predictor._anchor_time = 0.0

    track_id = 1
    for judgement in chart.judgements:
        # Two independently tracked detector fragments project to the same
        # physical chart note.  They are one phase observation, not two
        # consecutive song/chart mismatches.
        now = judgement.time_s - 0.5 - 0.1
        for _ in range(2):
            predictor.observe_tracks([
                _phase_track(track_id, judgement.lane, now),
            ], now)
            track_id += 1

    assert predictor._mismatch_streak == 4
    assert not predictor.disabled_for_run


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
