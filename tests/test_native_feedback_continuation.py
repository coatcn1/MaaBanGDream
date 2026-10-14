"""常开判定条计数 + 效果跟随门禁的续发测试。

与上游 #90 的周期窗模型不同：触发源是判定条本身的连续同向命中（常开 5Hz），
不是每 3 秒开一次的窗。这里覆盖「请求 → 效果回执 → 何时允许下一次请求」的
闭环，验证效果跟随门禁（``_signal_follows_effect``）与上限阻塞（``phase-limit``）
在常开路径下依然成立。
"""

from types import SimpleNamespace

import pytest

from agent.realtime.native_life_feedback import NativeLifeFeedback
from agent.realtime.timing_feedback import TimingFeedback


class PhaseBackend:
    """可编程的相位后端：显式控制回执状态与「输入是否已越过效果切片」。"""

    def __init__(self):
        self._run_id = "run"
        self.now = 0
        self.requests = []
        self.revoked = []
        self.busy = False
        self.input_chunk = 0
        self.input_token = 1000
        self.input_planned = None
        self.input_age = .01
        self.phase = dict(run_id="run", request_sequence=0, event_cursor=0, state="idle",
                          delta_ms=0, original_observed_s=None, first_adjusted_due_s=None,
                          first_affected_chunk_sequence=None, first_affected_action_token=None,
                          cumulative_phase_ms=0, terminal_reason=None)

    def phase_status_snapshot(self):
        if self.busy:
            return None
        return SimpleNamespace(**self.phase, transient_chunk_sequence=self.input_chunk,
                               transient_action_token=self.input_token,
                               transient_planned_s=self.now - .01 if self.input_planned is None else self.input_planned,
                               transient_actual_s=self.now - self.input_age)

    def feedback_input_eligible(self, now):
        self.now = now
        return True

    def request_future_phase(self, delta, observed_s):
        self.requests.append((delta, observed_s, self.now))
        if self.phase["state"] in {"requested", "waiting-touch-boundary", "waiting-gap"}:
            return False
        self.phase.update(request_sequence=self.phase["request_sequence"] + 1,
                          event_cursor=self.phase["event_cursor"] + 1, state="requested",
                          delta_ms=delta, original_observed_s=observed_s,
                          first_adjusted_due_s=None, first_affected_chunk_sequence=None,
                          first_affected_action_token=None, terminal_reason=None)
        return True

    def outcome(self, state, *, due=None, reason=None):
        self.phase.update(event_cursor=self.phase["event_cursor"] + 1, state=state, terminal_reason=reason)
        if state == "applied":
            self.phase["cumulative_phase_ms"] += self.phase["delta_ms"]
            self.phase.update(first_adjusted_due_s=due,
                              first_affected_chunk_sequence=self.phase["request_sequence"] if due is not None else None,
                              first_affected_action_token=self.phase["request_sequence"] if due is not None else None)
            if due is not None:
                self.input_chunk = self.phase["request_sequence"]

    def revoke_future_phase(self, reason):
        self.revoked.append(reason)
        if self.phase["state"] in {"requested", "waiting-touch-boundary", "waiting-gap"}:
            self.outcome("revoked", reason=reason)


def open_scene(sampler, now):
    sampler.observe_scene(now, visible=True)


def hit(sampler, kind, now, *, age=.01):
    # 走与引擎相同的顺序：先确认新鲜帧（填 frame_evidence），再计数。
    assert sampler.fresh({"sequence": sampler.last_sequence + 1, "request_s": now - age,
                          "reused": False}, now)
    return sampler.observe_judgement(kind, now, source="steady")


def pair(sampler, start, kind=TimingFeedback.FAST, *, gap=.05):
    """一对同向命中（streak 2）——常开路径的最小触发单位。"""
    open_scene(sampler, start)
    hit(sampler, kind, start)
    hit(sampler, kind, start + gap)


def request_once(sampler, backend, start, kind=TimingFeedback.FAST):
    pair(sampler, start, kind)
    assert len(backend.requests) == 1
    return backend.requests[-1]


def test_two_same_direction_hits_request_one_step():
    backend = PhaseBackend()
    sampler = NativeLifeFeedback(backend)
    delta, observed, _now = request_once(sampler, backend, 0)
    assert delta == 15.0
    assert observed == pytest.approx(.05)
    assert sampler.summary["requests"] == 1


