"""实际执行普通 TAP 组的只读观测票据；owner 不进行截图或等待。"""

from bisect import bisect_left, bisect_right
from collections import Counter, deque
from dataclasses import asdict, dataclass
import math

from .native_observation_clock import JLOG_DEVICE_UNCERTAINTY_S


JUDGEMENT_KINDS = frozenset({"tap", "flick", "down", "up"})


def action_key(action):
    return tuple(action.get(key) for key in ("kind", "due_s", "lane", "contact", "note_index"))


class SingleTapEligibility:
    def __init__(self, actions, *, allow_groups=False):
        ordered = sorted(actions, key=lambda row: float(row["due_s"]))
        counts = Counter(float(row["due_s"]) for row in ordered if row["kind"] in JUDGEMENT_KINDS)
        tap_counts = Counter(float(row["due_s"]) for row in ordered if row["kind"] == "tap")
        interference = sorted(float(row["due_s"]) for row in ordered if row["kind"] in {"flick", "up"})
        active = set()
        self._eligible = set()
        for row in ordered:
            due, kind = float(row["due_s"]), row["kind"]
            homogeneous = counts[due] == tap_counts[due] if allow_groups else counts[due] == 1
            if kind == "tap" and not active and homogeneous:
                lo, hi = bisect_left(interference, due - .2), bisect_right(interference, due + .2)
                if lo == hi:
                    self._eligible.add(action_key(row))
            if kind == "down":
                active.add(row.get("contact"))
            elif kind == "up":
                active.discard(row.get("contact"))
            elif kind == "flick" and row.get("contact", -1) >= 0:
                active.discard(row.get("contact"))
            # move 不结束 HOLD/Slide；未闭合 contact 会保守阻断其后的单键资格。

    def eligible(self, action):
        return action_key(action) in self._eligible


@dataclass(frozen=True, slots=True)
class FeedbackObservationTicket:
    run_id: str
    action_token: int
    chunk_sequence: int
    planned_s: float
    actual_s: float
    clock_uncertainty_ms: float
    judgement_sequence: int
    action_tokens: tuple[int, ...] = ()
    device_actual_s: float | None = None
    device_uncertainty_s: float = JLOG_DEVICE_UNCERTAINTY_S


@dataclass(frozen=True, slots=True)
class FeedbackObservationState:
    ticket: FeedbackObservationTicket | None = None
    judgement_sequence: int = 0
    last_actual_s: float | None = None
    last_uncertainty_ms: float | None = None
    next_judgement_lower_s: float | None = None
    judgement_history: tuple = ()


class FeedbackTicketStream:
    STRIDE = 1
    CAPACITY = 256

    def __init__(self, run_id):
        self.run_id = run_id
        self._snapshot = FeedbackObservationState()
        self._scheduled = {}
        self._residuals = deque(maxlen=64)
        self._judgement_history = deque(maxlen=64)
        self._eligible_count = 0
        self._published_until_s = None
        self._final_published = False
        self.summary = Counter()
        self.events = deque(maxlen=self.CAPACITY)
        self.events_dropped = 0

    def snapshot(self):
        return self._snapshot

    def _record(self, event, **details):
        self.summary[event] += 1
        if len(self.events) == self.CAPACITY:
            self.events_dropped += 1
        self.events.append(dict(event=event, **details))

    def _publish(self, ticket, sequence, actual, uncertainty):
        # 将已发布下一判定的计划时刻向前扩至既有 40ms 残差门限之外，
        # owner 回执尚未到达也不能把后续输入覆盖的截图归给旧票。
        margin = max(.040, max(self._residuals, default=0) + .010)
        margin += uncertainty / 1000 if uncertainty is not None and math.isfinite(uncertainty) else .001
        horizon = float("inf") if self._final_published else self._published_until_s
        next_lower = min(min(self._scheduled.values(), default=float("inf")),
                         horizon if horizon is not None else float("inf")) - margin
        self._snapshot = FeedbackObservationState(ticket, sequence, actual, uncertainty,
            next_lower if math.isfinite(next_lower) else None, tuple(self._judgement_history))

    def publish_receipts(self, receipts, *, published_until_s=None, final_chunk=False):
        if published_until_s is not None:
            self._published_until_s = published_until_s
            self._final_published = bool(final_chunk)
        for receipt in receipts:
            if receipt.get("action_kind") in JUDGEMENT_KINDS:
                self._scheduled[int(receipt["action_token"])] = float(receipt["planned_engine_s"])
        state = self._snapshot
        self._publish(state.ticket, state.judgement_sequence, state.last_actual_s, state.last_uncertainty_ms)

    def observe_commit(self, receipts, actual_s, chunk_sequence, uncertainty, *, device_actual_s=None):
        judgements = [row for row in receipts if row.get("action_kind") in JUDGEMENT_KINDS]
        if not judgements:
            return
        for row in judgements:
            self._scheduled.pop(int(row["action_token"]), None)
            self._residuals.append(abs(actual_s - float(row["planned_engine_s"])))
        state = self._snapshot
        sequence = state.judgement_sequence + len(judgements)
        self._judgement_history.append((state.judgement_sequence + 1, sequence, actual_s, uncertainty))
        ticket = None
        reason = "non-single-tap"
        if (len(judgements) == len(receipts) and all(row.get("feedback_single_tap")
                and row.get("action_kind") == "tap" and row.get("command") == "d" for row in judgements)
                and len({float(row["planned_engine_s"]) for row in judgements}) == 1):
            row = judgements[0]
            if uncertainty is None or not math.isfinite(uncertainty) or not 0 <= uncertainty <= 1:
                reason = "untrusted-clock"
            else:
                self._eligible_count += 1
                if (self._eligible_count - 1) % self.STRIDE == 0:
                    ticket = FeedbackObservationTicket(self.run_id, int(row["action_token"]), chunk_sequence,
                        float(row["planned_engine_s"]), actual_s, uncertainty, sequence,
                        tuple(int(item["action_token"]) for item in judgements), device_actual_s)
                    reason = "ticket-created"
                else:
                    reason = "stride-skipped"
        self._record(reason, action_tokens=[int(row["action_token"]) for row in judgements],
                     actual_s=actual_s, judgement_sequence=sequence)
        self._publish(ticket, sequence, actual_s, uncertainty)

    def report(self):
        return dict(stride=self.STRIDE, capacity=self.CAPACITY, summary=dict(self.summary),
                    dropped=self.events_dropped, events=list(self.events), snapshot=asdict(self._snapshot))
