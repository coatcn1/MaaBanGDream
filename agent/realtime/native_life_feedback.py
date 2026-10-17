"""Native 常开基础观测、有界候选加密及独立判定事件反馈。"""

from collections import Counter, deque
from dataclasses import asdict
import math

import numpy as np

from .timing_feedback import TimingFeedback, TimingFeedbackDetector, probe_judgement_feedback


class NativeLifeFeedback:
    WINDOW_SECONDS = .8
    SAMPLE_INTERVAL_SECONDS = .05
    TREND_SECONDS = 1.0
    SNIFF_CAPACITY = 4096

    def __init__(self, backend, *, detector=None):
        self.backend = backend
        self.events = deque(maxlen=128)
        self.sniff_records = deque(maxlen=self.SNIFF_CAPACITY)
        self.sniff_dropped = 0
        self.summary = Counter()
        self.ignored_reasons = Counter()
        self.burst_started_at = None
        self.burst_until = 0.0
        self.scene_valid = False
        self.last_sequence = -1
        self.last_fresh_at = None
        self.frame_evidence = {}
        self.life_evidence = {}
        self.votes = deque(maxlen=2)
        self._pending_frame_at = None
        self.qualified_direction = None
        self.qualified_at = None
        self._expected_run_id = getattr(backend, "_run_id", None)
        self._phase_status = None
        self._pending_observed_s = None
        self._pending_sequence = None
        self._effect_status = None
        self._seen_effect_sequence = None
        self._post_effect_blank_sequence = None
        self._effect_blank_count = 0
        self._blocked_direction = None
        self._terminal_phase_reason = None
        self._last_status_notice = None
        self._visible_direction = None
        self._event_locked = False
        self._candidate_direction = None
        self._candidate_at = None
        self._blank_count = 0
        self._last_blank_at = None
        self._last_event_token = None
        self._last_submit_s = float("-inf")
        self._owned_sequences = deque(maxlen=64)
        self._last_applied_sequence = None
        self.last_sniff = None
        self.roi_worthy = False

    def active(self, now):
        return self.scene_valid and self.burst_started_at is not None and now < self.burst_until

    def finish_due(self, now):
        return self.burst_started_at is not None and now >= self.burst_until

    def _clear_trend(self, reason):
        self.votes.clear()
        self.qualified_direction = None
        self.qualified_at = None
        self._pending_observed_s = None
        self._pending_sequence = None
        revoke = getattr(self.backend, "revoke_future_phase", None)
        if callable(revoke):
            revoke(reason)

    def _event(self, record):
        if len(self.events) == self.events.maxlen:
            self.summary["events_dropped"] += 1
        self.events.append(record)

    def close(self, now, reason):
        self._clear_trend(reason)
        self.scene_valid = False
        self.burst_started_at = None
        self.burst_until = 0.0
        self._pending_frame_at = None
        self._candidate_direction = None
        self._candidate_at = None
        self._blank_count = self._effect_blank_count = 0
        self._post_effect_blank_sequence = None
        # 场景或帧失效不能把仍未消退的旧字样重新计数，恢复后仍需可信分离。
        self._event_locked = self._event_locked or self._visible_direction is not None
        self.ignored_reasons[reason] += 1
        self.summary["ignored_events"] += 1
        self._event({"event": reason, "timestamp": now, **self.frame_evidence})

    def observe_scene(self, now, *, visible):
        if not visible:
            self.close(now, "invalid-scene")
            return
        self.scene_valid = True
        if self.finish_due(now):
            self.burst_started_at = None
        if self.votes and now - self.votes[-1][1] > self.TREND_SECONDS:
            self.votes.clear()
            self.qualified_direction = None

    def fresh(self, metadata, now):
        sequence = metadata.get("sequence", -1)
        request_s = metadata.get("request_s")
        self._pending_frame_at = None
        self.frame_evidence = {"frame_sequence": sequence, "request_s": request_s,
            "reused": metadata.get("reused", True), "observed_s": now,
            "request_age_ms": (now - request_s) * 1000 if type(request_s) in {int, float} else None}
        valid = (type(sequence) is int and sequence > self.last_sequence
                 and not metadata.get("reused", True)
                 and type(request_s) in {int, float} and math.isfinite(request_s)
                 and 0 <= now - request_s <= .080)
        if type(sequence) is int:
            self.last_sequence = max(sequence, self.last_sequence)
        if not valid:
            self.summary["invalid_frames"] += 1
            self.close(now, "slow-or-reused-frames")
            self._sniff(now, None, None, None, "slow-or-reused-frames", None)
            return False
        if self.last_fresh_at is not None and now - self.last_fresh_at > .5:
            self.close(now, "sampling-gap")
        self.last_fresh_at = now
        self.summary["fresh_frames"] += 1
        self._pending_frame_at = now
        return True

    def observe_life(self, value, now, *, visible):
        self.life_evidence = {"value": value, "visible": bool(visible), "observed_s": now}

    def _source_follows_effect(self, source):
        effect = self._effect_status
        if effect is None:
            return True
        if source is None or effect.first_adjusted_due_s is None or effect.first_affected_chunk_sequence is None:
            return False
        return (source.chunk_sequence >= effect.first_affected_chunk_sequence
                and (effect.first_affected_action_token is None or source.action_token >= effect.first_affected_action_token)
                and source.planned_s >= effect.first_adjusted_due_s
                and source.actual_s - source.clock_uncertainty_ms / 1000.0 >= effect.first_adjusted_due_s)

    def _sniff(self, now, kind, slow, fast, decision, source):
        evidence = (asdict(source) if hasattr(source, "__dataclass_fields__") else vars(source).copy()) if source is not None else None
        record = {"timestamp": now, **self.frame_evidence, "scene_valid": self.scene_valid,
            "slow_px": slow, "fast_px": fast, "kind": kind.value if kind else None,
            "decision": decision, "input": evidence,
            "request_sequence": self._phase_status.request_sequence if self._phase_status else None,
            "phase_state": self._phase_status.state if self._phase_status else None,
            "phase_reason": self._phase_status.terminal_reason if self._phase_status else None,
            "cumulative_phase_ms": self._phase_status.cumulative_phase_ms if self._phase_status else None,
            "effect_sequence": self._effect_status.request_sequence if self._effect_status else None,
            "post_effect_blank_sequence": self._post_effect_blank_sequence}
        record.update({key: self.summary[key] for key in
                       ("sampling_frames", "raw_sightings", "distinct_events", "eligible_events")})
        if len(self.sniff_records) == self.SNIFF_CAPACITY:
            self.sniff_dropped += 1
        self.sniff_records.append(record)
        self.last_sniff = record

    def sniff_report(self):
        return {"schema_version": 2, "capacity": self.SNIFF_CAPACITY,
                "dropped": self.sniff_dropped, "raw_roi_included": False,
                "records": list(self.sniff_records)}

    def observe_frame(self, image, now):
        self.roi_worthy = False
        if self._pending_frame_at != now:
            return
        self._pending_frame_at = None
        if not self.scene_valid or not isinstance(image, np.ndarray) or image.shape != (720, 1280, 3):
            self.close(now, "invalid-scene" if not self.scene_valid else "sampling-error")
            return
        self.summary["sampling_frames"] += 1
        kind, slow, fast = probe_judgement_feedback(image)
        minimum = TimingFeedbackDetector.MIN_COLOURED_PIXELS
        signals = {direction for direction, pixels in ((TimingFeedback.FAST, fast), (TimingFeedback.SLOW, slow)) if pixels >= minimum}
        if signals:
            self.summary["raw_sightings"] += 1
            self.summary["sightings"] += 1
            for direction in signals:
                self.summary[direction.value + "_sightings"] += 1
        # 视觉失效处理先于输入资格；HOLD/释放不能让被反色否定的旧请求落地。
        direction = self.qualified_direction or (self.votes[-1][0] if self.votes else self._visible_direction)
        if len(signals) > 1:
            self._clear_trend("mixed-feedback")
            self._event_locked = True
            self._visible_direction = None
            self._candidate_at = self._candidate_direction = None
            self._blank_count = self._effect_blank_count = 0
            self.roi_worthy = True
            self.ignored_reasons["mixed-feedback"] += 1
            self._sniff(now, kind, slow, fast, "mixed-feedback", None)
            return
        if signals and direction is not None and any(value != direction for value in signals):
            self._clear_trend("direction-reversed")
            self._event_locked = False
            self._candidate_at = self._candidate_direction = None
            self.roi_worthy = True
        self.refresh_phase_status(now)
        reader = getattr(self.backend, "feedback_input_snapshot", None)
        source = reader(self.frame_evidence["request_s"], now) if callable(reader) else None
        decision = self._observe_signal(kind, signals, source, now)
        self._sniff(now, kind, slow, fast, decision, source)

    def _observe_signal(self, kind, signals, source, now):
        if not signals:
            self._candidate_direction = self._candidate_at = None
            contiguous = self._last_blank_at is not None and now - self._last_blank_at <= .25
            self._blank_count = self._blank_count + 1 if contiguous else 1
            self._last_blank_at = now
            if self._effect_status is not None and source is not None and self._source_follows_effect(source):
                self._effect_blank_count = self._effect_blank_count + 1 if contiguous else 1
            else:
                self._effect_blank_count = 0
            if self._blank_count >= 2:
                self._event_locked = False
                self._visible_direction = None
            if self._effect_status is not None and self._effect_blank_count >= 2:
                self._post_effect_blank_sequence = self._effect_status.request_sequence
            return "blank"
        self._blank_count = self._effect_blank_count = 0
        self._last_blank_at = None
        if kind is None:
            return "ambiguous-colour"
        self._visible_direction = kind
        if self._event_locked:
            return "sustained-glyph"
        if self._candidate_direction != kind or self._candidate_at is None or now - self._candidate_at > .080:
            self._candidate_direction, self._candidate_at = kind, now
            if not self.active(now):
                self.burst_started_at = now
                self.burst_until = now + self.WINDOW_SECONDS
                self.summary["candidate_bursts"] += 1
            self.roi_worthy = True
            return "candidate"
        self._event_locked = True
        self._candidate_at = None
        self.summary["distinct_events"] += 1
        self.summary["debounced_reports"] += 1
        self.summary[kind.value + "_events"] += 1
        self.roi_worthy = True
        if source is None:
            self.ignored_reasons["ineligible-input"] += 1
            self.summary["ignored_events"] += 1
            return "ineligible-input"
        if source.action_token == self._last_event_token:
            self.ignored_reasons["duplicate-input-token"] += 1
            return "duplicate-input-token"
        self._last_event_token = source.action_token
        if (not self._source_follows_effect(source) or self._effect_status is not None
                and self._post_effect_blank_sequence != self._effect_status.request_sequence):
            self.ignored_reasons["before-effect-blank"] += 1
            return "before-effect-blank"
        self.summary["eligible_events"] += 1
        self.summary[kind.value + "_eligible_events"] += 1
        # 兼容字段明确映射到合格独立事件，不再是窗口票或 fresh 帧数。
        self.summary[kind.value + "_votes"] += 1
        self.summary["direction_votes"] += 1
        if self.votes and (self.votes[-1][0] != kind or now - self.votes[-1][1] > self.TREND_SECONDS):
            self.votes.clear()
        self.votes.append((kind, now))
        if len(self.votes) < 2:
            return "eligible-event"
        self.qualified_direction, self.qualified_at = kind, now
        if (self._pending_observed_s is not None or self._phase_status is None
                or self._phase_status.state in {"requested", "waiting-touch-boundary", "waiting-gap"}
                or self._terminal_phase_reason is not None or self._blocked_direction == kind):
            return "pending-or-blocked"
        if now - self._last_submit_s < .3:
            return "request-cooldown"
        delta = 15.0 if kind is TimingFeedback.FAST else -15.0
        self._last_submit_s = now
        self.summary["requests"] += 1
        accepted = self.backend.request_future_phase(delta, now)
        self.summary["accepted_requests" if accepted else "rejected_requests"] += 1
        self._event({"event": "phase-request", "timestamp": now,
                            "delta_ms": delta, "accepted": accepted, **self.frame_evidence})
        self.votes.clear()
        if accepted:
            self._pending_observed_s = now
            self._pending_sequence = None
            self.refresh_phase_status(now)
        return "requested" if accepted else "request-rejected"

    def refresh_phase_status(self, now):
        reader = getattr(self.backend, "phase_status_snapshot", None)
        status = reader() if callable(reader) else None
        if status is None or (self._expected_run_id is not None and status.run_id != self._expected_run_id):
            self._phase_status = None
            return False
        self._phase_status = status
        if status.state == "stopped" or status.terminal_reason in {
            "no-future-actions", "not-running", "stopped", "invalid-clock", "invalid-request"
        }:
            self._terminal_phase_reason = status.terminal_reason or "stopped"
        if status.state == "applied":
            # 只有真实反向应用释放了完整一步余量，才解除对应方向的上限阻塞。
            cumulative = status.cumulative_phase_ms
            if (self._blocked_direction is TimingFeedback.FAST and cumulative <= 185
                    or self._blocked_direction is TimingFeedback.SLOW and cumulative >= -185):
                self._blocked_direction = None
            if status.request_sequence in self._owned_sequences:
                # revoke 与 owner 应用交错时仍保留真实已应用相位，不能遗失效果边界。
                self._effect_status = status
                if self._last_applied_sequence != status.request_sequence:
                    self._last_applied_sequence = status.request_sequence
                    self.summary["applied_requests"] += 1
        if self._pending_observed_s is not None:
            same = (status.original_observed_s == self._pending_observed_s
                    and (self._pending_sequence is None or status.request_sequence == self._pending_sequence))
            if not same:
                return False
            self._pending_sequence = status.request_sequence
            if status.request_sequence not in self._owned_sequences:
                self._owned_sequences.append(status.request_sequence)
            if status.state == "applied":
                self._effect_status = status
                self._pending_observed_s = None
                self._pending_sequence = None
            elif status.state in {"expired", "rejected", "revoked", "stopped"}:
                self._pending_observed_s = None
                self._pending_sequence = None
                if status.terminal_reason == "phase-limit":
                    self._blocked_direction = self.qualified_direction
                elif status.terminal_reason in {"no-future-actions", "not-running", "stopped", "invalid-clock", "invalid-request"}:
                    self._terminal_phase_reason = status.terminal_reason
        elif self._effect_status is not None and status.state == "applied" and (
            status.request_sequence == self._effect_status.request_sequence
        ):
            # effect-published 的新 revision 会补齐先前 applied 尚未知的效果边界。
            self._effect_status = status
        notice = (status.run_id, status.request_sequence, status.event_cursor, status.state)
        if notice != self._last_status_notice:
            self._last_status_notice = notice
            self._event({"event": "phase-status", "timestamp": now,
                                "request_sequence": status.request_sequence, "state": status.state,
                                "event_cursor": status.event_cursor, "reason": status.terminal_reason,
                                "first_adjusted_due_s": status.first_adjusted_due_s})
        if self._effect_status is not None and self._seen_effect_sequence != self._effect_status.request_sequence:
            self._seen_effect_sequence = self._effect_status.request_sequence
            self._post_effect_blank_sequence = None
            self._effect_blank_count = 0
            self.votes.clear()
        return True
