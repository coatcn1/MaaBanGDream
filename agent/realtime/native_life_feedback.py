"""已执行普通 TAP 组后的连续方向观测；每一步等待真实效果后重新取证。"""

from collections import Counter, deque
from dataclasses import asdict
import math

import numpy as np

from .timing_feedback import TimingFeedback, TimingFeedbackDetector, probe_judgement_feedback
from .native_clocked_feedback import ClockedFeedbackEvidence
from .native_visual_refresh import NativeVisualRefreshEvidence


class NativeLifeFeedback:
    SNIFF_CAPACITY = 256
    RENDER_WAIT_SECONDS = .025
    TICKET_TTL_SECONDS = .2
    SAMPLE_INTERVAL_SECONDS = .03
    # 稀疏方向票有独立寿命，不延长截图图龄、目标寿命或已接受请求的有效期。
    VOTE_WINDOW_SECONDS = 10.0
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
        self._frame_upper_s = None
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
        self._clocked = ClockedFeedbackEvidence(backend) if getattr(backend, '_native_clock_trial', False) else None
        self._visual_refresh = (NativeVisualRefreshEvidence(backend)
            if (getattr(backend, '_native_clock_trial', False) and getattr(backend, 'life_feedback_enabled', False)
                and getattr(backend, '_native_visual_refresh_enabled', False)) else None)
        self._visual_refresh_context = None
        self._visual_refresh_capture_due = float('-inf')
        self._clocked_key = None
        self._clocked_capture_due = float('-inf')
        self._clocked_capture_token = None
        self._clocked_capture_attempts = {}
        self._clocked_selected_due = None
        self._clocked_provenance = None

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

    def _expire_votes(self, now):
        expired = 0
        while self.votes and now - self.votes[0][2] > self.VOTE_WINDOW_SECONDS:
            self.votes.popleft()
            expired += 1
        if expired:
            self.qualified_direction = self.qualified_at = None
            self.summary["expired_direction_votes"] += expired
            self._event(dict(event="direction-votes-expired", timestamp=now, count=expired,
                             window_seconds=self.VOTE_WINDOW_SECONDS))

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
        if self._clocked is not None:
            self._clocked.reset(now)
        if self._visual_refresh is not None:
            self._visual_refresh.reset()
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
        if self._visual_refresh is not None:
            self.refresh_phase_status(now)
            self._expire_votes(now)
            self._drain_visual_refresh(now)
            try:
                if (self.scene_valid and not self._visual_refresh.disabled_reason
                        and self._visual_refresh._state(now) is not None):
                    # 仍由唯一异步capture流按原30ms兴趣节拍合并请求，不另开截图流。
                    return max(now, self._visual_refresh_capture_due)
            except Exception as exc:
                self._visual_refresh.disable(exc)
            return None
        if self._clocked is not None:
            self.refresh_phase_status(now)
            self._expire_votes(now)
            self._drain_clocked(now)
            if self._guard_active or self._clocked_key is None:
                self._clocked_selected_due = None
                return None
            rows = self.backend.execution_commit_snapshot()
            tokens = {row.action_tokens[0] for row in rows if row.action_tokens}
            self._clocked_capture_attempts = {token: count for token, count in self._clocked_capture_attempts.items()
                                             if token in tokens}
            candidate = next((row for row in reversed(rows) if any(action.kind in {'tap','flick','down','up'}
                              for action in row.actions)), None)
            if (candidate is None or not self._clocked._single_tap(candidate)
                    or self._clocked_capture_attempts.get(candidate.action_tokens[0], 0) >= 2
                    or not self._clocked.minimum_host_s <= candidate.received_host_s <= now
                    or now - candidate.received_host_s > self.TICKET_TTL_SECONDS):
                self._clocked_selected_due = None
                return None
            self._clocked_capture_token = candidate.action_tokens[0]
            # RX只给“实际已执行”的H上界；先取图再等probe，绝不从RX差估计相位。
            self._clocked_selected_due = max(self._clocked_capture_due,
                candidate.received_host_s + self.RENDER_WAIT_SECONDS + .002)
            return self._clocked_selected_due
        self.refresh_phase_status(now)
        self._expire_votes(now)
        if self._guard_active:
            return None
        state = self._state()
        if self._target is not None:
            # 预取的图可能早已完成，须先消费其时间上下界，不能按迟到的处理时刻丢票。
            # 最多保留 TTL 加既有 80ms 图龄，截图卡住仍会有界释放目标。
            pending_frame = self._target_prefetched and now - self._target.actual_s <= self.TICKET_TTL_SECONDS + .080
            reason = None if pending_frame else self._target_reason(state, now)
            if reason:
                self._event(dict(event="ticket-discarded", timestamp=now,
                                 action_token=self._target.action_token, reason=reason))
                # 新目标无法归属只丢其证据，不倒改先前三票已提交的合法请求。
                if reason not in self.LOCAL_TARGET_REJECTIONS:
                    self._clear_trend(reason)
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
                    if reason not in self.LOCAL_TARGET_REJECTIONS:
                        self._clear_trend(reason)
                    self._ignore(reason)
                    self._consume_target()
        return self._target_capture_due

    def capture_request_due(self, now):
        if self._visual_refresh is not None:
            return self.capture_due(now) is not None and now >= self._visual_refresh_capture_due
        if self._clocked is not None:
            return (not self._guard_active and self._clocked_selected_due is not None
                    and now >= self._clocked_selected_due)
        return self._target is not None and not self._target_prefetched and now >= self._target_capture_due

    def capture_requested(self, now):
        if self._visual_refresh is not None:
            self._visual_refresh_capture_due = now + self.SAMPLE_INTERVAL_SECONDS
            return
        if self._clocked is not None:
            self._clocked_capture_due = now + self.SAMPLE_INTERVAL_SECONDS
            token = self._clocked_capture_token
            if token is not None:
                self._clocked_capture_attempts[token] = self._clocked_capture_attempts.get(token, 0) + 1
            return
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
        if state is None:
            return "new-judgement-overwrote-ticket"
        if state.judgement_sequence != target.judgement_sequence:
            # 回执线程可领先截图消费者多次判定；必须连续证明最早后继也晚于整次截图。
            following = [row for row in state.judgement_history if row[1] > target.judgement_sequence]
            cursor = target.judgement_sequence + 1
            for first, last, actual, uncertainty in following:
                if (first != cursor or uncertainty is None or not math.isfinite(uncertainty)
                        or not 0 <= uncertainty <= 1 or actual - uncertainty / 1000 <= now):
                    return "new-judgement-overwrote-ticket"
                cursor = last + 1
            if cursor != state.judgement_sequence + 1:
                return "new-judgement-overwrote-ticket"
        if now - target.actual_s > self.TICKET_TTL_SECONDS:
            return "ticket-expired"
        if state.next_judgement_lower_s is not None and state.next_judgement_lower_s <= now:
            return "published-judgement-may-cover-frame"
        return None

    def fresh(self, metadata, now):
        if self._visual_refresh is not None:
            # 复用图也推进post兴趣预算，不能在没有新帧时立即重试形成忙循环。
            self._visual_refresh_capture_due = now + self.SAMPLE_INTERVAL_SECONDS
        self._expire_votes(now)
        sequence, request_s = metadata.get("sequence", -1), metadata.get("request_s")
        self._pending_frame_at = None
        self.frame_evidence = dict(frame_sequence=sequence, request_s=request_s,
            reused=metadata.get("reused", True), observed_s=now,
            request_age_ms=(now - request_s) * 1000 if type(request_s) in {int, float} else None)
        upper = metadata.get("first_done_observed_s")
        # first_done 是宿主首次确认截图完成的上界，不能当作设备精确渲染时刻。
        self._frame_upper_s = (upper if type(upper) in {int, float} and math.isfinite(upper)
            and type(request_s) in {int, float} and request_s <= upper <= now else now)
        self.frame_evidence["capture_upper_s"] = self._frame_upper_s
        valid = (type(sequence) is int and sequence > self.last_sequence and not metadata.get("reused", True)
                 and type(request_s) in {int, float} and math.isfinite(request_s) and 0 <= now - request_s <= .080)
        if type(sequence) is int:
            self.last_sequence = max(sequence, self.last_sequence)
        if not valid:
            self.summary["invalid_frames"] += 1
            # 单次异步复用既不投票，也不否定此前真实方向或正在等待触点的请求。
            # 只有连续缺少新鲜画面超过既有上限，才撤销本来源的证据与请求。
            if self.last_fresh_at is not None and now - self.last_fresh_at > .5:
                self._clear_trend("sampling-gap")
                self._ignore("sampling-gap")
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
                      direction_vote_window_seconds=self.VOTE_WINDOW_SECONDS,
                      oldest_direction_vote_age_ms=(now - self.votes[0][2]) * 1000 if self.votes else None,
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
                                      direction_vote_window_seconds=self.VOTE_WINDOW_SECONDS,
                                      pending_sequence=self._pending_sequence),
                    disabled_reason=self.disabled_reason,
                    **({'visual_refresh': self._visual_refresh.report()} if self._visual_refresh is not None else {}),
                    clocked_evidence=(dict(pending=len(self._clocked.frames), capacity=self._clocked.CAPACITY,
                        dropped=self._clocked.dropped, contradicted_frames=self._clocked.contradicted_frames)
                        if self._clocked is not None else None), records=list(self.sniff_records))

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
        if self._clocked is None and direction is not None and signals and any(value != direction for value in signals):
            self._clear_trend("direction-reversed")
        if signals:
            self.summary["raw_sightings"] += 1
            self.summary["sightings"] += 1
        if self._visual_refresh is not None:
            # ROI保存使用此刻的image和原metadata；异步旧事件不能篡改当前图的标签。
            self.roi_worthy = False
            try:
                self._visual_refresh.enqueue(image, self.frame_evidence, self._frame_upper_s,
                    now, kind, signals, slow, fast)
                self._drain_visual_refresh(now)
            except Exception as exc:
                self._visual_refresh.disable(exc)
                self._clear_trend('visual-refresh-error', revoke=False)
            self._sniff(now, kind, slow, fast, 'visual-refresh-sample', None)
            return
        if self._clocked is not None:
            self._observe_clocked_frame(kind, signals, slow, fast, now)
            return
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
        reason = self._target_reason(state, self._frame_upper_s)
        if reason:
            if reason not in self.LOCAL_TARGET_REJECTIONS:
                self._clear_trend(reason)
            self._consume_target()
            return self._ignore(reason)
        if request_s < target.actual_s + target.clock_uncertainty_ms / 1000 + self.RENDER_WAIT_SECONDS:
            self._target_capture_due = now + self.SAMPLE_INTERVAL_SECONDS
            self._target_prefetched = False
            return self._ignore("frame-before-target-render")
        if not signals:
            # 无方向字样可能尚未渲染，不能当作 PERFECT 或反向证据撤销合法 pending。
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
        return self._vote_direction(kind, target, now)

    def _vote_direction(self, kind, target, now, *, observed_s=None, refresh_id=None, refresh_context=None):
        if refresh_id is not None and refresh_context is None:
            return self._ignore('refresh-context-unavailable')
        observation_s = now if observed_s is None else observed_s
        # 这里计量按键后的新图方向，不宣称同色字样已刷新成独立视觉判定。
        self.summary["visual_refresh_observations" if refresh_id is not None else "single_tap_observations"] += 1
        self.summary[kind.value + "_observations"] += 1
        self.summary["debounced_reports"] += 1
        status = self._phase_status
        if (self._clocked is not None and self._pending_observed_s is not None and status is not None
                and status.owner == 'feedback' and status.original_observed_s == self._pending_observed_s
                and (self._pending_sequence is None or status.request_sequence == self._pending_sequence)
                and (kind is TimingFeedback.FAST and status.delta_ms < 0
                     or kind is TimingFeedback.SLOW and status.delta_ms > 0)):
            # 仅已闭合归属的反色能否定本方旧三票；空白、未 seal 图及别来源槽位不在此入口。
            self._clear_trend('direction-reversed', revoke=False)
            self.backend.revoke_future_phase('direction-reversed', owner='feedback',
                                             expected_sequence=status.request_sequence)
            self.refresh_phase_status(now)
        if (self._pending_observed_s is not None or self._phase_status is None
                or self._phase_status.state in {"requested", "waiting-touch-boundary", "waiting-gap", "waiting-execution-receipts"}):
            self._clear_trend("waiting-phase-request", revoke=False)
            return self._ignore("waiting-phase-request")
        if self.votes and self.votes[-1][0] != kind:
            self._clear_trend("direction-reversed")
        self.summary["eligible_events"] += 1
        self.summary[kind.value + "_votes"] += 1
        self.summary["direction_votes"] += 1
        self.votes.append((kind, ('visual-refresh', refresh_id) if refresh_id is not None else target.action_token, observation_s))
        self.roi_worthy = True
        if len(self.votes) < 3:
            return "eligible-single-tap"
        self.qualified_direction, self.qualified_at = kind, observation_s
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
        accepted = self.backend.request_future_phase(delta, observation_s,
            **({'expected_observation_context': refresh_context} if refresh_id is not None else {}))
        self.summary["accepted_requests" if accepted else "rejected_requests"] += 1
        reason = getattr(self.backend, "_phase_request_rejection_reason", None) if not accepted else None
        if reason:
            self.ignored_reasons["request-rejected:" + reason] += 1
        self._event(dict(event="phase-request", timestamp=now, delta_ms=delta, accepted=accepted,
                         reason=reason, action_token=target.action_token if target is not None else None,
                         **({'visual_refresh_id': refresh_id} if refresh_id is not None else {}), **self.frame_evidence))
        if accepted:
            self._pending_observed_s = observation_s
            self._pending_sequence = None
            self.refresh_phase_status(now)
        else:
            self._reset_observation_epoch(now, "request-not-accepted")
        return "requested" if accepted else "request-rejected"

    def _drain_visual_refresh(self, now):
        try:
            pulses = self._visual_refresh.drain(now)
            context = self._visual_refresh.context
            if self._visual_refresh_context is not None and context != self._visual_refresh_context:
                self._clear_trend('visual-refresh-context-changed', revoke=False)
            self._visual_refresh_context = context
            original, upper, worthy = self.frame_evidence, self._frame_upper_s, self.roi_worthy
            try:
                for window in pulses:
                    current = window['current']
                    self.frame_evidence = dict(frame_sequence=current['frame_sequence'],
                        request_s=current['request_s'], capture_upper_s=current['capture_upper_s'],
                        observed_s=current['observed_s'], reused=False)
                    self._frame_upper_s = current['capture_upper_s']
                    self._submit_direction_window(window, now)
            finally:
                self.frame_evidence, self._frame_upper_s, self.roi_worthy = original, upper, worthy
        except Exception as exc:
            self._visual_refresh.disable(exc)
            self._clear_trend('visual-refresh-error', revoke=False)

    def _submit_direction_window(self, window, now):
        frames=window['frames']
        if (len(frames)!=3 or len({frame['frame_sequence'] for frame in frames})!=3
                or self._phase_status is None or self._phase_effect_unresolved
                or self._pending_observed_s is not None or self._phase_status.state in {
                    'requested','waiting-touch-boundary','waiting-gap','waiting-execution-receipts'}):
            return self._discard_direction_window(window,now,'direction-window-pending-or-unresolved')
        kind=TimingFeedback(frames[-1]['kind'])
        if self._terminal_phase_reason is not None or self._blocked_direction==kind:
            return self._discard_direction_window(window,now,'pending-or-blocked')
        if now-self._last_submit_s<.3:
            return self._discard_direction_window(window,now,'request-cooldown')
        # 三项是真实原图sequence，不是把一次刷新伪造成三个独立GR。
        self.summary['fresh_direction_frames']+=3
        self.summary['qualified_direction_windows']+=1
        self.summary['debounced_reports']+=1
        self.summary[kind.value+'_observations']+=3
        self.summary[kind.value+'_votes']+=3
        self.summary['direction_votes']+=3
        self.summary['eligible_events']+=1
        self.qualified_direction,self.qualified_at=kind,frames[-1]['observed_s']
        self._last_submit_s=now
        self.summary['requests']+=1
        delta=15. if kind is TimingFeedback.FAST else -15.
        observed=frames[-1]['observed_s']
        accepted=self.backend.request_future_phase(delta,observed,
            expected_observation_context=window['sealed_context'])
        self.summary['accepted_requests' if accepted else 'rejected_requests']+=1
        reason=getattr(self.backend,'_phase_request_rejection_reason',None) if not accepted else None
        if reason:self.ignored_reasons['request-rejected:'+reason]+=1
        self._event(dict(event='phase-request',timestamp=now,delta_ms=delta,accepted=accepted,reason=reason,
            action_token=None,direction_window_id=window['window_id'],unit='qualified-direction-window',
            frame_sequences=[frame['frame_sequence'] for frame in frames],**self.frame_evidence))
        self.votes.clear()
        if accepted:
            self._pending_observed_s=observed
            self._pending_sequence=None
            self.refresh_phase_status(now)
        else:
            self._reset_observation_epoch(now,'request-not-accepted')
        return 'requested' if accepted else 'request-rejected'

    def _discard_direction_window(self,window,now,reason):
        # 早拒只消费当前窗；解除等context变化的锁存，但后续必须重建全新峰谷及三帧。
        # 不修改_last_submit_s，不重放旧窗，也不清全局sequence高水位。
        self._visual_refresh.detector.reset()
        self.summary['discarded_direction_windows']+=1
        self._event(dict(event='direction-window-discarded',timestamp=now,reason=reason,
            direction_window_id=window['window_id'],
            frame_sequences=[frame['frame_sequence'] for frame in window['frames']]))
        return self._ignore(reason)

    def take_roi_evidence(self):
        return self._visual_refresh.take_roi_evidence() if self._visual_refresh is not None else ()

    def acknowledge_roi_evidence(self,sequence,accepted):
        if self._visual_refresh is not None:
            self._visual_refresh.acknowledge_roi_evidence(sequence,accepted)

    @property
    def direction_window_mode(self):
        return self._visual_refresh is not None

    def _observe_clocked_frame(self, kind, signals, slow, fast, now):
        if self._guard_active:
            self._sniff(now, kind, slow, fast, self._ignore('visual-owner-active'), None)
            return
        reason = self._clocked.enqueue(self.frame_evidence, self._frame_upper_s, now, kind, signals, slow, fast)
        self._sniff(now, kind, slow, fast, reason, None)
        self._drain_clocked(now)

    def _drain_clocked(self, now):
        key = self._clocked.current_key(now)
        provenance = self.backend.observation_provenance()
        if self._clocked_provenance is not None and provenance != self._clocked_provenance:
            self._clear_trend('clock-hard-fence-changed')
        self._clocked_provenance = provenance
        self._clocked_key = key
        decisions = self._clocked.drain(now)
        original_evidence, original_upper = self.frame_evidence, self._frame_upper_s
        for decision in decisions:
            frame, target = decision.frame, decision.target
            self.frame_evidence = dict(frame_sequence=frame.sequence, request_s=frame.request_s,
                capture_upper_s=frame.upper_s, observed_s=frame.observed_s, reused=False,
                request_age_ms=(frame.observed_s - frame.request_s) * 1000,
                attribution_delay_ms=(now - frame.observed_s) * 1000)
            self._frame_upper_s = frame.upper_s
            reason = decision.reason
            if reason == 'qualified':
                self.roi_worthy = True
                if self._guard_active:
                    reason = self._ignore('visual-owner-active')
                elif len(frame.signals) > 1:
                    self._clear_trend('mixed-feedback')
                    reason = self._ignore('mixed-feedback')
                elif not frame.signals:
                    reason = self._ignore('neutral-target')
                elif frame.kind is None:
                    self._clear_trend('ambiguous-colour')
                    reason = self._ignore('ambiguous-colour')
                elif self._phase_effect_unresolved:
                    reason = self._ignore('unresolved-phase-effect')
                elif not self._source_follows_effect(target):
                    reason = self._ignore('waiting-effect-execution')
                else:
                    reason = self._vote_direction(frame.kind, target, now, observed_s=frame.observed_s)
            else:
                self._ignore(reason)
                if (reason.startswith('clock-') and reason not in {
                        'clock-unavailable', 'clock-epoch-changed', 'clock-frame-uncovered'}
                        or reason == 'untrusted-input-execution'):
                    self._clear_trend(reason, revoke=False)
            self._sniff(now, frame.kind, frame.slow_pixels, frame.fast_pixels, reason, target)
        self.frame_evidence, self._frame_upper_s = original_evidence, original_upper

    def _reset_observation_epoch(self, now, reason):
        # 新相位或视觉声明只让旧观测失效，不撤销已由仲裁槽接受的请求。
        self._clear_trend(reason, revoke=False)
        self._blank_baseline = None
        self._last_blank_at = None
        self._minimum_target_actual_s = now
        if self._clocked is not None:
            self._clocked.reset(now)
        if self._visual_refresh is not None:
            self._visual_refresh.reset()
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
