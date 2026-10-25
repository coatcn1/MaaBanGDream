"""已执行单键后的连续方向观测；每一步都等待真实效果后重新收集证据。"""

from collections import Counter, deque
from dataclasses import asdict
import math

import numpy as np

from .timing_feedback import TimingFeedback, TimingFeedbackDetector, probe_judgement_feedback


class NativeLifeFeedback:
    SNIFF_CAPACITY = 256
    RENDER_WAIT_SECONDS = .025
    TICKET_TTL_SECONDS = .2
    SAMPLE_INTERVAL_SECONDS = .03
    LOCAL_TARGET_REJECTIONS = frozenset({"new-judgement-overwrote-ticket",
        "published-judgement-may-cover-frame", "ticket-expired", "frame-before-target-render",
        "before-observation-epoch"})

    def __init__(self, backend, *, detector=None):
        self.backend = backend
        self.events = deque(maxlen=128)
        self.sniff_records = deque(maxlen=self.SNIFF_CAPACITY)
        self.sniff_dropped = 0
        self.summary = Counter()
        self.ignored_reasons = Counter()
        self.scene_valid = False
        self.last_sequence = -1
        self.last_fresh_at = None
        self.frame_evidence = {}
        self.life_evidence = {}
        self.votes = deque(maxlen=3)
        self._pending_frame_at = None
        self.qualified_direction = None
        self.qualified_at = None
        self._expected_run_id = getattr(backend, "_run_id", None)
        self._phase_status = None
        self._pending_observed_s = None
        self._pending_sequence = None
        self._effect_status = None
        self._seen_effect_sequence = None
        self._blocked_direction = None
        self._terminal_phase_reason = None
        self._last_status_notice = None
        self._slot_epoch = None
        self._status_trusted = False
        self._owned_sequences = deque(maxlen=64)
        self._last_applied_sequence = None
        self._last_submit_s = float("-inf")
        self._target = None
        self._target_blank = False
        self._target_capture_due = None
        self._target_prefetched = False
        self._consumed_tokens = deque(maxlen=128)
        self._blank_baseline = None
        self._last_blank_at = None
        self._guard_active = False
        self._guard_epoch = 0
        self._phase_effect_epoch = None
        self._phase_effect_unresolved = False
        self._seen_cumulative_phase_ms = None
        self._minimum_target_actual_s = float("-inf")
        self.disabled_reason = None
        self.burst_started_at = None
        self.last_sniff = None
        self.roi_worthy = False

    def active(self, now):
        return False

    def finish_due(self, now):
        return False

    def _state(self):
        reader = getattr(self.backend, "feedback_observation_snapshot", None)
        return reader() if callable(reader) else None

    def _clear_trend(self, reason, *, revoke=True):
        self.votes.clear()
        self.qualified_direction = self.qualified_at = None
        if revoke:
            revoke_request = getattr(self.backend, "revoke_future_phase", None)
            if callable(revoke_request):
                revoke_request(reason)

    def _event(self, record):
        if len(self.events) == self.events.maxlen:
            self.summary["events_dropped"] += 1
        self.events.append(record)

    def _ignore(self, reason):
        self.ignored_reasons[reason] += 1
        self.summary["ignored_events"] += 1
        return reason



    def _consume_target(self):
        if self._target is not None:
            self._consumed_tokens.append(self._target.action_token)
        self._target = None
        self._target_capture_due = None
        self._target_blank = False
        self._target_prefetched = False

    def close(self, now, reason):
        self._clear_trend(reason)
        self.scene_valid = False
        self._pending_frame_at = None
        self._blank_baseline = None
        self._consume_target()
        self._ignore(reason)
        self._event(dict(event=reason, timestamp=now, **self.frame_evidence))

    def disable(self, now, reason):
        self.disabled_reason = reason
        self.close(now, "observer-disabled")

    def observe_scene(self, now, *, visible):
        if not visible:
            self.close(now, "invalid-scene")
        else:
            self.scene_valid = True

    def capture_due(self, now):
        if self.disabled_reason:
            return None
        self.refresh_phase_status(now)
        if self._guard_active:
            return None
        state = self._state()
        if self._target is not None:
            reason = self._target_reason(state, now)
            if reason:
                self._event(dict(event="ticket-discarded", timestamp=now,
                                 action_token=self._target.action_token, reason=reason))
                # 新目标无法归属只丢其证据，不倒改先前三票已提交的合法请求。
                self._clear_trend(reason, revoke=reason not in self.LOCAL_TARGET_REJECTIONS)
                self._ignore(reason)
                self._consume_target()
        if self._target is None and state is not None and state.ticket is not None:
            ticket = state.ticket
            if ticket.action_token not in self._consumed_tokens:
                self._target = ticket
                self._target_capture_due = max(now, ticket.actual_s + ticket.clock_uncertainty_ms / 1000 + self.RENDER_WAIT_SECONDS)
                self._event(dict(event="ticket-selected", timestamp=now, **asdict(ticket)))
                reason = self._target_reason(state, now)
                if reason:
                    self._clear_trend(reason, revoke=reason not in self.LOCAL_TARGET_REJECTIONS)
                    self._ignore(reason)
                    self._consume_target()
        return self._target_capture_due

    def capture_request_due(self, now):
        return self._target is not None and not self._target_prefetched and now >= self._target_capture_due

    def capture_requested(self, now):
        self._target_prefetched = True
        self._target_capture_due = now + self.SAMPLE_INTERVAL_SECONDS

    def _target_reason(self, state, now):
        target = self._target
        if target is None:
            return "no-ticket"
        if target.run_id != self._expected_run_id:
            return "foreign-run-ticket"
        if target.actual_s - target.clock_uncertainty_ms / 1000 < self._minimum_target_actual_s:
            return "before-observation-epoch"
        if state is None or state.judgement_sequence != target.judgement_sequence:
            return "new-judgement-overwrote-ticket"
        if now - target.actual_s > self.TICKET_TTL_SECONDS:
            return "ticket-expired"
        if state.next_judgement_lower_s is not None and state.next_judgement_lower_s <= now:
            return "published-judgement-may-cover-frame"
        return None

    def fresh(self, metadata, now):
        sequence, request_s = metadata.get("sequence", -1), metadata.get("request_s")
        self._pending_frame_at = None
        self.frame_evidence = dict(frame_sequence=sequence, request_s=request_s,
            reused=metadata.get("reused", True), observed_s=now,
            request_age_ms=(now - request_s) * 1000 if type(request_s) in {int, float} else None)
        valid = (type(sequence) is int and sequence > self.last_sequence and not metadata.get("reused", True)
                 and type(request_s) in {int, float} and math.isfinite(request_s) and 0 <= now - request_s <= .080)
        if type(sequence) is int:
            self.last_sequence = max(sequence, self.last_sequence)
        if not valid:
            self.summary["invalid_frames"] += 1
            self._clear_trend("slow-or-reused-frames")
            self._blank_baseline = None
            self._ignore("slow-or-reused-frames")
            self._sniff(now, None, None, None, "slow-or-reused-frames", self._target)
            self._target_capture_due = now + self.SAMPLE_INTERVAL_SECONDS if self._target else None
            self._target_prefetched = False
            return False
        if self.last_fresh_at is not None and now - self.last_fresh_at > .5:
            self._clear_trend("sampling-gap")
            self._blank_baseline = None
            self._ignore("sampling-gap")
        self.last_fresh_at = now
        self.summary["fresh_frames"] += 1
        self._pending_frame_at = now
        return True

    def observe_life(self, value, now, *, visible):
        self.life_evidence = dict(value=value, visible=bool(visible), observed_s=now)

    def _source_follows_effect(self, source):
        effect = self._effect_status
        if effect is None:
            return True
        if (source is None or source.run_id != effect.run_id or effect.first_adjusted_due_s is None
                or effect.first_affected_chunk_sequence is None or effect.first_affected_action_token is None):
            return False
        return (source.chunk_sequence >= effect.first_affected_chunk_sequence
                and (effect.first_affected_action_token is None or source.action_token >= effect.first_affected_action_token)
                and source.planned_s >= effect.first_adjusted_due_s
                and source.actual_s - source.clock_uncertainty_ms / 1000 >= effect.first_adjusted_due_s)

    def _sniff(self, now, kind, slow, fast, decision, source):
        record = dict(timestamp=now, **self.frame_evidence, scene_valid=self.scene_valid,
            slow_px=slow, fast_px=fast, kind=kind.value if kind else None, decision=decision,
            input=asdict(source) if source is not None else None,
            request_sequence=self._phase_status.request_sequence if self._phase_status else None,
            phase_state=self._phase_status.state if self._phase_status else None,
            phase_reason=self._phase_status.terminal_reason if self._phase_status else None,
            cumulative_phase_ms=self._phase_status.cumulative_phase_ms if self._phase_status else None)
        record.update(direction_vote_count=len(self.votes),
                      effect_sequence=self._effect_status.request_sequence if self._effect_status else None,
                      effect_first_adjusted_due_s=self._effect_status.first_adjusted_due_s if self._effect_status else None,
                      effect_chunk=self._effect_status.first_affected_chunk_sequence if self._effect_status else None,
                      effect_token=self._effect_status.first_affected_action_token if self._effect_status else None)
        if len(self.sniff_records) == self.SNIFF_CAPACITY:
            self.sniff_dropped += 1
        self.sniff_records.append(record)
        self.last_sniff = record

    def sniff_report(self):
        return dict(schema_version=3, capacity=self.SNIFF_CAPACITY, dropped=self.sniff_dropped,
                    raw_roi_included=False, ignored_reasons=dict(self.ignored_reasons),
                    continuation=dict(post_effect_blank_required=False, new_effect_source_required=True,
                                      pending_sequence=self._pending_sequence),
                    disabled_reason=self.disabled_reason, records=list(self.sniff_records))

    def observe_frame(self, image, now):
        self.roi_worthy = False
        if self._pending_frame_at != now or self.disabled_reason:
            return
        self._pending_frame_at = None
        if not self.scene_valid or not isinstance(image, np.ndarray) or image.shape != (720, 1280, 3):
            self.close(now, "invalid-scene" if not self.scene_valid else "sampling-error")
            return
        self.summary["sampling_frames"] += 1
        target_at_entry = self._target
        self.roi_worthy = target_at_entry is not None
        self.refresh_phase_status(now)
        kind, slow, fast = probe_judgement_feedback(image)
        minimum = TimingFeedbackDetector.MIN_COLOURED_PIXELS
        signals = {direction for direction, pixels in ((TimingFeedback.FAST, fast), (TimingFeedback.SLOW, slow)) if pixels >= minimum}
        direction = self.qualified_direction or (self.votes[-1][0] if self.votes else None)
        status = self._phase_status
        if (direction is None and self._pending_observed_s is not None and status is not None
                and status.owner == "feedback" and status.original_observed_s == self._pending_observed_s
                and (self._pending_sequence is None or status.request_sequence == self._pending_sequence)
                and status.state in {"requested", "waiting-touch-boundary", "waiting-gap", "waiting-execution-receipts"}):
            # 局部丢票可能已清空趋势，真实反色仍须能否定本来源的 pending。
            direction = TimingFeedback.FAST if status.delta_ms > 0 else TimingFeedback.SLOW
        if direction is not None and signals and any(value != direction for value in signals):
            self._clear_trend("direction-reversed")
        if signals:
            self.summary["raw_sightings"] += 1
            self.summary["sightings"] += 1
        state, target = self._state(), self._target
        decision = self._observe_signal(kind, signals, state, now)
        self._sniff(now, kind, slow, fast, decision, target_at_entry or target)

    def _observe_signal(self, kind, signals, state, now):
        request_s = self.frame_evidence["request_s"]
        if self._guard_active:
            return self._ignore("visual-owner-active")
        if len(signals) > 1:
            self._clear_trend("mixed-feedback")
            self._blank_baseline = None
            self._consume_target()
            return self._ignore("mixed-feedback")
        if self._target is None:
            if not signals and state is not None and (state.last_actual_s is None or
                    request_s > state.last_actual_s + (state.last_uncertainty_ms or 0) / 1000) and (
                    state.next_judgement_lower_s is None or state.next_judgement_lower_s > now):
                self._blank_baseline = (state.judgement_sequence, request_s)
                self.summary["blank_baselines"] += 1
                self._last_blank_at = now
                return "blank-baseline"
            if signals:
                self._blank_baseline = None
            return "no-target-ticket"
        target = self._target
        reason = self._target_reason(state, now)
        if reason:
            self._clear_trend(reason, revoke=reason not in self.LOCAL_TARGET_REJECTIONS)
            self._consume_target()
            return self._ignore(reason)
        if request_s < target.actual_s + target.clock_uncertainty_ms / 1000 + self.RENDER_WAIT_SECONDS:
            self._clear_trend("frame-before-target-render", revoke=False)
            self._target_capture_due = now + self.SAMPLE_INTERVAL_SECONDS
            self._target_prefetched = False
            return self._ignore("frame-before-target-render")
        if not signals:
            self._clear_trend("neutral-target")
            if not self._target_blank:
                self._target_blank = True
                self._target_capture_due = now + self.SAMPLE_INTERVAL_SECONDS
                self._target_prefetched = False
                return "target-blank-awaiting-render"
            self._consume_target()
            return self._ignore("neutral-target")
        self._blank_baseline = None
        self._consume_target()
        if kind is None:
            self._clear_trend("ambiguous-colour")
            return self._ignore("ambiguous-colour")
        if self._phase_effect_unresolved:
            self._clear_trend("unresolved-phase-effect")
            return self._ignore("unresolved-phase-effect")
        if not self._source_follows_effect(target):
            self._clear_trend("waiting-effect-execution", revoke=False)
            return self._ignore("waiting-effect-execution")
        # 这里计量按键后的新图方向，不宣称同色字样已刷新成独立视觉判定。
        self.summary["single_tap_observations"] += 1
        self.summary[kind.value + "_observations"] += 1
        self.summary["debounced_reports"] += 1
        if (self._pending_observed_s is not None or self._phase_status is None
                or self._phase_status.state in {"requested", "waiting-touch-boundary", "waiting-gap", "waiting-execution-receipts"}):
            self._clear_trend("waiting-phase-request", revoke=False)
            return self._ignore("waiting-phase-request")
        if self.votes and self.votes[-1][0] != kind:
            self._clear_trend("direction-reversed")
        self.summary["eligible_events"] += 1
        self.summary[kind.value + "_votes"] += 1
        self.summary["direction_votes"] += 1
        self.votes.append((kind, target.action_token))
        self.roi_worthy = True
        if len(self.votes) < 3:
            return "eligible-single-tap"
        self.qualified_direction, self.qualified_at = kind, now
        if (self._pending_observed_s is not None or self._phase_status is None
                or self._phase_status.state in {"requested", "waiting-touch-boundary", "waiting-gap", "waiting-execution-receipts"}
                or self._terminal_phase_reason is not None or self._blocked_direction == kind):
            return self._ignore("pending-or-blocked")
        if now - self._last_submit_s < .3:
            return self._ignore("request-cooldown")
        delta = 15.0 if kind is TimingFeedback.FAST else -15.0
        # 每次提交消耗当前三票，拒绝也必须从全新目标重新收集，不能定时重放。
        self._last_submit_s = now
        self.summary["requests"] += 1
        self.votes.clear()
        accepted = self.backend.request_future_phase(delta, now)
        self.summary["accepted_requests" if accepted else "rejected_requests"] += 1
        reason = getattr(self.backend, "_phase_request_rejection_reason", None) if not accepted else None
        if reason:
            self.ignored_reasons["request-rejected:" + reason] += 1
        self._event(dict(event="phase-request", timestamp=now, delta_ms=delta, accepted=accepted,
                         reason=reason, action_token=target.action_token, **self.frame_evidence))
        if accepted:
            self._pending_observed_s = now
            self._pending_sequence = None
            self.refresh_phase_status(now)
        else:
            self._reset_observation_epoch(now, "request-not-accepted")
        return "requested" if accepted else "request-rejected"

    def _reset_observation_epoch(self, now, reason):
        # 新相位或视觉声明只让旧观测失效，不撤销已由仲裁槽接受的请求。
        self._clear_trend(reason, revoke=False)
        self._blank_baseline = None
        self._last_blank_at = None
        self._minimum_target_actual_s = now
        self._consume_target()
        self._event(dict(event=reason, timestamp=now))

    def refresh_phase_status(self, now):
        guard = bool(getattr(self.backend, "_visual_phase_guard_requested",
                             getattr(self.backend, "_visual_phase_guard", False)))
        guard_epoch = getattr(self.backend, "_visual_phase_guard_epoch", 0)
        if guard != self._guard_active or guard_epoch != self._guard_epoch:
            self._guard_active = guard
            self._guard_epoch = guard_epoch
            self._reset_observation_epoch(now, "visual-guard-enabled" if guard else "visual-guard-released")
        reader = getattr(self.backend, "phase_status_snapshot", None)
        status = reader() if callable(reader) else None
        if status is None or (self._expected_run_id is not None and status.run_id != self._expected_run_id):
            self._phase_status = None
            self._status_trusted = False
            self._reset_observation_epoch(now, "untrusted-phase-status")
            return False
        if not self._status_trusted and self._slot_epoch is not None:
            self._reset_observation_epoch(now, "phase-status-restored")
        self._status_trusted = True
        slot_epoch = (status.owner, status.request_sequence, status.state, status.original_observed_s)
        if self._slot_epoch is not None and slot_epoch != self._slot_epoch:
            self._reset_observation_epoch(now, "phase-slot-epoch-changed")
        self._slot_epoch = slot_epoch
        self._phase_status = status
        if status.state == "stopped" or status.terminal_reason in {
            "no-future-actions", "not-running", "stopped", "invalid-clock", "invalid-request"
        }:
            self._terminal_phase_reason = status.terminal_reason or "stopped"
        effect_reader = getattr(self.backend, "phase_effect_snapshot", None)
        effect = effect_reader() if callable(effect_reader) else None
        if status.state == "applied" and (effect is None or status.event_cursor > effect.event_cursor):
            effect = status
        if effect is not None and effect.run_id != self._expected_run_id:
            effect = None
        unresolved = (effect is None and status.cumulative_phase_ms != 0 or effect is not None
            and status.event_cursor > effect.event_cursor and status.cumulative_phase_ms != effect.cumulative_phase_ms)
        if unresolved and (not self._phase_effect_unresolved or self._seen_cumulative_phase_ms != status.cumulative_phase_ms):
            self._reset_observation_epoch(now, "unresolved-phase-effect")
        self._phase_effect_unresolved = unresolved
        self._seen_cumulative_phase_ms = status.cumulative_phase_ms
        if effect is not None:
            epoch = (effect.run_id, effect.request_sequence, effect.cumulative_phase_ms,
                     effect.first_adjusted_due_s, effect.first_affected_chunk_sequence,
                     effect.first_affected_action_token)
            if epoch != self._phase_effect_epoch:
                self._phase_effect_epoch = epoch
                self._reset_observation_epoch(now, "phase-effect-epoch-changed")
            # 任意来源的真实应用都会改变后续画面归属；只把本来源计入应用次数。
            self._effect_status = effect
            # 只有真实反向应用释放了完整一步余量，才解除对应方向的上限阻塞。
            cumulative = effect.cumulative_phase_ms
            if (self._blocked_direction is TimingFeedback.FAST and cumulative <= 185
                    or self._blocked_direction is TimingFeedback.SLOW and cumulative >= -185):
                self._blocked_direction = None
            if effect.request_sequence in self._owned_sequences:
                # revoke 与 owner 应用交错时仍保留真实已应用相位，不能遗失效果边界。
                if self._last_applied_sequence != effect.request_sequence:
                    self._last_applied_sequence = effect.request_sequence
                    self.summary["applied_requests"] += 1
            if (effect.owner == "feedback" and self._pending_observed_s is not None
                    and effect.original_observed_s == self._pending_observed_s
                    and (self._pending_sequence is None or effect.request_sequence == self._pending_sequence)):
                # owner 可能在截图线程首次读到 requested 前已完成应用。
                # 先登记本方回执再清 pending，避免 accepted=1/applied=0 的假诊断。
                if effect.request_sequence not in self._owned_sequences:
                    self._owned_sequences.append(effect.request_sequence)
                if self._last_applied_sequence != effect.request_sequence:
                    self._last_applied_sequence = effect.request_sequence
                    self.summary["applied_requests"] += 1
                self._pending_observed_s = self._pending_sequence = None
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
                    self._blocked_direction = TimingFeedback.FAST if status.delta_ms > 0 else TimingFeedback.SLOW
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
            self.votes.clear()
            self._blank_baseline = None
            self._consume_target()
        return True
