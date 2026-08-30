from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .engine import EngineStats
from .live_session import (
    LiveRunContext,
    current_live_run,
    update_live_run,
)
from .result_parser import LiveResult


@dataclass(frozen=True, slots=True)
class PreflightPerformanceSnapshot:
    expected_note_speed: float
    profile: str | None
    actual_note_speed: float | None = None


def _preflight_mode(params: dict, current_mode: str) -> str:
    explicit = params.get("run_mode")
    if explicit:
        return str(explicit)
    if bool(params.get("visual_evaluation", False)):
        return "visual-evaluation"
    if str(current_mode).lower() in {"realtime", "pending"}:
        return "formal"
    return str(current_mode)


def write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def result_report_payload(
    result: LiveResult | None,
    stats: Any,
    *,
    timing_offset_ms: int,
    suggested_timing_offset_ms: int | None,
    run_context: LiveRunContext | None = None,
    result_status: str | None = None,
    reason: str | None = None,
) -> dict:
    context = run_context.to_mapping() if run_context is not None else {}
    status = result_status or ("stable" if result is not None else "failed")
    mode = str(context.get("mode") or "")
    calibration_run = mode == "calibration" or mode.startswith("calibration-")
    if (
        result is not None
        and calibration_run
        and context.get("song_id", "unknown") == "unknown"
    ):
        status = "unknown_song"
    valid = result is not None and status in {"stable", "experimental"}
    payload = {
        "schema_version": 1,
        "valid": valid,
        "result_status": status,
        "run_id": context.get("run_id"),
        "song_id": context.get("song_id", "unknown"),
        "song_id_method": context.get("song_id_method", "unknown"),
        "started_at": context.get("started_at"),
        "mode": context.get("mode"),
        "difficulty": context.get("difficulty"),
        "profile": context.get("profile_name"),
        "session": context,
        "settings": context.get("settings", {}),
        "debug_recording_path": context.get("recording_path"),
        "eligible_for_profile_acceptance": (
            valid and context.get("mode") != "visual-evaluation"
        ),
        "initial_timing_offset_ms": timing_offset_ms,
        "current_timing_offset_ms": stats.final_timing_offset_ms,
        "suggested_timing_offset_ms": suggested_timing_offset_ms,
        "realtime_feedback_fast": stats.timing_feedback_fast,
        "realtime_feedback_slow": stats.timing_feedback_slow,
        "realtime_feedback_valid": stats.timing_feedback_valid,
        "realtime_feedback_ignored": stats.timing_feedback_ignored,
        "realtime_feedback_ignored_reasons": stats.timing_feedback_ignored_reasons,
        "filtered_adjacent_artifacts": stats.filtered_adjacent_artifacts,
        "rejected_hold_candidates": stats.rejected_hold_candidates,
        "recovered_contacts": stats.recovered_contacts,
        "processed_frames": stats.processed_frames,
        "dispatched_actions": stats.dispatched_actions,
        "action_counts": stats.action_counts,
        "frame_interval_p50_ms": stats.frame_interval_p50_ms,
        "frame_interval_p95_ms": stats.frame_interval_p95_ms,
        "frame_interval_max_ms": stats.frame_interval_max_ms,
        "stage_timings_ms": getattr(stats, "stage_timings_ms", {}),
        "frame_interval_outliers": list(
            getattr(stats, "frame_interval_outliers", ())
        ),
        "cleanup_failed": bool(getattr(stats, "cleanup_failed", False)),
        "cleanup_errors": list(getattr(stats, "cleanup_errors", ())),
        "recorder_error": getattr(stats, "recorder_error", None),
        "startup_timed_out": bool(
            getattr(stats, "startup_timed_out", False)
        ),
        "effective_fps": stats.effective_fps,
        "terminal_reason": stats.terminal_reason,
    }
    if result is not None:
        payload.update(result.to_dict())
    if reason is not None:
        payload["reason"] = reason
    return payload


def _prepare_preflight_context(
    params: dict,
    *,
    visual_settings: Any | None,
    performance_snapshot: PreflightPerformanceSnapshot | None,
    run_context: LiveRunContext | None,
) -> LiveRunContext | None:
    base = run_context or current_live_run()
    if base is None:
        return None
    changes: dict[str, Any] = {
        "mode": _preflight_mode(params, base.mode),
        "difficulty": str(params.get("difficulty", base.difficulty)),
        "actual_note_speed": None,
        "note_skin_type": None,
        "tap_effect": None,
        "judgement_assist": None,
        "recording_path": None,
        "prepared_for_play": False,
    }
    if params.get("note_speed") is not None:
        changes["expected_note_speed"] = float(params["note_speed"])
    if visual_settings is not None:
        changes.update({
            "note_skin_type": int(visual_settings.note_skin_type),
            "tap_effect": int(visual_settings.tap_effect),
            "judgement_assist": bool(
                visual_settings.judgement_assist_effect
            ),
        })
    if performance_snapshot is not None:
        changes.update({
            "expected_note_speed": float(
                performance_snapshot.expected_note_speed
            ),
            "profile_name": performance_snapshot.profile,
        })
        if performance_snapshot.actual_note_speed is not None:
            changes["actual_note_speed"] = float(
                performance_snapshot.actual_note_speed
            )
    current = current_live_run()
    if current is not None and current.run_id == base.run_id:
        return update_live_run(**changes)
    return replace(base, **changes)


def write_preflight_terminal_result(
    *,
    output_dir: Path,
    params: dict,
    terminal_stage: str,
    reason: str,
    visual_settings: Any | None = None,
    performance_snapshot: PreflightPerformanceSnapshot | None = None,
    performance_settings: Any | None = None,
    run_context: LiveRunContext | None = None,
) -> Path | None:
    """Write a zero-frame terminal result once a per-round session exists.

    The earlier visual-settings gate currently runs before ``LiveRunContext``
    is created.  Returning ``None`` for that known lifecycle gap prevents an
    artifact with a fabricated run id; callers still preserve task failure.
    """
    if performance_snapshot is None and performance_settings is not None:
        performance_snapshot = PreflightPerformanceSnapshot(
            expected_note_speed=float(performance_settings.expected_note_speed),
            actual_note_speed=float(performance_settings.actual_note_speed),
            profile=performance_settings.profile,
        )
    run_context = _prepare_preflight_context(
        params,
        visual_settings=visual_settings,
        performance_snapshot=performance_snapshot,
        run_context=run_context,
    )
    if run_context is None:
        return None
    timing_offset_ms = int(params.get("timing_offset_ms", 0))
    stats = EngineStats(
        0,
        0,
        False,
        initial_timing_offset_ms=timing_offset_ms,
        final_timing_offset_ms=timing_offset_ms,
        terminal_reason=reason,
    )
    payload = result_report_payload(
        None,
        stats,
        timing_offset_ms=timing_offset_ms,
        suggested_timing_offset_ms=None,
        run_context=run_context,
        result_status="preflight_error",
        reason=reason,
    )
    payload["terminal_stage"] = str(terminal_stage)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = (
        datetime.now().strftime("%Y%m%d-%H%M%S")
        + f"-{run_context.run_id[:8]}"
    )
    path = output_dir / f"realtime-result-{stamp}.json"
    write_json_atomic(path, payload)
    return path