def test_slow_direction_requests_negative_step():
    backend = PhaseBackend()
    sampler = NativeLifeFeedback(backend)
    delta, _observed, _now = request_once(sampler, backend, 0, TimingFeedback.SLOW)
    assert delta == -15.0


def test_expired_request_retries_on_next_same_direction_pair():
    backend = PhaseBackend()
    sampler = NativeLifeFeedback(backend)
    request_once(sampler, backend, 0)
    backend.outcome("expired", reason="expired")
    pair(sampler, 1)
    assert len(backend.requests) == 2


@pytest.mark.parametrize("state", ["requested", "waiting-touch-boundary", "waiting-gap", "busy", "old-run"])
def test_pending_or_old_status_does_not_resubmit(state):
    backend = PhaseBackend()
    sampler = NativeLifeFeedback(backend)
    request_once(sampler, backend, 0)
    if state == "busy":
        backend.busy = True
    elif state == "old-run":
        backend.phase["run_id"] = "old"
    else:
        backend.phase["state"] = state
    open_scene(sampler, 1)
    for offset in (0, .05, .10, .15):
        hit(sampler, TimingFeedback.FAST, 1 + offset)
    assert len(backend.requests) == 1


@pytest.mark.parametrize("problem", ["no-effect", "before-effect", "old-chunk", "old-token", "old-planned"])
def test_applied_phase_needs_new_affected_input_before_continuation(problem):
    backend = PhaseBackend()
    sampler = NativeLifeFeedback(backend)
    request_once(sampler, backend, 0)
    backend.outcome("applied", due=None if problem == "no-effect" else (100 if problem == "before-effect" else 7))
    if problem == "old-chunk":
        backend.input_chunk = 0
    elif problem == "old-token":
        backend.input_token = 0
    elif problem == "old-planned":
        backend.input_planned = 6
    open_scene(sampler, 1)
    for offset in (0, .05, .10, .15):
        hit(sampler, TimingFeedback.FAST, 1 + offset)
    assert len(backend.requests) == 1


def test_applied_effect_published_later_resumes_continuation():
    backend = PhaseBackend()
    sampler = NativeLifeFeedback(backend)
    request_once(sampler, backend, 0)
    backend.outcome("applied", due=None)
    open_scene(sampler, 1)
    hit(sampler, TimingFeedback.FAST, 1)
    hit(sampler, TimingFeedback.FAST, 1.05)
    assert len(backend.requests) == 1
    # 效果边界晚到：补齐 due/chunk/token，并让输入越过该切片。
    backend.phase.update(event_cursor=backend.phase["event_cursor"] + 1,
                         first_adjusted_due_s=.5, first_affected_chunk_sequence=1,
                         first_affected_action_token=1)
    backend.input_chunk = 1
    backend.input_planned = .6
    open_scene(sampler, 2)
    hit(sampler, TimingFeedback.FAST, 2)
    hit(sampler, TimingFeedback.FAST, 2.05)
    assert len(backend.requests) == 2


def test_phase_limit_direction_recovers_only_after_opposite_headroom():
    backend = PhaseBackend()
    sampler = NativeLifeFeedback(backend)
    request_once(sampler, backend, 0, TimingFeedback.FAST)
    # 该方向撞 C++ 上限：拒绝并标记阻塞。
    backend.outcome("rejected", reason="phase-limit")
    open_scene(sampler, 1)
    for offset in (0, .05, .10, .15):
        hit(sampler, TimingFeedback.FAST, 1 + offset)
    assert len(backend.requests) == 1
    # 反向应用释放余量后，反向可再次请求（不再被阻塞）。
    pair(sampler, 2, TimingFeedback.SLOW)
    assert len(backend.requests) == 2
    assert backend.requests[-1][0] == -15.0


def test_foreign_same_run_request_is_not_our_terminal_receipt():
    backend = PhaseBackend()
    sampler = NativeLifeFeedback(backend)
    request_once(sampler, backend, 0)
    # 同 run 的外来请求（sequence 不匹配）不能替我们结算 pending。
    backend.phase.update(request_sequence=100, state="expired", original_observed_s=8)
    open_scene(sampler, 1)
    for offset in (0, .05, .10, .15):
        hit(sampler, TimingFeedback.FAST, 1 + offset)
    assert len(backend.requests) == 1
