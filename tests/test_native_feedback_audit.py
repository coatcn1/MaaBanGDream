import json
from types import SimpleNamespace

import pytest

from agent.realtime.engine import EngineStats
from agent.realtime.native_feedback_audit import save_native_feedback_audit
from test_realtime_profile_play_action import _completed_play_harness


def native_report():
    return dict(planned=637, sent=637, executed=637, underflows=0,
        executed_observation_complete=True, state="finished", session_state="finished",
        release_confirmed=True, stop_latency_ms=120., frozen_timing_offset_ms=63,
        life_feedback=dict(enabled=True, summary={"requests": 1, "accepted_requests": 1, "applied_requests": 0},
            ignored_reasons={"no-blank-baseline": 4}, phase_status={"state": "expired", "terminal_reason": "expired"},
            tickets={"summary": {"ticket-created": 4}}, sniff={"records": []}))


@pytest.mark.parametrize("skip_result", [False, True])
def test_native_audit_survives_disabled_debug_and_skipped_results(tmp_path, monkeypatch, skip_result):
    _completed_play_harness(monkeypatch, tmp_path, debug_recording=False,
        diagnostic_trace=False, skip_result_check=skip_result, native_report=native_report())
    reports = list((tmp_path / "debug/native-feedback").glob("*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["frozen_profile_offset_ms"] == 63
    assert payload["life_feedback"]["summary"]["accepted_requests"] == 1
    assert payload["song_id"] and payload["effective_difficulty"] == "Easy"
    assert "serial" not in reports[0].read_text(encoding="utf-8")


def test_native_audit_failure_does_not_change_completed_run(tmp_path, monkeypatch, capsys):
    def fail(*args):
        raise OSError("disk unavailable")
    monkeypatch.setattr("agent.realtime.native_feedback_audit.write_json_atomic", fail)
    _completed_play_harness(monkeypatch, tmp_path, debug_recording=False,
        diagnostic_trace=False, skip_result_check=True, native_report=native_report())
    assert "audit_warning=OSError" in capsys.readouterr().out


def test_native_audit_skips_manual_stop_before_file_io(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.realtime.native_feedback_audit.write_json_atomic", lambda *args: pytest.fail("stopped"))
    stats = EngineStats(1, 1, False, completed=True, native_report=native_report())
    assert save_native_feedback_audit(tmp_path, stats, SimpleNamespace(), 63, stopping=True) is None
    assert not (tmp_path / "debug").exists()
