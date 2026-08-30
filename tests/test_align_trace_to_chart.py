from __future__ import annotations

import json

import pytest

from scripts.align_trace_to_chart import (
    load_chart_judgements,
    load_trace_actions,
    match_actions_to_judgements,
)


CHART = [
    {"type": "BPM", "bpm": 192, "beat": 0},
    {"type": "Single", "lane": 0, "beat": 6.5},
    {"type": "Single", "lane": 2, "beat": 7.0},
    {"type": "Long", "connections": [
        {"lane": 1, "beat": 9.0},
        {"lane": 1, "beat": 10.0},
    ]},
    {"type": "Slide", "connections": [
        {"lane": 5, "beat": 12.0},
        {"lane": 4, "beat": 13.0},
    ]},
]


def _trace(rows: list[dict]) -> str:
    return "\n".join(json.dumps(row) for row in rows)


def test_chart_judgements_and_trace_alignment(tmp_path):
    chart_path = tmp_path / "chart.json"
    chart_path.write_text(json.dumps(CHART), encoding="utf-8")
    judgements = load_chart_judgements(chart_path)
    assert len(judgements) == 6  # 2 taps + 2 hold-head/tail + 2 slide-head/tail

    # Engine actions happen 2.0 s after the chart beat grid: taps at beat
    # 6.5/7.0 (2.03 s / 2.19 s) => engine 4.03 s / 4.19 s; a down for the
    # hold head at beat 9 (2.81 s) => engine 4.81 s; an up for the slide
    # tail at beat 13 (4.06 s) => engine 6.06 s.
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(_trace([
        {
            "elapsed_ms": 4030.0,
            "timestamp": 1000.0,
            "notes": [],
            "actions": [{"kind": "tap", "lane": 0, "timestamp": 1000.0}],
        },
        {
            "elapsed_ms": 4190.0,
            "timestamp": 1001.0,
            "notes": [],
            "actions": [{"kind": "tap", "lane": 2, "timestamp": 1001.0}],
        },
        {
            "elapsed_ms": 4810.0,
            "timestamp": 1002.0,
            "notes": [],
            "actions": [{
                "kind": "down",
                "lane": 1,
                "timestamp": 1002.0,
                "contact": 1,
            }],
        },
        {
            "elapsed_ms": 6060.0,
            "timestamp": 1003.0,
            "notes": [],
            "actions": [{
                "kind": "up",
                "lane": 4,
                "timestamp": 1003.0,
                "contact": 1,
            }],
        },
    ]), encoding="utf-8")

    actions = load_trace_actions(trace_path)
    # A fixed engine-to-song offset of +2.0 s puts every action on its chart
    # judgement (chart time = engine elapsed - 2.0).
    report = match_actions_to_judgements(actions, judgements, offset_s=-2.0)

    assert report["matched"] == 4
    assert report["missed"] == 2  # the hold tail and the slide head were
    # intentionally not pressed in this trace.
    assert report["spurious"] == 0


def test_schema_v1_chart_uses_its_bpm_map(tmp_path):
    chart_path = tmp_path / "chart.json"
    chart_path.write_text(json.dumps({
        "schema_version": 1,
        "source": {"provider": "bestdori"},
        "song": {"bestdori_id": 125},
        "difficulty": {"name": "hard"},
        "chart": [
            {"type": "BPM", "bpm": 153, "beat": 0},
            {"type": "Single", "lane": 2, "beat": 8},
        ],
    }), encoding="utf-8")

    judgements = load_chart_judgements(chart_path)

    assert judgements == [{
        "time_s": pytest.approx(8 * 60 / 153),
        "lane": 2,
        "type": "tap",
    }]


def test_estimate_offset_recovers_engine_to_song_shift(tmp_path):
    from scripts.align_trace_to_chart import estimate_offset

    chart_path = tmp_path / "chart.json"
    chart_path.write_text(json.dumps(CHART), encoding="utf-8")
    judgements = load_chart_judgements(chart_path)
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(_trace([
        {
            "elapsed_ms": 4030.0,
            "timestamp": 1000.0,
            "notes": [],
            "actions": [{"kind": "tap", "lane": 0, "timestamp": 1000.0}],
        },
        {
            "elapsed_ms": 4190.0,
            "timestamp": 1001.0,
            "notes": [],
            "actions": [{"kind": "tap", "lane": 2, "timestamp": 1001.0}],
        },
        {
            "elapsed_ms": 4810.0,
            "timestamp": 1002.0,
            "notes": [],
            "actions": [{
                "kind": "down",
                "lane": 1,
                "timestamp": 1002.0,
                "contact": 1,
            }],
        },
        {
            "elapsed_ms": 6060.0,
            "timestamp": 1003.0,
            "notes": [],
            "actions": [{
                "kind": "up",
                "lane": 4,
                "timestamp": 1003.0,
                "contact": 1,
            }],
        },
    ]), encoding="utf-8")
    actions = load_trace_actions(trace_path)
    offset = estimate_offset(actions, judgements)
    assert abs(offset - (-2.0)) <= 0.05
