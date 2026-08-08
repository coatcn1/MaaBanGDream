from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.replay_realtime_trace import (
    replay,
    trace_replay_metadata,
    transformed_trace_frames,
)


ROOT = Path(__file__).resolve().parents[1]


def _local_recording(name: str) -> Path:
    trace = ROOT / "debug" / "recordings" / name / "trace.jsonl"
    if not trace.is_file():
        pytest.skip(f"local ignored replay fixture is unavailable: {name}")
    return trace


def _recorded_actions(path: Path) -> list[dict[str, object]]:
    actions = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                actions.extend(json.loads(line).get("actions", []))
    return actions


def _write_trace(path, timestamps: list[float]) -> None:
    rows = [
        {
            "timestamp": timestamp,
            "notes": [
                {
                    "kind": "tap",
                    "lane": 3,
                    "x": 640,
                    "y": 400 + index,
                    "width": 20,
                    "height": 10,
                    "timestamp": timestamp,
                }
            ],
            "actions": [],
        }
        for index, timestamp in enumerate(timestamps)
    ]
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_trace_fault_injection_drops_frames_and_shifts_remaining_clock(tmp_path):
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, [10.0, 10.1, 10.2, 10.3])

    frames = list(transformed_trace_frames(
        trace,
        fault_after_frame=0,
        drop_frames=1,
        inject_gap_ms=350,
    ))

    assert [frame["timestamp"] for frame in frames] == pytest.approx(
        [10.0, 10.55, 10.65]
    )
    assert [frame["notes"][0]["timestamp"] for frame in frames] == pytest.approx(
        [10.0, 10.55, 10.65]
    )
    assert [frame["notes"][0]["y"] for frame in frames] == [400, 402, 403]


@pytest.mark.parametrize(
    ("inject_gap_ms", "drop_frames"),
    [(-1, 0), (0, -1)],
)
def test_trace_fault_injection_rejects_negative_values(
    tmp_path, inject_gap_ms, drop_frames,
):
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, [1.0, 2.0])

    with pytest.raises(ValueError):
        list(transformed_trace_frames(
            trace,
            inject_gap_ms=inject_gap_ms,
            drop_frames=drop_frames,
        ))


def test_replay_reports_residual_hold_cleanup_state(tmp_path):
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, [1.0, 1.1, 1.2])

    result = replay(trace, inject_gap_ms=500, fault_after_frame=0)

    assert result["active_holds_after_replay"] is False
    assert result["cleanup_actions"] == 0
    assert result["cleanup_up_actions"] == 0


def test_replay_applies_recorded_timing_feedback_per_frame(tmp_path, monkeypatch):
    trace = tmp_path / "trace.jsonl"
    rows = []
    for timestamp, offset in ((1.0, -12), (1.1, -13), (1.2, -13)):
        rows.append({
            "timestamp": timestamp,
            "notes": [],
            "actions": [],
            "timing_feedback": {"current_offset_ms": offset},
        })
    trace.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    applied = []

    from scripts import replay_realtime_trace as replay_module

    original = replay_module.RealtimePlanner.set_timing_offset_ms

    def record_offset(self, value):
        applied.append(value)
        return original(self, value)

    monkeypatch.setattr(
        replay_module.RealtimePlanner,
        "set_timing_offset_ms",
        record_offset,
    )

    result = replay(trace, timing_offset_ms=-12)

    assert applied == [-13]
    assert result["replay_timing"] == {
        "initial_offset_ms": -12,
        "final_offset_ms": -13,
        "recorded_feedback_enabled": True,
        "recorded_adjustments": 1,
    }


def test_replay_can_keep_timing_offset_fixed(tmp_path, monkeypatch):
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({
        "timestamp": 1.0,
        "notes": [],
        "actions": [],
        "timing_feedback": {"current_offset_ms": -13},
    }) + "\n", encoding="utf-8")
    applied = []

    from scripts import replay_realtime_trace as replay_module

    monkeypatch.setattr(
        replay_module.RealtimePlanner,
        "set_timing_offset_ms",
        lambda self, value: applied.append(value),
    )

    result = replay(
        trace,
        timing_offset_ms=-12,
        use_recorded_timing_feedback=False,
    )

    assert applied == []
    assert result["replay_timing"]["final_offset_ms"] == -12
    assert result["replay_timing"]["recorded_feedback_enabled"] is False


def test_replay_metadata_uses_adjacent_modern_summary(tmp_path):
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, [1.0, 1.1])
    (tmp_path / "summary.json").write_text(json.dumps({
        "schema_version": 1,
        "session": {"difficulty": "Normal"},
        "timing_feedback": {"initial_offset_ms": -12},
    }), encoding="utf-8")

    assert trace_replay_metadata(trace) == {
        "difficulty": "Normal",
        "timing_offset_ms": -12,
    }


def test_replay_metadata_keeps_legacy_trace_explicit(tmp_path):
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, [1.0, 1.1])

    assert trace_replay_metadata(trace) == {}


def test_local_000656_replay_is_an_exact_277_action_golden():
    trace = _local_recording("realtime-20260808-000656")

    result = replay(
        trace,
        difficulty="Normal",
        timing_offset_ms=-12,
        collect=True,
    )

    assert result["recorded_actions"] == 277
    assert result["replayed_actions"] == 277
    assert result["actions_sequence"] == _recorded_actions(trace)
    assert result["active_holds_after_replay"] is False
    assert result["cleanup_actions"] == 0


@pytest.mark.parametrize("gap_ms", [150, 350, 500, 1000])
def test_local_000656_gap_matrix_is_safe_and_deterministic(gap_ms):
    trace = _local_recording("realtime-20260808-000656")

    first = replay(
        trace,
        difficulty="Normal",
        timing_offset_ms=-12,
        collect=True,
        inject_gap_ms=gap_ms,
    )
    second = replay(
        trace,
        difficulty="Normal",
        timing_offset_ms=-12,
        collect=True,
        inject_gap_ms=gap_ms,
    )

    assert first["actions_sequence"] == second["actions_sequence"]
    assert first["recorded_actions"] == first["replayed_actions"] == 277
    assert first["recorded_structural_actions"] == first[
        "replayed_structural_actions"
    ]
    assert first["recorded_transient_actions"] == first[
        "replayed_transient_actions"
    ]
    assert first["recorded_duplicate_judgements"] == first[
        "replayed_duplicate_judgements"
    ]
    assert first["recorded_post_release_rescues"] == 0
    assert first["replayed_post_release_rescues"] == 0
    assert first["replayed_releases_under_300_ms"] == 0
    assert first["active_holds_after_replay"] is False
    assert first["cleanup_actions"] == 0


def test_local_235842_real_359_ms_gap_replays_without_residual_contacts():
    trace = _local_recording("realtime-20260807-235842")
    timestamps = [
        float(json.loads(line)["timestamp"])
        for line in trace.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    result = replay(trace, difficulty="Normal", timing_offset_ms=0)

    assert max(b - a for a, b in zip(timestamps, timestamps[1:])) == pytest.approx(
        0.359,
        abs=0.001,
    )
    assert result["recorded_actions"] == result["replayed_actions"] == 260
    assert result["recorded_structural_actions"] == result[
        "replayed_structural_actions"
    ]
    assert result["recorded_transient_actions"] == result[
        "replayed_transient_actions"
    ]
    assert result["active_holds_after_replay"] is False
    assert result["cleanup_actions"] == 0
