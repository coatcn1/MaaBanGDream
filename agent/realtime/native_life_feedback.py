"""常开判定条计数的有界 Native 相位反馈窗口。"""

from collections import Counter, deque
import math

import numpy as np

from .timing_feedback import TimingFeedback, TimingFeedbackDetector


class NativeLifeFeedback:
    """把判定条的连续同向命中折算成未来相位纠偏请求。

    上游（#90）以「每 3 秒开 0.8 秒窗、窗内 60Hz、3 票同向」的周期窗触发，
    只覆盖约 27% 的时间；偏移在窗外出现时整段无人纠。这里改为**常开 5Hz 判定条
    计数**：平均采样成本与周期窗相当（60Hz×27% ≈ 16Hz vs 5Hz，且 5Hz 覆盖整局），
    但不再有 2.2 秒的盲区。判定条本身即触发源，与生命掉血解耦 —— 掉血的是
    Bad/Miss，纯 Great 偏移局不掉血，上游的掉血窗因此永不触发（真机实测）。

    保留 #90 的安全机制：相位状态轮询（终态/上限阻塞）、效果跟随门禁
    （``_signal_follows_effect``，只对「上一效果已落到输入流之后」的信号继续纠偏）、
    以及单槽信箱。只替换触发模型与边界值。
    """

    # 判定条持续约 1s，5Hz 采样足以命中；常开路径平均成本与上游周期窗相当。
    SAMPLE_INTERVAL_SECONDS = .2
    # 连续同向命中达到阈值即请求一次相位纠偏。真机（.local 分析）显示净偏移
    # 「刚出窗」时判定条命中稀疏（间隔 5~16s），阈值 3 常凑不齐（run 213322 全程
    # 只在 71.8s 凑够一次，前 50s 攒了 32 个 great 没人纠）；降到 2 把触发点提前
    # 几十秒。正常局命中≈0，误触发风险低。
    STREAK_THRESHOLD = 2
    # 相邻同向命中允许的最大间隔，超过即视为新一段，计数清零，防止跨段拼接假连续。
    STREAK_GAP_S = 1.0
    # 两次请求之间的最小间隔；C++ 侧另有单步/累计夹取。真机大偏移局 74215edb
    # （great=54）密集簇 4s 内 19 条同向命中，冷却 1.0s 把请求压到 1 次/秒 → 欠纠；
    # 离线复算（.local/sim_cooldown.py，严格重放命中线）显示 1.0→0.3 把簇内累计从
    # 45ms 提到 90ms。过冲风险低：请求后清零计数，纠过头时反向命中会重新累积并在
    # 阈值内触发反向纠偏（闭环自愈）。
    REQUEST_COOLDOWN_S = 0.3
    # 单步纠偏量(ms)，须与 C++ playback_session 单步上限、native_play delta 白名单、
    # C++ 累计上限保持一致。整体偏移（浏览器刷新类）实测可达上百 ms，2ms 步长在冷却
    # 窗口内追不平。
    PHASE_STEP_MS = 15.0

    def __init__(self, backend, *, detector=None):
        self.backend = backend
        self.detector = detector or TimingFeedbackDetector()
        self.events = deque(maxlen=128)
        self.summary = Counter()
        self.ignored_reasons = Counter()
        self.last_sequence = -1
        self.last_fresh_at = None
        self.frame_evidence = {}
        self.life_evidence = {}
        # 常开计数状态：连续同向命中数与方向，与生命掉血完全解耦。
        self.same_direction = None
        self.same_reports = 0
        self.last_judgement_at = float("-inf")
        self.last_request_at = float("-inf")
        self.mixed = False
        self.scene_valid = False
        self._pending_frame_at = None
        self._expected_run_id = getattr(backend, "_run_id", None)
        self._phase_status = None
        self._pending_observed_s = None
        self._pending_sequence = None
        self._effect_status = None
        self._blocked_direction = None
        self._request_direction = None
        self._terminal_phase_reason = None
        self._last_signal_input = None
        self._last_signal_evidence = {}
        self._last_status_notice = None

    def active(self, now):
        # 演奏场成立即常开；不再有周期性窗口开关。
        return self.scene_valid

    def finish_due(self, now):
        # 常开模型没有「窗口到期」概念；保留接口供引擎终态采样判断调用。
        return False

    def _reset_streak(self):
        self.same_direction = None
        self.same_reports = 0
        self.mixed = False

    def _clear_trend(self, reason):
        self._reset_streak()
        self._pending_observed_s = None
        self._pending_sequence = None
        revoke = getattr(self.backend, "revoke_future_phase", None)
        if callable(revoke):
            revoke(reason)

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
            # 上限 200ms、单步 15ms → 余量回到 185ms 以内即视为已释放。
            cumulative = status.cumulative_phase_ms
            if (self._blocked_direction is TimingFeedback.FAST and cumulative <= 185
                    or self._blocked_direction is TimingFeedback.SLOW and cumulative >= -185):
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
                    self._blocked_direction = self._request_direction
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

    def observe_scene(self, now, *, visible):
        """只由已开始的 Native 演奏场证据驱动，不消费生命值或掉血次数。"""
        if not visible:
            self.close(now, "invalid-scene")
            return
        self.scene_valid = True

    def close(self, now, reason):
        if self.scene_valid:
            self.summary[reason] += 1
            self.events.append({"event": reason, "timestamp": now,
                                "window_start_s": self.last_judgement_at,
                                **self.frame_evidence, "observed_s": now})
        self.scene_valid = False
        self._reset_streak()
        if reason != "invalid-scene":
            self._clear_trend(reason)
        self.last_fresh_at = None
        self._pending_frame_at = None
        self.detector = TimingFeedbackDetector()

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
            if self.scene_valid:
                self.close(now, "slow-or-reused-frames")
            return False
        self.summary["fresh_frames"] += 1
        # 常开 5Hz 下相邻帧间隔约 200ms，不能用 60Hz 窗口的 80ms 判卡顿；
        # 跨段保护由 STREAK_GAP_S 在计数时承担，这里只记一次长停顿诊断。
        if (self.scene_valid and self.last_fresh_at is not None
                and now - self.last_fresh_at > .5):
            self.summary["sampling_gaps"] += 1
        self.last_fresh_at = now
        self._pending_frame_at = now
        return True

    def observe_life(self, value, now, *, visible):
        # 生命只保留诊断证据；常开计数的触发源是判定条，不是掉血。
        self.life_evidence = {"value": value, "visible": bool(visible), "observed_s": now}

    def observe_judgement(self, feedback, now, *, source):
        """常开判定条计数：与生命掉血解耦，连续同向达到阈值即请求相位纠偏。

        判定条只对非 PERFECT 判定出现（PERFECT 不产生 FAST/SLOW 条），所以中间夹
        PERFECT/MISS 时调用方直接不调用本函数，天然不打断累积。反向不再整段否决，
        而是重锚到新方向（计数重置为 1），靠阈值兜底：真·交替（F/S/F/S）永远凑不齐
        阈值，不会触发；``mixed`` 仅作诊断字段。
        """
        if feedback is None:
            return None
        self.summary["sightings"] += 1
        self.summary[feedback.value + "_reports"] += 1
        eligible = self.backend.feedback_input_eligible(now)
        self.events.append({"event": "feedback", "timestamp": now, "source": source,
                            "direction": feedback.value, "eligible": eligible,
                            **self.frame_evidence})
        if not eligible:
            # 不合格只代表「此刻触点窗口不适合发请求」，不代表偏移消失；不清零，
            # 保留已累的同向计数与方向。跨段保护仍由 STREAK_GAP_S 承担。
            self.summary["ignored_reports"] += 1
            self.ignored_reasons["ineligible-input"] += 1
            return "ineligible"
        self.summary["eligible_reports"] += 1
        # 每次命中先刷新相位状态：效果边界、上限阻塞与终态都从缓存快照读取。
        self.refresh_phase_status(now)
        self._last_signal_input = self._phase_status
        self._last_signal_evidence = dict(self.frame_evidence)
        if now - self.last_judgement_at > self.STREAK_GAP_S:
            self._reset_streak()
        self.last_judgement_at = now
        if self.same_direction is not None and feedback != self.same_direction:
            self.mixed = True
        self.same_reports = self.same_reports + 1 if feedback == self.same_direction else 1
        self.same_direction = feedback
        if (self.same_reports >= self.STREAK_THRESHOLD
                and now - self.last_request_at >= self.REQUEST_COOLDOWN_S):
            if (self._pending_observed_s is None
                    and self._terminal_phase_reason is None
                    and self._phase_status is not None
                    and self._phase_status.state not in {"requested", "waiting-touch-boundary", "waiting-gap"}
                    and self._blocked_direction != feedback
                    and self._signal_follows_effect(now)):
                # FAST 表示输入偏早，未来执行延后；SLOW 则提前，不改冻结的 Profile 偏移。
                delta_ms = (self.PHASE_STEP_MS if feedback is TimingFeedback.FAST
                            else -self.PHASE_STEP_MS)
                self.last_request_at = now
                self._request_direction = feedback
                self.summary["requests"] += 1
                accepted = self.backend.request_future_phase(delta_ms, now)
                if accepted:
                    self._pending_observed_s = now
                    self._pending_sequence = None
                    self.refresh_phase_status(now)
                self.summary["accepted_requests" if accepted else "rejected_requests"] += 1
                self.events.append({"event": "phase-request", "timestamp": now, "source": source,
                                    "streak": self.same_reports, "delta_ms": delta_ms,
                                    "accepted": accepted, **self.frame_evidence})
                # 一次请求后重置同向计数：下一次需重新累积到阈值，避免同一簇连发。
                self._reset_streak()
                return "requested"
        return "counted"

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
                # 释放保护只打断当前连续证据，不撤销此前已取得的可信单向证据。
                self.summary["ineligible_frames"] += 1
                self.detector = TimingFeedbackDetector()
                return
            self.summary["sampling_frames"] += 1
            feedback = self.detector.detect(image)
            self.observe_judgement(feedback, now, source="steady")
        except Exception as exc:
            self.events.append({"event": "sampling-error", "reason": type(exc).__name__,
                                "timestamp": now, **self.frame_evidence})
            self.close(now, "sampling-error")
