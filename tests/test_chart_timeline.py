from __future__ import annotations

from pathlib import Path

from agent.realtime.chart_timeline import ChartTimeline


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHART_PATH = PROJECT_ROOT / "resource" / "charts" / "song-306-hard.json"


def test_bundled_song_306_hard_chart_parses():
    chart = ChartTimeline.from_json(CHART_PATH)
    assert chart.bpm == 192.0
    # 314 taps + 25 Longs + 17 Slides, each hold contributing head+tail.
    assert len(chart.judgements) == 398
    assert len({item.note_index for item in chart.judgements}) == 356


def test_lane_indexing_and_next_judgement():
    chart = ChartTimeline.from_json(CHART_PATH)
    first_lane0 = chart.next_judgement(0, 0.0)
    assert first_lane0 is not None
    assert first_lane0.lane == 0
    assert first_lane0.time_s == 2.8125
    # The first lane-0 note is a tap at beat 9 (192 BPM -> 2.8125 s).
    assert first_lane0.kind == "tap"

    later = chart.next_judgement(0, 2.9)
    assert later is not None and later.time_s >= 2.9


def test_hold_tail_pairing_and_slide_tail_lane():
    chart = ChartTimeline.from_json(CHART_PATH)
    # Straight lane-0 Long at 14.21875 -> 14.53125.
    head = chart.judgement_near(0, 14.22, window_s=0.05)
    assert head is not None and head.kind == "hold-head"
    tail = chart.hold_tail_for_head(head)
    assert tail is not None
    assert tail.kind == "hold-tail"
    assert tail.time_s == 14.53125
    assert tail.lane == 0

    # The 11.25 s slide starts on lane 0 and ends on lane 4; its tail must
    # be looked up on the tail lane, not the head lane.
    slide_head = chart.judgement_near(0, 11.25, window_s=0.02)
    assert slide_head is not None and slide_head.kind == "hold-head"
    slide_tail = chart.hold_tail_for_head(slide_head)
    assert slide_tail is not None
    assert slide_tail.lane == 4
    assert slide_tail.time_s == 11.5625
