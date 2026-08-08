from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np

from agent.realtime import profile_play_action
from agent.realtime.engine import EngineStats
from agent.realtime.live_session import LiveRunContext
from agent.realtime.profile_play_action import (
    _result_report_payload,
    resolve_profile,
    resolve_profile_for_settings_gate,
)
from agent.realtime.result_parser import LiveResult


def _run_context(*, recording_path: str | None = None) -> LiveRunContext:
    return LiveRunContext(
        run_id="91cb1867-5e7f-435c-8ccd-cf1a1b378005",
        started_at=datetime(2026, 8, 9, 1, 2, 3, tzinfo=timezone.utc),
        mode="formal",
        difficulty="Expert",
        profile_name="expert-20260809.json",
        song_id="song-phash-v1-0123456789abcdef",
        song_id_method="song-phash-v1",
        expected_note_speed=5.0,
        actual_note_speed=5.0,
        note_skin_type=4,
        tap_effect=2,
        judgement_assist=False,
        debug_recording=recording_path is not None,
        recording_path=recording_path,
    )


def _stats() -> EngineStats:
    return EngineStats(
        120,
        42,
        False,
        completed=True,
        stage_timings_ms={
            "capture": {"p50": 4.1, "p95": 6.3, "max": 469.0},
        },
        frame_interval_outliers=(
            {
                "frame": 71,
                "elapsed_ms": 2234.0,
                "interval_ms": 485.0,
                "dominant_stage": "capture",
                "notes": 2,
                "actions": 1,
                "active_contacts": 0,
            },
        ),
    )


def test_stable_result_payload_links_run_settings_recording_and_stage_metrics():
    payload = _result_report_payload(
        LiveResult(100, 10, 2, 1, 2, 3, 4),
        _stats(),
        timing_offset_ms=-11,
        suggested_timing_offset_ms=-14,
        run_context=_run_context(
            recording_path="debug/recordings/realtime-20260809-010203"
        ),
        result_status="stable",
    )

    assert payload["schema_version"] == 1
    assert payload["valid"] is True
    assert payload["result_status"] == "stable"
    assert payload["run_id"] == "91cb1867-5e7f-435c-8ccd-cf1a1b378005"
    assert payload["song_id"] == "song-phash-v1-0123456789abcdef"
    assert payload["profile"] == "expert-20260809.json"
    assert payload["session"]["run_id"] == payload["run_id"]
    assert payload["session"]["song_id"] == payload["song_id"]
    assert payload["settings"] == {
        "expected_note_speed": 5.0,
        "actual_note_speed": 5.0,
        "note_skin_type": 4,
        "tap_effect": 2,
        "judgement_assist": False,
    }
    assert payload["debug_recording_path"].startswith("debug/recordings/")
    assert payload["stage_timings_ms"]["capture"]["max"] == 469.0
    assert payload["frame_interval_outliers"][0]["dominant_stage"] == "capture"
    assert payload["cleanup_failed"] is False
    assert payload["recorder_error"] is None


def test_timeout_result_payload_is_structured_without_fabricated_judgements():
    payload = _result_report_payload(
        None,
        _stats(),
        timing_offset_ms=-11,
        suggested_timing_offset_ms=None,
        run_context=_run_context(),
        result_status="timed_out",
        reason="result digits did not stabilise in 60 seconds",
    )

    assert payload["valid"] is False
    assert payload["result_status"] == "timed_out"
    assert payload["reason"] == "result digits did not stabilise in 60 seconds"
    assert payload["debug_recording_path"] is None
    assert payload["eligible_for_profile_acceptance"] is False
    assert "perfect" not in payload
    assert payload["run_id"] == "91cb1867-5e7f-435c-8ccd-cf1a1b378005"


def test_calibration_result_with_unknown_song_is_invalid():
    context = replace(
        _run_context(),
        mode="calibration-rehearsal",
        song_id="unknown",
        song_id_method="unknown",
    )
    payload = _result_report_payload(
        LiveResult(100, 10, 2, 1, 2, 3, 4),
        _stats(),
        timing_offset_ms=-11,
        suggested_timing_offset_ms=-14,
        run_context=context,
        result_status="stable",
    )

    assert payload["valid"] is False
    assert payload["result_status"] == "unknown_song"
    assert payload["eligible_for_profile_acceptance"] is False
    assert payload["song_id"] == "unknown"


