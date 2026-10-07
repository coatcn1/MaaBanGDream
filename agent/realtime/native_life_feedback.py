"""单截图消费者的有界 Native 掉血反馈窗口。"""

from collections import deque
from collections import Counter
import math

import numpy as np

from .timing_feedback import TimingFeedback, TimingFeedbackDetector


class NativeLifeFeedback:
    def __init__(self, backend, *, detector=None):
        self.backend = backend
        self.detector = detector or TimingFeedbackDetector()
        self.life_samples = deque(maxlen=8)
        self.windows = deque(maxlen=2)
        self.events = deque(maxlen=128)
        self.summary = Counter()
        self.burst_until = 0.0
        self.cooldown_until = 0.0
        self.last_sequence = -1
        self.last_fresh_at = None
        self.frame_evidence = {}
        self.bad_frames = 0
        self.same_direction = None
        self.same_reports = 0
        self.requested = False
        self.mixed = False

    def active(self, now):
        if self.burst_until and now >= self.burst_until:
            self.close(now, "window-ended")
        return now < self.burst_until

    def close(self, now, reason):
        if reason != "window-ended":
            revoke = getattr(self.backend, "revoke_future_phase", None)
            if callable(revoke):
                revoke(reason)
        if self.burst_until:
            self.summary[reason] += 1
            self.events.append({"event": reason, "timestamp": now,
                                **self.frame_evidence, "observed_s": now})
            self.cooldown_until = now + 2.0
        self.burst_until = 0.0
        self.life_samples.clear()
        self.same_direction = None
        self.same_reports = 0
        self.detector = TimingFeedbackDetector()

    def fresh(self, metadata, now):
        sequence = metadata.get("sequence", -1)
        request_s = metadata.get("request_s")
        # request_s 是截图请求时刻，observed_s 是消费者观察时刻，均非渲染时刻。
        self.frame_evidence = {"frame_sequence": sequence, "request_s": request_s,
                               "reused": metadata.get("reused", True), "observed_s": now,
                               "request_age_ms": ((now - request_s) * 1000
                               if isinstance(request_s, (int, float)) else None)}
        valid = (type(sequence) is int and sequence >= 0 and sequence != self.last_sequence
                 and not metadata.get("reused", True)
                 and isinstance(request_s, (int, float)) and math.isfinite(request_s)
                 and 0 <= now - request_s <= .080)
        self.last_sequence = sequence
        if not valid:
            self.summary["invalid_frames"] += 1
            if self.active(now):
                invalidate = getattr(self.detector, "invalidate_history", None)
                if callable(invalidate):
                    invalidate()
                self.same_reports = 0
                self.bad_frames += 1
                if self.bad_frames >= 3:
                    self.close(now, "slow-or-reused-frames")
            return False
        self.bad_frames = 0
        self.summary["fresh_frames"] += 1
        if (self.active(now) and self.last_fresh_at is not None
                and now - self.last_fresh_at > .080):
            self.summary["sampling_gaps"] += 1
            invalidate = getattr(self.detector, "invalidate_history", None)
            if callable(invalidate):
                invalidate()
            self.same_reports = 0
        self.last_fresh_at = now
        return True

    def observe_life(self, value, now, *, visible):
        if not visible:
            self.close(now, "invalid-scene")
            return
        if self.active(now) or now < self.cooldown_until:
            return
        self.life_samples.append((now, int(value), dict(self.frame_evidence)))
        while self.life_samples and now - self.life_samples[0][0] > 1.0:
            self.life_samples.popleft()
        values = [value for _, value, _ in self.life_samples]
        drops = [max(0, before - after) for before, after in zip(values, values[1:])]
        # 生命条已归一化到 0..1000；治疗不抵消跌落，也不能把单帧跳变当连掉血。
        if len(values) < 3 or sum(drop > 0 for drop in drops) < 2 or sum(drops) < 80:
            return
        while self.windows and now - self.windows[0] >= 10.0:
            self.windows.popleft()
        if len(self.windows) >= 2:
            return
        self.windows.append(now)
        self.summary["windows"] += 1
        self.burst_until = now + 2.0
        self.bad_frames = 0
        self.same_reports = 0
        self.same_direction = None
        self.requested = False
        self.mixed = False
        self.detector = TimingFeedbackDetector()
        self.events.append({"event": "life-drop-window", "timestamp": now,
                            "drop": sum(drops), "samples": len(values),
                            **self.frame_evidence,
                            "life_samples": [{"observed_s": at, "value": value, **metadata}
                                             for at, value, metadata in self.life_samples]})

    def observe_frame(self, image, now):
        if not self.active(now):
            return
        try:
            if isinstance(self.detector, TimingFeedbackDetector) and (
                not isinstance(image, np.ndarray) or image.shape != (720, 1280, 3)
            ):
                self.close(now, "sampling-error")
                return
            feedback = self.detector.detect(image)
            if feedback is None:
                return
            self.summary[feedback.value + "_reports"] += 1
            eligible = self.backend.feedback_input_eligible(now)
            self.events.append({"event": "feedback", "timestamp": now,
                                "direction": feedback.value, "eligible": eligible,
                                **self.frame_evidence})
            if not eligible:
                self.summary["ignored_reports"] += 1
                self.same_reports = 0
                self.same_direction = None
                return
            self.summary["eligible_reports"] += 1
            if self.same_direction is not None and feedback != self.same_direction:
                self.mixed = True
            self.same_reports = self.same_reports + 1 if feedback == self.same_direction else 1
            self.same_direction = feedback
            if self.same_reports >= 3 and not self.requested and not self.mixed:
                # FAST 表示输入偏早，未来执行延后；SLOW 则提前，独立于 Profile 正值语义。
                delta_ms = 2.0 if feedback is TimingFeedback.FAST else -2.0
                self.requested = True
                accepted = self.backend.request_future_phase(delta_ms, now)
                self.summary["requests"] += 1
                self.summary["accepted_requests" if accepted else "rejected_requests"] += 1
                self.events.append({"event": "phase-request", "timestamp": now,
                                    "delta_ms": delta_ms, "accepted": accepted,
                                    **self.frame_evidence})
        except Exception as exc:
            self.events.append({"event": "sampling-error", "reason": type(exc).__name__,
                                "timestamp": now, **self.frame_evidence})
            self.close(now, "sampling-error")
