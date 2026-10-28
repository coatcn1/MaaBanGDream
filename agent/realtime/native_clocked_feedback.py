"""新鲜图只提取一次方向；之后以冻结 H 区间和真实后继输入作有限内存归属。"""

from collections import deque
from dataclasses import dataclass
import math

from .native_observation_clock import ClockIdentity


JUDGEMENTS = frozenset({'tap', 'flick', 'down', 'up'})


@dataclass(frozen=True, slots=True)
class ClockedFeedbackTarget:
    run_id: str
    action_token: int
    chunk_sequence: int
    planned_s: float
    actual_s: float
    clock_uncertainty_ms: float
    device_actual_s: float
    device_uncertainty_s: float
    host_lower_s: float
    host_upper_s: float
    observation_identity: ClockIdentity
    observation_epoch: int
    observation_trust_revision: int
    # 此流用真实 commit cursor 归属，不能将不同commit伪装成刷新出来的新判定。
    judgement_sequence: int
    action_tokens: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ClockedFeedbackFrame:
    sequence: int
    request_s: float
    upper_s: float
    observed_s: float
    kind: object
    signals: frozenset
    slow_pixels: int
    fast_pixels: int
    clock_key: tuple


@dataclass(frozen=True, slots=True)
class ClockedFeedbackDecision:
    frame: ClockedFeedbackFrame
    target: ClockedFeedbackTarget | None
    reason: str


