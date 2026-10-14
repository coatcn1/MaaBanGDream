from types import SimpleNamespace

import numpy as np
import pytest

from agent.realtime.native_life_feedback import NativeLifeFeedback
from agent.realtime.timing_feedback import TimingFeedback


class PhaseBackend:
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


def sample(sampler, kind, now, *, age=.01):
    assert sampler.fresh({"sequence": sampler.last_sequence + 1, "request_s": now - age, "reused": False}, now)
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    if kind is not None:
        image[514:556, 570:710] = (255, 180, 0) if kind is TimingFeedback.FAST else (0, 140, 255)
    sampler.observe_frame(image, now)


def window(sampler, kind, start):
    sampler.observe_scene(start, visible=True)
    for index in range(16):
        sample(sampler, kind, start + index * .05)
    sampler.observe_scene(start + .8, visible=True)


def qualified():
    backend = PhaseBackend()
    sampler = NativeLifeFeedback(backend)
    for start in (0, 3, 6):
        window(sampler, TimingFeedback.FAST, start)
    assert len(backend.requests) == 1
    return sampler, backend


def test_expired_request_retries_on_next_new_same_direction_window():
    sampler, backend = qualified()
    backend.outcome("expired", reason="expired")
    window(sampler, TimingFeedback.FAST, 9)
    assert len(backend.requests) == 2
    assert backend.requests[-1][1] == pytest.approx(9.75)


def test_no_feedback_keeps_live_votes_but_never_requests_from_old_evidence():
    backend = PhaseBackend()
    sampler = NativeLifeFeedback(backend)
    window(sampler, TimingFeedback.SLOW, 0)
    window(sampler, None, 3)
    assert len(sampler.votes) == 1
    window(sampler, TimingFeedback.SLOW, 6)
    window(sampler, TimingFeedback.SLOW, 9)
    assert len(backend.requests) == 1
    backend.outcome("expired", reason="expired")
    for start in (12, 15, 18, 21):
        window(sampler, None, start)
    assert not sampler.votes
    window(sampler, TimingFeedback.SLOW, 24)
    assert len(backend.requests) == 1


@pytest.mark.parametrize("state", ["requested", "waiting-touch-boundary", "waiting-gap", "busy", "old-run"])
def test_pending_unavailable_or_old_run_status_does_not_resubmit(state):
    sampler, backend = qualified()
    if state == "busy":
        backend.busy = True
    elif state == "old-run":
        backend.phase["run_id"] = "old"
    else:
        backend.phase["state"] = state
    for start in (9, 12, 15):
        window(sampler, TimingFeedback.FAST, start)
    assert len(backend.requests) == 1


@pytest.mark.parametrize("problem", ["no-effect", "before-effect", "old-chunk", "old-token", "old-planned"])
def test_applied_phase_needs_new_affected_input_before_continuation(problem):
    sampler, backend = qualified()
    backend.outcome("applied", due=None if problem == "no-effect" else (100 if problem == "before-effect" else 7))
    if problem == "old-chunk":
        backend.input_chunk = 0
    elif problem == "old-token":
        backend.input_token = 0
    elif problem == "old-planned":
        backend.input_planned = 6
    for start in (9, 12, 15):
        window(sampler, TimingFeedback.FAST, start)
    assert len(backend.requests) == 1


def test_applied_effect_published_later_is_seen_and_resumes_without_three_more_windows():
    sampler, backend = qualified()
    backend.outcome("applied", due=None)
    window(sampler, TimingFeedback.FAST, 9)
    assert len(backend.requests) == 1
    backend.phase.update(event_cursor=backend.phase["event_cursor"] + 1, first_adjusted_due_s=10,
                         first_affected_chunk_sequence=1, first_affected_action_token=1)
    backend.input_chunk = 1
    window(sampler, TimingFeedback.FAST, 12)
    assert len(backend.requests) == 2