def test_formal_result_with_unknown_song_remains_valid():
    context = replace(
        _run_context(),
        mode="formal",
        song_id="unknown",
        song_id_method="unknown",
    )

    payload = _result_report_payload(
        LiveResult(100, 10, 2, 1, 2, 3, 4),
        _stats(),
        timing_offset_ms=-11,
        suggested_timing_offset_ms=-14,
        run_context=context,
        result_status="stable",
    )

    assert payload["valid"] is True
    assert payload["result_status"] == "stable"
    assert payload["eligible_for_profile_acceptance"] is True


def test_visual_evaluation_result_is_valid_but_never_profile_eligible():
    context = _run_context()
    context = replace(context, mode="visual-evaluation")
    payload = _result_report_payload(
        LiveResult(100, 10, 2, 1, 2, 3, 4),
        _stats(),
        timing_offset_ms=-11,
        suggested_timing_offset_ms=-14,
        run_context=context,
        result_status="experimental",
    )

    assert payload["valid"] is True
    assert payload["result_status"] == "experimental"
    assert payload["eligible_for_profile_acceptance"] is False


def test_profile_resolution_uses_visual_evaluation_only_when_explicit(monkeypatch):
    controller = SimpleNamespace(
        post_screencap=lambda: SimpleNamespace(
            wait=lambda: SimpleNamespace(
                get=lambda: np.zeros((720, 1280, 3), dtype=np.uint8)
            )
        )
    )
    context = SimpleNamespace(tasker=SimpleNamespace(controller=controller))
    speed = SimpleNamespace(
        actual_note_speed=5.0,
        expected_note_speed=5.0,
        profile="expert.json",
    )
    visual = SimpleNamespace(
        note_skin_type=7,
        tap_effect=4,
        judgement_assist_effect=False,
    )
    monkeypatch.setattr(profile_play_action, "verified_settings", lambda _d: speed)
    monkeypatch.setattr(
        profile_play_action, "verified_game_visual_settings", lambda: visual,
    )
    resolved = SimpleNamespace(profile_path=SimpleNamespace(name="expert.json"))
    calls = []
    monkeypatch.setattr(
        profile_play_action.RealtimeProfileStore,
        "resolve_for_visual_evaluation",
        lambda self, value, **kwargs: calls.append((value, kwargs)) or resolved,
    )
    monkeypatch.setattr(
        profile_play_action.RealtimeProfileStore,
        "resolve",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("strict resolver must not be used")
        ),
    )

    actual = resolve_profile(context, {
        "difficulty": "Expert",
        "settings_gate_required": True,
        "visual_evaluation": True,
        "dpi": 240,
        "game_fps": 60,
        "render_quality": "standard",
    })

    assert actual is resolved
    assert calls[0][0] == "expert.json"
    signature = calls[0][1]["current_signature"]
    assert signature.note_skin_type == 7
    assert signature.tap_effect == 4
    assert signature.judgement_assist_effect is False


def test_visual_evaluation_precheck_ignores_only_speed_and_visuals(monkeypatch):
    controller = SimpleNamespace(
        post_screencap=lambda: SimpleNamespace(
            wait=lambda: SimpleNamespace(
                get=lambda: np.zeros((720, 1280, 3), dtype=np.uint8)
            )
        )
    )
    context = SimpleNamespace(tasker=SimpleNamespace(controller=controller))
    visual = SimpleNamespace(
        note_skin_type=7,
        tap_effect=5,
        judgement_assist_effect=False,
    )
    monkeypatch.setattr(
        profile_play_action, "verified_game_visual_settings", lambda: visual,
    )
    resolved = SimpleNamespace(profile_path=SimpleNamespace(name="expert.json"))
    calls = []
    monkeypatch.setattr(
        profile_play_action.RealtimeProfileStore,
        "resolve_latest_for_visual_evaluation_environment",
        lambda self, **kwargs: calls.append(kwargs) or resolved,
    )
    monkeypatch.setattr(
        profile_play_action.RealtimeProfileStore,
        "resolve_latest_for_environment",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("strict visual precheck must not be used")
        ),
    )

    actual = resolve_profile_for_settings_gate(
        context,
        {
            "difficulty": "Expert",
            "visual_evaluation": True,
            "dpi": 240,
            "game_fps": 60,
            "render_quality": "standard",
        },
        require_verified_visual=True,
    )

    assert actual is resolved
    signature = calls[0]["current_signature"]
    assert signature.note_speed == 1.0
    assert signature.note_skin_type == 7
