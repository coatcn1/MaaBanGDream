from __future__ import annotations

import json
from pathlib import Path

from agent.realtime.chart_timeline import ChartTimeline


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHART_PATH = (
    PROJECT_ROOT / "resource" / "charts" / "bestdori" / "306" / "hard.json"
)


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
    assert not tail.tail_flick

    # The 11.25 s slide starts on lane 0 and ends on lane 4; its tail must
    # be looked up on the tail lane, not the head lane. Bestdori's Slide
    # type describes a path; this tail has no explicit flick flag.
    slide_head = chart.judgement_near(0, 11.25, window_s=0.02)
    assert slide_head is not None and slide_head.kind == "hold-head"
    slide_tail = chart.hold_tail_for_head(slide_head)
    assert slide_tail is not None
    assert slide_tail.lane == 4
    assert slide_tail.time_s == 11.5625
    assert not slide_tail.tail_flick


def test_piecewise_bpm_map_accumulates_time_across_tempo_changes(tmp_path):
    path = tmp_path / "multi-bpm.json"
    path.write_text(json.dumps([
        {"type": "BPM", "beat": 0, "bpm": 120},
        {"type": "Single", "beat": 2, "lane": 0},
        {"type": "BPM", "beat": 4, "bpm": 240},
        {"type": "Single", "beat": 6, "lane": 1},
    ]), encoding="utf-8")

    chart = ChartTimeline.from_json(path)

    assert chart.next_judgement(0, 0).time_s == 1.0
    # Four beats at 120 BPM (2 s), then two beats at 240 BPM (0.5 s).
    assert chart.next_judgement(1, 0).time_s == 2.5
    assert [change.time_s for change in chart.tempo_changes] == [0.0, 2.0]


def test_complete_slide_path_and_explicit_tail_flick_are_preserved(tmp_path):
    path = tmp_path / "path.json"
    path.write_text(json.dumps([
        {"type": "BPM", "beat": 0, "bpm": 120},
        {"type": "Slide", "connections": [
            {"beat": 1, "lane": 0},
            {"beat": 2, "lane": 3, "hidden": True},
            {"beat": 3, "lane": 1},
            {"beat": 4, "lane": 6, "flick": True, "direction": "Right"},
        ]},
    ]), encoding="utf-8")

    chart = ChartTimeline.from_json(path)
    head = chart.next_judgement(0, 0)
    hold_path = chart.hold_path_for_head(head)
    tail = chart.hold_tail_for_head(head)

    assert [point.lane for point in hold_path.points] == [0, 3, 1, 6]
    assert [point.time_s for point in hold_path.points] == [0.5, 1.0, 1.5, 2.0]
    assert hold_path.points[1].hidden
    assert tail.tail_flick
    assert hold_path.tail.direction == "Right"


def test_schema_v1_wrapper_metadata_is_loaded(tmp_path):
    path = tmp_path / "wrapped.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "source": {"provider": "bestdori"},
        "song": {"bestdori_id": 99},
        "difficulty": {"name": "hard"},
        "chart": [
            {"type": "BPM", "beat": 0, "bpm": 120},
            {"type": "Single", "beat": 1, "lane": 2, "flick": True},
        ],
    }), encoding="utf-8")

    chart = ChartTimeline.from_json(path)

    assert chart.metadata["song"]["bestdori_id"] == 99
    judgement = chart.next_judgement(2, 0)
    assert judgement.flick


def test_directional_note_preserves_horizontal_flick_semantics(tmp_path):
    path = tmp_path / "directional.json"
    path.write_text(json.dumps([
        {"type": "BPM", "beat": 0, "bpm": 120},
        {"type": "Directional", "beat": 2, "lane": 4, "direction": "Left"},
    ]), encoding="utf-8")

    chart = ChartTimeline.from_json(path)
    judgement = chart.next_judgement(4, 0)

    assert judgement.kind == "tap"
    assert judgement.flick
    assert judgement.direction == "Left"


def test_one_point_slide_is_repaired_to_single_judgement(tmp_path):
    path = tmp_path / "one-point-slide.json"
    path.write_text(json.dumps([
        {"type": "BPM", "beat": 0, "bpm": 120},
        {"type": "Slide", "connections": [
            {"beat": 2, "lane": 3, "flick": True, "direction": "Right"},
        ]},
    ]), encoding="utf-8")

    chart = ChartTimeline.from_json(path)

    assert len(chart.judgements) == 1
    assert chart.hold_paths == ()
    judgement = chart.next_judgement(3, 0)
    assert judgement.kind == "tap"
    assert judgement.flick
    assert judgement.direction == "Right"
