"""输入清理后尽力保存单局数字审计，不依赖调试录像或结果采集。"""

from pathlib import Path
import re

from .optional_diagnostics import safe_print
from .run_reporting import write_json_atomic


def save_native_feedback_audit(root, stats, live_run, frozen_offset_ms, *, stopping=False):
    if stopping or stats is None or getattr(stats, "stopped", False):
        return None
    report = getattr(stats, "native_report", None)
    if not report:
        return None
    try:
        run_id = str(live_run.run_id)
        if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", run_id):
            raise ValueError("invalid run ID")
        feedback = report.get("life_feedback", {})
        payload = dict(schema_version=1, run_id=run_id, song_id=live_run.song_id,
            effective_difficulty=getattr(live_run, "effective_difficulty", None) or live_run.difficulty,
            frozen_profile_offset_ms=report.get("frozen_timing_offset_ms", frozen_offset_ms),
            completed=stats.completed, cleanup_failed=stats.cleanup_failed,
            release_confirmed=report.get("release_confirmed"),
            terminal_reason=stats.terminal_reason,
            life_feedback=feedback)
        path = Path(root) / "debug" / "native-feedback" / f"{run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, payload)
        summary, phase = feedback.get("summary", {}), feedback.get("phase_status", {})
        safe_print("NativeFeedback audit=" + str(path)
            + f" profile_offset_ms={payload['frozen_profile_offset_ms']}"
            + f" requests={summary.get('requests', 0)} accepted={summary.get('accepted_requests', 0)}"
            + f" applied={summary.get('applied_requests', 0)} phase_ms={phase.get('cumulative_phase_ms')}"
            + f" phase_state={phase.get('state')} phase_reason={phase.get('terminal_reason')}", flush=True)
        return path
    except Exception as exc:
        safe_print(f"NativeFeedback audit_warning={type(exc).__name__}: {exc}", flush=True)
        return None