class ClockedFeedbackEvidence:
    CAPACITY = 16
    TARGET_TTL_S = .2
    # 仅等待原图对应的真实后继回执闭合；不放宽80ms采样图龄或200ms目标窗口。
    MAX_PENDING_S = .5
    RENDER_WAIT_S = .025
    RELEASE_GUARD_S = .15

    def __init__(self, backend):
        self.backend = backend
        self.frames = deque(maxlen=self.CAPACITY)
        self.consumed_tokens = deque(maxlen=128)
        self.minimum_host_s = float('-inf')
        self.clock_key = None
        self.dropped = 0
        self.contradicted_frames = 0
        self._drain_key = None
        self._pending_deadline_s = float('inf')

    def reset(self, minimum_host_s):
        self.frames.clear()
        self.minimum_host_s = minimum_host_s
        self._drain_key = None

    @staticmethod
    def key(snapshot):
        return snapshot.identity, snapshot.epoch, snapshot.trust_revision

    def _snapshot(self, now):
        snapshot = self.backend.observation_clock_snapshot()
        return snapshot if self.backend._observation_clock_ready(snapshot, max(now, self.backend._clock())) else None

    @staticmethod
    def row_key(row):
        return row.clock_identity, row.observation_epoch, row.trust_revision

    def current_key(self, now):
        snapshot = self._snapshot(now)
        return self.key(snapshot) if snapshot is not None else None

    def enqueue(self, metadata, upper_s, now, kind, signals, slow, fast):
        snapshot = self._snapshot(now)
        if snapshot is None:
            return 'clock-unavailable'
        request = metadata['request_s']
        if not 0 <= now - request <= .080 or not request <= upper_s <= now:
            return 'slow-or-reused-frames'
        if signals:
            retained = deque((frame for frame in self.frames if not frame.signals or frame.signals == signals),
                             maxlen=self.CAPACITY)
            self.contradicted_frames += len(self.frames) - len(retained)
            self.frames = retained
        if len(self.frames) == self.CAPACITY:
            self.dropped += 1
        self.frames.append(ClockedFeedbackFrame(metadata['frame_sequence'], request, upper_s, now,
            kind, frozenset(signals), slow, fast, self.key(snapshot)))
        self._drain_key = None
        return 'clock-attribution-pending'

    @staticmethod
    def _single_tap(raw):
        return (bool(raw.actions) and all(action.kind == 'tap' and action.command == 'd' and action.single_tap
                for action in raw.actions) and len({action.planned_s for action in raw.actions}) == 1)

    def _target(self, row):
        raw, first = row.execution, row.execution.actions[0]
        return ClockedFeedbackTarget(raw.run_id, first.action_token, raw.chunk_sequence, first.planned_s,
            raw.actual_execution_s, self.backend._clock_uncertainty_ms, raw.device_actual_s,
            raw.device_uncertainty_s, row.host_lower_s, row.host_upper_s, row.clock_identity,
            row.observation_epoch, row.trust_revision, raw.command_cursor, raw.action_tokens)

    def _attribute(self, frame, raw_rows, mapped, now):
        by_cursor = {row.execution.command_cursor: row for row in mapped}
        raw_cursors = {row.command_cursor for row in raw_rows}
        for row in reversed(mapped):
            raw = row.execution
            if self.row_key(row) != frame.clock_key:
                return None, 'clock-target-key-mismatch'
            if (raw.command_cursor not in raw_cursors or not self._single_tap(raw)
                    or raw.action_tokens[0] in self.consumed_tokens):
                continue
            if row.host_lower_s < self.minimum_host_s or row.host_upper_s + self.RENDER_WAIT_S > frame.request_s:
                continue
            target = self._target(row)
            # 200ms约束原图曝光相对目标的年龄；后继回执等待另从原图observed起算，
            # 不能再从目标执行起算500ms，否则稀疏后继会让新鲜原图必然过期。
            if frame.upper_s - row.host_lower_s > self.TARGET_TTL_S:
                return target, 'ticket-expired'
            release_upper = raw.last_release_host_upper_s
            if release_upper is not None and (not math.isfinite(release_upper)
                    or frame.request_s - release_upper < self.RELEASE_GUARD_S):
                return target, 'recent-release'
            if (target.clock_uncertainty_ms is None or not math.isfinite(target.clock_uncertainty_ms)
                    or not 0 <= target.clock_uncertainty_ms <= 1
                    or any(abs(raw.actual_execution_s - action.planned_s) > .04 for action in raw.actions)):
                return target, 'untrusted-input-execution'
            following = next((other for other in raw_rows if other.command_cursor > raw.command_cursor
                and any(action.kind in JUDGEMENTS for action in other.actions)), None)
            if following is None:
                return target, 'awaiting-real-successor'
            successor = by_cursor.get(following.command_cursor)
            if successor is None:
                return target, 'awaiting-successor-clock-view'
            if self.row_key(successor) != frame.clock_key:
                return target, 'clock-successor-key-mismatch'
            if successor.host_lower_s <= frame.upper_s:
                return target, 'new-judgement-overwrote-ticket'
            return target, 'qualified'
        return None, 'no-rendered-target'

    def drain(self, now):
        if not self.frames:
            return ()
        snapshot = self._snapshot(now)
        if snapshot is None:
            decisions = tuple(ClockedFeedbackDecision(frame, None, 'clock-unavailable') for frame in self.frames)
            self.frames.clear()
            return decisions
        key = self.key(snapshot)
        now = max(now, self.backend._clock())
        raw = self.backend.execution_commit_snapshot()
        mapped = self.backend.mapped_execution_commit_snapshot(now)
        if self.current_key(now) != key or any(self.row_key(row) != key for row in mapped):
            decisions = tuple(ClockedFeedbackDecision(frame, None, 'clock-cache-key-mismatch') for frame in self.frames)
            self.frames.clear()
            return decisions
        signature = (key, snapshot.probes[-1].sequence, raw[-1].command_cursor if raw else None,
            len(mapped), mapped[-1].execution.command_cursor if mapped else None,
            mapped[0].execution.command_cursor if mapped else None,
            len(self.frames), self.frames[-1].sequence, self.minimum_host_s)
        if signature == self._drain_key and now <= self._pending_deadline_s:
            return ()
        self._drain_key = signature
        self._pending_deadline_s = float('inf')
        remaining, decisions = deque(maxlen=self.CAPACITY), []
        for frame in self.frames:
            if frame.clock_key != key:
                decisions.append(ClockedFeedbackDecision(frame, None, 'clock-epoch-changed'))
                continue
            if now - frame.observed_s > self.MAX_PENDING_S:
                decisions.append(ClockedFeedbackDecision(frame, None, 'attribution-expired'))
                continue
            view = snapshot.frame_view(frame.request_s, frame.upper_s, now_s=now,
                                       execution_offset_s=self.backend._device_clock_offset_s)
            if view is None:
                if snapshot.probes[-1].host_send_s < frame.upper_s:
                    remaining.append(frame)
                    self._pending_deadline_s = min(self._pending_deadline_s, frame.observed_s + self.MAX_PENDING_S)
                else:
                    decisions.append(ClockedFeedbackDecision(frame, None, 'clock-frame-uncovered'))
                continue
            target, reason = self._attribute(frame, raw, mapped, now)
            if reason.startswith('awaiting-'):
                remaining.append(frame)
                self._pending_deadline_s = min(self._pending_deadline_s, frame.observed_s + self.MAX_PENDING_S)
                continue
            # 拒绝的是本张图，不能抢占输入事件的消费权并毒化仍待闭合的合法原图。
            if reason == 'qualified' and target is not None and frame.signals:
                self.consumed_tokens.append(target.action_token)
            decisions.append(ClockedFeedbackDecision(frame, target, reason))
        self.frames = remaining
        return tuple(decisions)
