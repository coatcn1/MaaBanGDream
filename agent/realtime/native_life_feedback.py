"""单截图消费者的有界 Native 判定反馈窗口。"""

from collections import Counter, deque
import math

import numpy as np

from .timing_feedback import TimingFeedback, TimingFeedbackDetector


class NativeLifeFeedback:
    WINDOW_PERIOD_SECONDS = 3.0
    WINDOW_SECONDS = .8
    SAMPLE_INTERVAL_SECONDS = .05
    TREND_SECONDS = 12.0

    def __init__(self, backend, *, detector=None):
        self.backend = backend
        self.detector = detector or TimingFeedbackDetector()
        self.events = deque(maxlen=128)
        self.summary = Counter()
        self.ignored_reasons = Counter()
        self.burst_started_at = None
        self.burst_until = 0.0
        self.next_window_at = 0.0
        self.last_sequence = -1
        self.last_fresh_at = None
        self.frame_evidence = {}
        self.life_evidence = {}
        self.votes = deque(maxlen=3)
        self._pending_frame_at = None
        self._window_direction = None
        self._window_directions = set()
        self._last_signal_at = None
        self._last_signal_evidence = {}
        self.mixed = False
        self.qualified_direction = None
        self.qualified_at = None
        self._expected_run_id = getattr(backend, "_run_id", None)
        self._phase_status = None
        self._pending_observed_s = None
        self._pending_sequence = None
        self._effect_status = None
        self._last_submit_s = float("-inf")
        self._blocked_direction = None
        self._terminal_phase_reason = None
        self._last_signal_input = None
        self._last_status_notice = None

    def active(self, now):
        # 纯查询不得在终点或生命复核前提交第三票。
        return self.burst_started_at is not None and now < self.burst_until

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

    def _expire_votes(self, now):
        while self.votes and now - self.votes[0][1] > self.TREND_SECONDS:
            self.votes.popleft()
        if self.qualified_at is not None and now - self.qualified_at > self.TREND_SECONDS:
            self._clear_trend("trend-expired")

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
            if (self._blocked_direction is TimingFeedback.FAST and cumulative <= 38
                    or self._blocked_direction is TimingFeedback.SLOW and cumulative >= -38):
                self._blocked_direction = None
        if self._pending_observed_s is not None:
            same = (status.original_observed_s == self._pending_observed_s
                    and (self._pending_sequence is None or status.request_sequence == self._pending_sequence))
            if not same:
                return False
            self._pending_sequence = status.request_sequence
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
            self.events.append({"event": "phase-status", "timestamp": now,
                                "request_sequence": status.request_sequence, "state": status.state,
                                "event_cursor": status.event_cursor, "reason": status.terminal_reason,
                                "first_adjusted_due_s": status.first_adjusted_due_s})
        return True

    def _signal_follows_effect(self, observed_s):
        effect = self._effect_status
        if effect is None:
            return True
        source = self._last_signal_input
        due = effect.first_adjusted_due_s
        chunk = effect.first_affected_chunk_sequence
        token = effect.first_affected_action_token
        if due is None or chunk is None or source is None:
            return False
        planned = source.transient_planned_s
        actual = source.transient_actual_s
        request_s = self._last_signal_evidence.get("request_s")
        return (planned is not None and actual is not None
                and source.transient_chunk_sequence is not None
                and source.transient_action_token is not None
                and source.transient_chunk_sequence >= chunk
                and (token is None or source.transient_action_token >= token)
                and planned >= due and actual >= due
                and request_s is not None and actual <= request_s
                and 0 <= observed_s - actual <= .6)

    def _finish_vote(self, now):
        reason = None
        if self.mixed:
            reason = "mixed-feedback"
        elif self.last_fresh_at is None or min(now, self.burst_until) - self.last_fresh_at > .080:
            # 结窗新图不能覆盖窗内末帧时间，避免跨 deadline 的卡顿伪造完整窗口。
            reason = "sampling-gap"
            self.summary["sampling_gaps"] += 1
        elif self._window_direction is None or self._last_signal_at is None:
            reason = "no-feedback"
        elif now - self.burst_started_at >= self.WINDOW_PERIOD_SECONDS:
            reason = "stale-window"
        elif not self.backend.feedback_input_eligible(now):
            reason = "ineligible-input"
        if reason is not None:
            self.summary["ignored_windows"] += 1
            self.ignored_reasons[reason] += 1
            if reason == "no-feedback":
                self._expire_votes(now)
            else:
                self._clear_trend(reason)
            return reason
        direction = self._window_direction
        observed_s = self._last_signal_at
        self._expire_votes(observed_s)
        if self.votes and self.votes[-1][0] != direction:
            self._clear_trend("direction-changed")
        self.votes.append((direction, observed_s))
        self.summary[direction.value + "_votes"] += 1
        self.summary["direction_votes"] += 1
        self.events.append({"event": "window-vote", "timestamp": now,
                            "direction": direction.value, "signal_observed_s": observed_s,
                            "window_start_s": self.burst_started_at,
                            "trend_votes": len(self.votes), **self._last_signal_evidence})
        if len(self.votes) == 3:
            self.qualified_direction = direction
        if self.qualified_direction == direction:
            self.qualified_at = observed_s
        status_ready = self.refresh_phase_status(now)
        if (self.qualified_direction == direction and status_ready
                and self._pending_observed_s is None and self._terminal_phase_reason is None
                and self._phase_status.state not in {"requested", "waiting-touch-boundary", "waiting-gap"}
                and self._blocked_direction != direction and now - self._last_submit_s >= 3
                and self._signal_follows_effect(observed_s)):
            # FAST 表示输入偏早，未来执行延后；SLOW 则提前，不改冻结的 Profile 偏移。
            delta_ms = 2.0 if direction is TimingFeedback.FAST else -2.0
            self._last_submit_s = now
            # 结窗抖动不能让下一窗提前于节流边界，又白等一个三秒周期。
            earliest_start = now + self.WINDOW_PERIOD_SECONDS - self.WINDOW_SECONDS
            if earliest_start > self.next_window_at:
                self.next_window_at = math.nextafter(earliest_start, math.inf)
            self.summary["requests"] += 1
            accepted = self.backend.request_future_phase(delta_ms, observed_s)
            if accepted:
                self._pending_observed_s = observed_s
                self._pending_sequence = None
                self.refresh_phase_status(now)
            self.summary["accepted_requests" if accepted else "rejected_requests"] += 1
            self.events.append({"event": "phase-request", "timestamp": now,
                                "signal_observed_s": observed_s, "delta_ms": delta_ms,
                                "accepted": accepted, **self._last_signal_evidence})
        return "window-ended"

    def close(self, now, reason):
        if self.burst_started_at is not None:
            if reason == "window-ended":
                try:
                    reason = self._finish_vote(now)
                except Exception as exc:
                    self.events.append({"event": "sampling-error", "timestamp": now,
                                        "reason": type(exc).__name__})
                    reason = "sampling-error"
            else:
                self.summary["ignored_windows"] += 1
                self.ignored_reasons[reason] += 1
            self.summary[reason] += 1
            self.events.append({"event": reason, "timestamp": now,
                                "window_start_s": self.burst_started_at,
                                **self.frame_evidence, "observed_s": now})
        if reason not in {"window-ended", "no-feedback"}:
            self._clear_trend(reason)
        self.burst_started_at = None
        self.burst_until = 0.0
        self._pending_frame_at = None
        self.last_fresh_at = None

    def observe_scene(self, now, *, visible):
        """只由已开始的 Native 演奏场证据驱动，不消费生命值或掉血次数。"""
        if not visible:
            self.close(now, "invalid-scene")
            return
        self._expire_votes(now)
        if self.finish_due(now):
            self.close(now, "window-ended")
        if self.burst_started_at is not None or now < self.next_window_at:
            return
        self.burst_started_at = now
        self.burst_until = now + self.WINDOW_SECONDS
        # 以实际开始时刻安排下一窗，卡顿后不追补已经错过的窗口。
        self.next_window_at = now + self.WINDOW_PERIOD_SECONDS
        self.summary["windows"] += 1
        self.last_fresh_at = None
        self._window_direction = None
        self._window_directions.clear()
        self._last_signal_at = None
        self._last_signal_evidence = {}
        self._last_signal_input = None
        self.mixed = False
        self.detector = TimingFeedbackDetector()
        self.events.append({"event": "periodic-window", "timestamp": now,
                            "window_start_s": now, "window_end_s": self.burst_until,
                            **self.frame_evidence, "life": dict(self.life_evidence)})

    def fresh(self, metadata, now):
        sequence = metadata.get("sequence", -1)
        request_s = metadata.get("request_s")
        self._pending_frame_at = None
        # 两个时间都在既有 monotonic 域内，request_s 仍不代表游戏渲染时刻。
        self.frame_evidence = {"frame_sequence": sequence, "request_s": request_s,
                               "reused": metadata.get("reused", True), "observed_s": now,
                               "request_age_ms": ((now - request_s) * 1000
                               if isinstance(request_s, (int, float)) else None)}
        valid = (type(sequence) is int and sequence > self.last_sequence
                 and not metadata.get("reused", True)
                 and type(request_s) in {int, float} and math.isfinite(request_s)
                 and 0 <= now - request_s <= .080)
        if type(sequence) is int:
            self.last_sequence = max(sequence, self.last_sequence)
        if not valid:
            self.summary["invalid_frames"] += 1
            if self.burst_started_at is not None:
                self.close(now, "slow-or-reused-frames")
            return False
        self.summary["fresh_frames"] += 1
        if self.active(now):
            if self.last_fresh_at is not None and now - self.last_fresh_at > .080:
                self.summary["sampling_gaps"] += 1
                self.close(now, "sampling-gap")
                return True
            self.last_fresh_at = now
        self._pending_frame_at = now
        return True

    def observe_life(self, value, now, *, visible):
        self.life_evidence = {"value": value, "visible": bool(visible), "observed_s": now}

    def observe_frame(self, image, now):
        if not self.active(now) or self._pending_frame_at != now:
            return
        self._pending_frame_at = None
        try:
            if not isinstance(image, np.ndarray) or image.shape != (720, 1280, 3):
                self.close(now, "sampling-error")
                return
            self.last_fresh_at = now
            if not self.backend.feedback_input_eligible(now):
                # 释放保护只打断当前连续证据，不撤销此前已经取得的可信单向证据。
                # 空白帧也检查资格，避免两侧孤立色块跨过不合格区间拼成 2/3。
                self.summary["ineligible_frames"] += 1
                self.detector = TimingFeedbackDetector()
                return
            self.summary["sampling_frames"] += 1
            feedback = self.detector.detect(image)
            signals = self.detector.last_raw_directions
            if signals:
                self.summary["sightings"] += 1
            if feedback is not None:
                self.summary["debounced_reports"] += 1
                self.summary[feedback.value + "_reports"] += 1
            if not signals:
                return
            if self.qualified_direction is not None and any(kind != self.qualified_direction for kind in signals):
                # 反向的可信原始颜色当帧撤销旧请求，不等整窗结束，更不伪撤销已应用偏移。
                self._clear_trend("direction-reversed")
            elif self.votes and any(kind != self.votes[-1][0] for kind in signals):
                self._clear_trend("direction-reversed")
            self._window_directions.update(signals)
            if len(self._window_directions) > 1:
                if not self.mixed:
                    self.mixed = True
                    self._clear_trend("mixed-feedback")
                return
            if feedback is not None:
                self._window_direction = feedback
            if self._window_direction is not None:
                self.refresh_phase_status(now)
                self._last_signal_at = now
                self._last_signal_evidence = dict(self.frame_evidence)
                self._last_signal_input = self._phase_status
        except Exception as exc:
            self.events.append({"event": "sampling-error", "reason": type(exc).__name__,
                                "timestamp": now, **self.frame_evidence})
            self.close(now, "sampling-error")
