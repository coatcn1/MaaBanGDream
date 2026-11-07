"""实际执行普通 TAP 组的只读观测票据；owner 不进行截图或等待。"""

from bisect import bisect_left, bisect_right
from collections import Counter, deque
from dataclasses import asdict, dataclass
import math

from .native_observation_clock import JLOG_DEVICE_UNCERTAINTY_S, ObservationClockView


JUDGEMENT_KINDS = frozenset({"tap", "flick", "down", "up"})


@dataclass(frozen=True, slots=True)
class FeedbackVisualInputSnapshot:
    registered_ordinals: bytes
    registered_host_times_s: tuple
    executed_ordinals: bytes
    recent_executions: tuple


@dataclass(frozen=True, slots=True)
class FeedbackExecutionInterval:
    ordinal: int
    input_start_s: float | None
    commit_end_s: float
    uncertainty_s: float
    clock_key: tuple | None
    provenance: tuple | None


@dataclass(frozen=True, slots=True)
class FrozenFeedbackVisualChart:
    start_time_s: float
    judgements: tuple


class FeedbackReceiptIdentityChain:
    """只读来源身份贯穿重排与跨块事件；任何歧义只让附加反馈失去资格。"""

    def __init__(self, plan, final_actions):
        self.plan = plan
        self._allowed = frozenset(plan.identity_for(action).ordinal for action in final_actions)
        if len(self._allowed) != len(final_actions):
            raise ValueError("随机反馈最终动作身份重复")
        retained_random = {plan.identity_for(action).note_index for action in final_actions
                           if plan.identity_for(action).intentional}
        if retained_random != plan.random_note_indices:
            raise ValueError("随机反馈最终动作丢失有意判定")
        self._registered = set()
        self._pending = {}
        self._tokens = set()
        self._last_chunk = -1
        self.bound_count = 0
        self._registered_bits = bytearray((len(plan.actions) + 7) // 8)
        self._registered_hosts = [None] * len(plan.actions)
        self._registered_host_snapshot = tuple(self._registered_hosts)
        self._executed_bits = bytearray(len(self._registered_bits))
        self._recent_executions = deque(maxlen=128)
        self._by_note = {}
        for identity in plan.actions:
            self._by_note.setdefault(identity.note_index, []).append(identity)
        self._publish_input_snapshot()

    def _publish_input_snapshot(self):
        self._input_snapshot = FeedbackVisualInputSnapshot(bytes(self._registered_bits),
            self._registered_host_snapshot, bytes(self._executed_bits), tuple(self._recent_executions))

    def input_snapshot(self):
        return self._input_snapshot

    @staticmethod
    def _has(bits, ordinal):
        return bool(bits[ordinal // 8] & (1 << (ordinal % 8)))

    @staticmethod
    def source_key(note, kind, due):
        if type(note) is not int or note < 0 or isinstance(due, bool):
            raise ValueError("随机反馈回执来源无效")
        seconds = float(due)
        if not math.isfinite(seconds):
            raise ValueError("随机反馈回执来源无效")
        # 回执已由C++ canonical_time生成；保留原double，禁止再次取整隐藏错误来源。
        return note, str(kind), seconds

    @staticmethod
    def _canonical_source_due(due):
        if isinstance(due, bool):
            raise ValueError("随机反馈注册来源无效")
        scaled = float(due) * 1e9
        if not math.isfinite(scaled):
            raise ValueError("随机反馈注册来源无效")
        # 复现C++ llround的半数远离零；先拆整数/小数，避免大数加0.5再次丢精度。
        nanos = math.trunc(scaled)
        fraction = scaled - nanos
        if fraction >= .5:
            nanos += 1
        elif fraction <= -.5:
            nanos -= 1
        if not -(1 << 63) <= nanos < (1 << 63):
            raise ValueError("随机反馈注册来源超出纳秒范围")
        return float(nanos) / 1e9

    def register_chunk(self, sequence, items, *, registered_host_s=None):
        if type(sequence) is not int or sequence <= self._last_chunk:
            raise ValueError("随机反馈切片乱序或重复")
        additions = {}
        ordinals = set()
        for item in items:
            identity = self.plan.identity_for(item["action"])
            key = self.source_key(identity.note_index, identity.kind,
                                  self._canonical_source_due(item["engine_due_s"]))
            if (identity.ordinal not in self._allowed or identity.ordinal in self._registered
                    or identity.ordinal in ordinals or key in self._pending or key in additions):
                raise ValueError("随机反馈动作来源重复或不唯一")
            additions[key] = identity
            ordinals.add(identity.ordinal)
        self._pending.update(additions)
        self._registered.update(ordinals)
        self._last_chunk = sequence
        for ordinal in ordinals:
            self._registered_bits[ordinal // 8] |= 1 << (ordinal % 8)
            self._registered_hosts[ordinal] = registered_host_s
        if ordinals:
            # 注册事实先于任何socket发送，失败也不回滚成“未发送”。
            self._registered_host_snapshot = tuple(self._registered_hosts)
            self._publish_input_snapshot()

    def bind_receipt(self, receipt):
        token = receipt["action_token"]
        if type(token) is not int or token <= 0 or token in self._tokens:
            raise ValueError("随机反馈回执 token 重复或无效")
        key = self.source_key(receipt["source_note_index"], receipt["action_kind"], receipt["source_due_s"])
        identity = self._pending.pop(key)
        self._tokens.add(token)
        self.bound_count += 1
        return dict(note_index=identity.note_index, original_due_s=identity.original_due_s,
                    identity_ordinal=identity.ordinal, intentional_random_great=identity.intentional,
                    feedback_identity_trusted=True)

    def record_execution(self, receipts, actual_s, uncertainty_s, *, start_s=None, clock_key=None, provenance=None):
        if (not math.isfinite(actual_s) or not math.isfinite(uncertainty_s) or uncertainty_s < 0
                or any(not row.get('feedback_identity_trusted') or type(row.get('identity_ordinal')) is not int
                       or row['identity_ordinal'] not in self._registered
                       or self._has(self._executed_bits, row['identity_ordinal']) for row in receipts)):
            raise ValueError('随机反馈执行身份缺失或重复')
        if start_s is not None and (not math.isfinite(start_s) or start_s > actual_s):
            start_s = None
        for row in receipts:
            ordinal = row['identity_ordinal']
            self._executed_bits[ordinal // 8] |= 1 << (ordinal % 8)
            self._recent_executions.append(FeedbackExecutionInterval(ordinal, start_s, actual_s, uncertainty_s,
                                                                   clock_key, provenance))
        self._publish_input_snapshot()

    def visual_pair_reason(self, pairs, request_s, upper_s, clock_view, *, provenance=None):
        if not isinstance(clock_view, ObservationClockView):
            return 'visual-pair-clock-unavailable'
        exposure = clock_view.host_to_execution(request_s, upper_s)
        if exposure is None or not pairs:
            return 'visual-pair-clock-or-identity-unavailable'
        snapshot = self.input_snapshot()
        seen = set()
        for pair in pairs:
            note = pair.get('note_index')
            identities = self._by_note.get(note, ())
            if type(note) is not int or note in seen or len(identities) != 1:
                return 'visual-pair-identity-unavailable'
            seen.add(note)
            identity = identities[0]
            if identity.ordinal not in self._allowed:
                return 'visual-pair-omitted-input'
            if (identity.kind != 'tap' or identity.planned_key[4] != -1 or identity.planned_key[6] is not None
                    or pair.get('lane') != identity.planned_key[2]
                    or type(pair.get('due_s')) not in {int, float}
                    or not math.isfinite(pair['due_s'])
                    or round(pair['due_s'] * 1e9) != round(identity.original_due_s * 1e9)):
                return 'visual-pair-not-original-normal-tap'
            ordinal = identity.ordinal
            if self._has(snapshot.executed_ordinals, ordinal):
                fact = next((row for row in reversed(snapshot.recent_executions) if row.ordinal == ordinal), None)
                if fact is None:
                    return 'visual-pair-execution-proof-unavailable'
                if fact.input_start_s is None:
                    return 'visual-pair-execution-start-unavailable'
                if (fact.clock_key != (clock_view.identity, clock_view.epoch, clock_view.trust_revision)
                        or fact.provenance is None or provenance is None or fact.provenance != provenance):
                    return 'visual-pair-execution-context-untrusted'
                if math.nextafter(fact.input_start_s - fact.uncertainty_s, float('-inf')) <= exposure[1]:
                    return 'visual-pair-input-may-cover-exposure'
            elif self._has(snapshot.registered_ordinals, ordinal):
                registered = snapshot.registered_host_times_s[ordinal]
                if registered is None or not math.isfinite(registered) or registered <= upper_s:
                    return 'visual-pair-input-pending'
        return None


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
    execution_engine_s: float | None = None
    execution_engine_uncertainty_ms: float | None = None


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

    def observe_commit(self, receipts, actual_s, chunk_sequence, uncertainty, *, device_actual_s=None, execution_engine_s=None, execution_engine_uncertainty_ms=None):
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
                        tuple(int(item["action_token"]) for item in judgements), device_actual_s,
                        execution_engine_s=execution_engine_s,
                        execution_engine_uncertainty_ms=execution_engine_uncertainty_ms)
                    reason = "ticket-created"
                else:
                    reason = "stride-skipped"
        self._record(reason, action_tokens=[int(row["action_token"]) for row in judgements],
                     actual_s=actual_s, judgement_sequence=sequence)
        self._publish(ticket, sequence, actual_s, uncertainty)

    def report(self):
        return dict(stride=self.STRIDE, capacity=self.CAPACITY, summary=dict(self.summary),
                    dropped=self.events_dropped, events=list(self.events), snapshot=asdict(self._snapshot))
