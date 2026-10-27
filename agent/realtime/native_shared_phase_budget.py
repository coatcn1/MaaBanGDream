"""同一冻结游戏时间参考下的共同修正额度；不推断 FAST/SLOW 的物理原因。"""

from collections import deque
import math


class NativeSharedPhaseBudget:
    def __init__(self, run_id, *, feedback_total=0., feedback_sequence=0):
        self.run_id = run_id
        self.feedback_baseline = float(feedback_total)
        self.feedback_total = float(feedback_total)
        self.feedback_credit = 0.
        self.feedback_sequence = feedback_sequence
        self.reference = None
        self.active = False
        self.unresolved_reason = None
        self.effects = deque(maxlen=64)

    def reference_ready(self, reference):
        if reference is None:
            self.unresolved_reason = 'unbridged-game-reference'
            return False
        if self.reference is None:
            self.reference = reference
        if self.reference != reference:
            self.unresolved_reason = 'unbridged-game-reference'
            return False
        return True

    def neutral_baseline(self, feedback_total, feedback_sequence):
        # 只有视觉净债务已归零且新鲜三帧回到中性时调用；保留所有外来真实相位。
        self.feedback_baseline = self.feedback_total = float(feedback_total)
        self.feedback_credit = 0.
        self.feedback_sequence = max(self.feedback_sequence, feedback_sequence)
        self.active = False
        self.unresolved_reason = None

    def effect_valid(self, effect):
        return (effect.run_id == self.run_id and effect.owner in {'feedback', 'game-clock'}
            and effect.state == 'applied' and type(effect.request_sequence) is int
            and 0 < effect.request_sequence < 2 ** 64
            and type(effect.delta_ms) in {int, float} and effect.delta_ms in {-15., 15.}
            and type(effect.owner_cumulative_phase_ms) in {int, float}
            and math.isfinite(effect.owner_cumulative_phase_ms))

    def observe_executed_feedback(self, effect, *, execution_confirmed, before_after_confirmed):
        if not self.effect_valid(effect) or effect.owner != 'feedback':
            self.unresolved_reason = 'foreign-effect-identity'
            return False
        if effect.request_sequence <= self.feedback_sequence:
            previous = next((row for row in self.effects if row[2] == effect.request_sequence), None)
            if previous is not None and previous != (effect.run_id, effect.owner, effect.request_sequence,
                                                    effect.delta_ms, effect.owner_cumulative_phase_ms):
                self.unresolved_reason = 'conflicting-foreign-effect'
                return False
            return True
        values = effect.delta_ms, effect.owner_cumulative_phase_ms
        if (any(type(value) not in {int, float} or not math.isfinite(value) for value in values)
                or abs(self.feedback_total + effect.delta_ms - effect.owner_cumulative_phase_ms) > 1e-6):
            self.unresolved_reason = 'foreign-effect-history-gap'
            return False
        if not execution_confirmed or not before_after_confirmed:
            self.unresolved_reason = 'foreign-effect-evidence-pending'
            return False
        self.feedback_credit += effect.delta_ms
        self.feedback_total = effect.owner_cumulative_phase_ms
        self.feedback_sequence = effect.request_sequence
        self.effects.append((effect.run_id, effect.owner, effect.request_sequence, effect.delta_ms,
                             effect.owner_cumulative_phase_ms))
        self.unresolved_reason = None
        return True

    def target(self, raw_ms):
        if raw_ms == 0:
            return 0.
        sign = 1 if raw_ms > 0 else -1
        credit = min(abs(raw_ms), max(0., sign * self.feedback_credit))
        return sign * (abs(raw_ms) - credit)

    def remaining(self, raw_ms, bounds, visual_owned_ms):
        if self.unresolved_reason is not None:
            return None
        return (self.target(raw_ms) - visual_owned_ms,
                (self.target(bounds[0]) - visual_owned_ms, self.target(bounds[1]) - visual_owned_ms))

    def report(self):
        return dict(active=self.active, feedback_baseline_ms=self.feedback_baseline,
            feedback_total_ms=self.feedback_total, feedback_credit_ms=self.feedback_credit,
            feedback_sequence=self.feedback_sequence, unresolved_reason=self.unresolved_reason,
            credited_effects=list(self.effects), purpose_inference=False)