def test_trusted_opposite_colour_immediately_revokes_then_requires_three_new_windows():
    sampler, backend = qualified()
    sampler.observe_scene(9, visible=True)
    sample(sampler, TimingFeedback.SLOW, 9)
    assert backend.revoked and backend.phase["state"] == "revoked"
    for index in range(1, 16):
        sample(sampler, TimingFeedback.SLOW, 9 + index * .05)
    sampler.observe_scene(9.8, visible=True)
    window(sampler, TimingFeedback.SLOW, 12)
    assert len(backend.requests) == 1
    window(sampler, TimingFeedback.SLOW, 15)
    assert len(backend.requests) == 2
    assert backend.requests[-1][0] == -2


def test_continuous_fast_keeps_adjusting_until_existing_phase_limit():
    backend = PhaseBackend()
    sampler = NativeLifeFeedback(backend)
    for start in range(0, 84, 3):
        previous = len(backend.requests)
        window(sampler, TimingFeedback.FAST, start)
        if len(backend.requests) > previous:
            if backend.phase["cumulative_phase_ms"] == 40:
                backend.outcome("rejected", reason="phase-limit")
            else:
                backend.outcome("applied", due=start + .85)
    assert backend.phase["cumulative_phase_ms"] == 40
    sampler.refresh_phase_status(start)
    assert len(backend.requests) == 21
    assert all(right[2] - left[2] >= 3 for left, right in zip(backend.requests, backend.requests[1:]))
    assert backend.phase["terminal_reason"] == "phase-limit"


def test_prefetched_old_frame_cannot_borrow_a_down_executed_after_request():
    sampler, backend = qualified()
    backend.outcome("applied", due=7)
    backend.input_age = .02
    sampler.observe_scene(9, visible=True)
    for index in range(16):
        sample(sampler, TimingFeedback.FAST, 9 + index * .05, age=.06)
    sampler.observe_scene(9.8, visible=True)
    assert len(backend.requests) == 1


def test_phase_limit_direction_recovers_only_after_opposite_applied_headroom():
    backend = PhaseBackend()
    sampler = NativeLifeFeedback(backend)
    start = 0.0
    while backend.phase["terminal_reason"] != "phase-limit":
        previous = len(backend.requests)
        window(sampler, TimingFeedback.FAST, start)
        if len(backend.requests) > previous:
            backend.outcome("rejected" if backend.phase["cumulative_phase_ms"] == 40 else "applied",
                            reason="phase-limit" if backend.phase["cumulative_phase_ms"] == 40 else None,
                            due=start + .85)
        start += 3.1
        assert start < 100
    assert backend.phase["cumulative_phase_ms"] == 40
    before = len(backend.requests)
    for _ in range(3):
        window(sampler, TimingFeedback.SLOW, start)
        start += 3.1
    assert len(backend.requests) == before + 1
    backend.outcome("applied", due=start - 3.1 + .85)
    assert backend.phase["cumulative_phase_ms"] == 38
    for _ in range(3):
        window(sampler, TimingFeedback.FAST, start)
        start += 3.1
    assert len(backend.requests) == before + 2


def test_foreign_same_run_request_is_not_our_terminal_receipt():
    sampler, backend = qualified()
    backend.phase.update(request_sequence=100, state="expired", original_observed_s=8)
    window(sampler, TimingFeedback.FAST, 9)
    assert len(backend.requests) == 1


def test_next_window_is_delayed_so_small_close_jitter_does_not_skip_a_period():
    backend = PhaseBackend()
    sampler = NativeLifeFeedback(backend)
    for start in (0, 3):
        window(sampler, TimingFeedback.FAST, start)
    sampler.observe_scene(6, visible=True)
    for index in range(16):
        sample(sampler, TimingFeedback.FAST, 6 + index * .05)
    sampler.observe_scene(6.83, visible=True)
    assert len(backend.requests) == 1
    assert sampler.next_window_at >= 9.03
    backend.outcome("expired", reason="expired")
    window(sampler, TimingFeedback.FAST, sampler.next_window_at)
    assert len(backend.requests) == 2
    assert backend.requests[-1][2] - backend.requests[-2][2] >= 3
